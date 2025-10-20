"""Asynchronous job management primitives for the FastAPI server."""

from __future__ import annotations

import asyncio
import shlex
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import (
    Callable,
    Deque,
    Dict,
    Iterable,
    List,
    MutableMapping,
    Optional,
    Sequence,
    Set,
)

DEFAULT_MAX_LOG_LINES = 2000
DEFAULT_RETENTION_SECONDS = 3600
DEFAULT_CLEANUP_INTERVAL_SECONDS = 300
DEFAULT_MAX_CONCURRENT = 2
DEFAULT_QUEUE_LIMIT = 50

__all__ = [
    "ActionDefinition",
    "ActionOption",
    "Job",
    "JobLogStore",
    "JobManager",
    "JobRejectedError",
    "JobStatus",
]


class JobStatus(str, Enum):
    pending = "pending"
    running = "running"
    success = "success"
    error = "error"


@dataclass(frozen=True)
class ActionOption:
    id: str
    label: str
    args: Sequence[str]
    description: str
    default_selected: bool = False


@dataclass(frozen=True)
class ActionDefinition:
    id: str
    title: str
    description: str
    command: Sequence[str]
    default_args: Sequence[str] = field(default_factory=tuple)
    options: Sequence[ActionOption] = field(default_factory=tuple)
    env: MutableMapping[str, str] = field(default_factory=dict)
    hint: str | None = None

    def default_arguments(self) -> List[str]:
        parts = [*self.default_args]
        for option in self.options:
            if option.default_selected:
                parts.extend(option.args)
        return parts

    def display_command(self) -> str:
        parts = [*self.command, *self.default_arguments()]
        return " ".join(shlex.quote(p) for p in parts)


@dataclass
class LogEntry:
    seq: int
    timestamp: datetime
    stream: str
    text: str

    def to_payload(self) -> Dict[str, object]:
        return {
            "seq": self.seq,
            "timestamp": self.timestamp.isoformat(),
            "stream": self.stream,
            "text": self.text,
        }


class JobLogStore:
    def __init__(self, max_lines: int = DEFAULT_MAX_LOG_LINES) -> None:
        self._max_lines = max_lines
        self._entries: List[LogEntry] = []
        self._latest_seq = 0

    def append(self, stream: str, text: str) -> LogEntry:
        self._latest_seq += 1
        entry = LogEntry(
            seq=self._latest_seq,
            timestamp=datetime.now(timezone.utc),
            stream=stream,
            text=text,
        )
        self._entries.append(entry)
        if len(self._entries) > self._max_lines:
            self._entries = self._entries[-self._max_lines :]
        return entry

    def collect(self, cursor: int) -> tuple[List[Dict[str, object]], int]:
        if cursor <= 0:
            items = list(self._entries)
        else:
            items = [entry for entry in self._entries if entry.seq > cursor]
        return [entry.to_payload() for entry in items], self._latest_seq


class JobRejectedError(RuntimeError):
    """Raised when a job cannot be queued due to capacity limits."""


class Job:
    def __init__(
        self,
        action: ActionDefinition,
        args: Sequence[str] | None = None,
        *,
        log_store: JobLogStore | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex
        self.action = action
        self.command = list(action.command)
        effective_args: List[str] = []
        if args is None:
            effective_args.extend(action.default_arguments())
        else:
            effective_args.extend(args)
        if effective_args:
            self.command.extend(effective_args)
        self.created_at = datetime.now(timezone.utc)
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.exit_code: int | None = None
        self.status: JobStatus = JobStatus.pending
        self._logs = log_store or JobLogStore()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._cancel_requested = False

    def display_command(self) -> str:
        return " ".join(shlex.quote(part) for part in self.command)

    def set_task(self, task: asyncio.Task[None]) -> None:
        self._task = task

    def set_process(self, process: asyncio.subprocess.Process | None) -> None:
        self._process = process

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested

    async def request_cancel(self) -> bool:
        should_log = False
        async with self._lock:
            if self.status not in (JobStatus.pending, JobStatus.running):
                return False
            if not self._cancel_requested:
                self._cancel_requested = True
                should_log = True

        if should_log:
            await self.append_log("system", "终止命令请求已发送…")

        process = self._process
        if process and process.returncode is None:
            try:
                # 先发送 SIGTERM，给进程优雅退出的机会
                process.terminate()
                await self.append_log("system", "已发送 SIGTERM 信号，等待进程响应…")

                # 在后台等待一段时间，如果进程还没退出就强制 kill
                async def _force_kill_after_timeout():
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5.0)
                        # 进程已正常退出
                        if process.returncode is not None:
                            await self.append_log("system", f"进程已退出，退出码: {process.returncode}")
                    except asyncio.TimeoutError:
                        # 超时后强制 kill
                        if process.returncode is None:
                            try:
                                await self.append_log("system", "进程未在 5 秒内响应，正在强制终止 (SIGKILL)…")
                                process.kill()
                                await asyncio.sleep(0.5)  # 等待一小会儿
                                if process.returncode is None:
                                    await self.append_log("system", "已发送 SIGKILL 信号。")
                                else:
                                    await self.append_log("system", f"进程已被强制终止，退出码: {process.returncode}")
                            except ProcessLookupError:
                                await self.append_log("system", "进程已不存在。")

                # 创建后台任务，不等待它完成
                asyncio.create_task(_force_kill_after_timeout())
            except ProcessLookupError:
                await self.append_log("system", "进程已不存在。")

        return True

    async def append_log(self, stream: str, text: str) -> None:
        async with self._lock:
            self._logs.append(stream, text)

    async def collect_logs(self, cursor: int) -> tuple[List[Dict[str, object]], int]:
        async with self._lock:
            return self._logs.collect(cursor)

    def snapshot(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "actionId": self.action.id,
            "title": self.action.title,
            "status": self.status.value,
            "command": self.command,
            "displayCommand": self.display_command(),
            "createdAt": self.created_at.isoformat(),
            "startedAt": self.started_at.isoformat() if self.started_at else None,
            "finishedAt": self.finished_at.isoformat() if self.finished_at else None,
            "exitCode": self.exit_code,
            "cancelRequested": self.cancel_requested,
        }


class JobManager:
    def __init__(
        self,
        *,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
        cleanup_interval: int = DEFAULT_CLEANUP_INTERVAL_SECONDS,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        queue_limit: int = DEFAULT_QUEUE_LIMIT,
        log_store_factory: Callable[[], JobLogStore] | None = None,
    ) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = asyncio.Lock()
        self._retention_seconds = max(retention_seconds, 0)
        self._cleanup_interval = cleanup_interval if cleanup_interval > 0 else 0
        self._max_concurrent = max_concurrent if max_concurrent >= 0 else 0
        self._queue_limit = queue_limit if queue_limit >= 0 else 0
        self._log_store_factory = log_store_factory or (lambda: JobLogStore())
        self._cleanup_task: asyncio.Task[None] | None = None
        self._pending: Deque[Job] = deque()
        self._pending_set: Set[str] = set()
        self._running: Set[str] = set()

    async def create_job(self, action: ActionDefinition, args: Sequence[str] | None = None) -> Job:
        job = Job(action, args, log_store=self._log_store_factory())
        queued = False
        queue_position = 0
        to_start: List[Job] = []
        async with self._lock:
            can_start = self._max_concurrent <= 0 or len(self._running) < self._max_concurrent
            if not can_start and self._queue_limit > 0 and len(self._pending) >= self._queue_limit:
                raise JobRejectedError("并发队列已满，请稍后重试。")
            self._jobs[job.id] = job
            if can_start:
                self._running.add(job.id)
                to_start.append(job)
            else:
                self._pending.append(job)
                self._pending_set.add(job.id)
                queue_position = len(self._pending)
                queued = True

        for candidate in to_start:
            candidate.set_task(asyncio.create_task(self._run_job(candidate)))
        if queued:
            await job.append_log("system", f"并发限制已满，任务已排队（当前第 {queue_position} 位）。")

        await self._prune_jobs()
        return job

    async def _cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._cleanup_interval)
                await self._prune_jobs()
        except asyncio.CancelledError:
            pass

    def start_cleanup_task(self) -> None:
        if self._cleanup_interval <= 0 or self._retention_seconds <= 0:
            return
        if self._cleanup_task and not self._cleanup_task.done():
            return
        loop = asyncio.get_running_loop()
        self._cleanup_task = loop.create_task(self._cleanup_loop())

    async def stop_cleanup_task(self) -> None:
        task = self._cleanup_task
        if not task:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._cleanup_task = None

    async def _schedule_pending_jobs(self) -> None:
        to_start: List[Job] = []
        async with self._lock:
            while self._pending and (self._max_concurrent <= 0 or len(self._running) < self._max_concurrent):
                candidate = self._pending.popleft()
                self._pending_set.discard(candidate.id)
                if candidate.status in (JobStatus.success, JobStatus.error):
                    continue
                self._running.add(candidate.id)
                to_start.append(candidate)
                if self._max_concurrent > 0 and len(self._running) >= self._max_concurrent:
                    break
        for job in to_start:
            job.set_task(asyncio.create_task(self._run_job(job)))

    async def _run_job(self, job: Job) -> None:
        async with self._lock:
            if job.status != JobStatus.pending:
                return
            job.status = JobStatus.running
            job.started_at = datetime.now(timezone.utc)
        try:
            import os
            env = os.environ.copy()
            env.update(job.action.env)
            env["PYTHONUNBUFFERED"] = "1"
            proc = await asyncio.create_subprocess_exec(
                *job.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            job.set_process(proc)

            async def _pump(stream: asyncio.StreamReader, name: str) -> None:
                while True:
                    chunk = await stream.readline()
                    if not chunk:
                        break
                    text = chunk.decode(errors="replace").rstrip()
                    await job.append_log(name, text)

            await asyncio.gather(
                _pump(proc.stdout or asyncio.StreamReader(), "stdout"),
                _pump(proc.stderr or asyncio.StreamReader(), "stderr"),
            )
            await proc.wait()
            exit_code = proc.returncode

            # 记录进程退出
            if job.cancel_requested:
                await job.append_log("system", f"进程已终止，退出码: {exit_code}")
        except Exception as exc:  # pragma: no cover - subprocess failure branch
            exit_code = -1
            await job.append_log("system", f"执行失败: {exc}")
        finally:
            async with self._lock:
                job.finished_at = datetime.now(timezone.utc)
                job.exit_code = exit_code
                job.status = JobStatus.success if exit_code == 0 else JobStatus.error
                self._running.discard(job.id)

        await self._schedule_pending_jobs()
        await self._prune_jobs()

    async def _prune_jobs(self) -> None:
        if self._retention_seconds <= 0:
            return
        cutoff = datetime.now(timezone.utc).timestamp() - self._retention_seconds
        async with self._lock:
            doomed = [
                job_id
                for job_id, job in self._jobs.items()
                if job.finished_at and job.finished_at.timestamp() < cutoff
            ]
            for job_id in doomed:
                self._jobs.pop(job_id, None)
                self._pending_set.discard(job_id)
                self._running.discard(job_id)

    async def list_jobs(self) -> Iterable[Job]:
        async with self._lock:
            return list(self._jobs.values())

    async def get_job(self, job_id: str) -> Job:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            return job

    async def terminate_job(self, job_id: str) -> Job:
        job = await self.get_job(job_id)
        await job.request_cancel()
        return job
