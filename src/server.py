from __future__ import annotations

import asyncio
import os
import shlex
import sys
import uuid
import copy
import json
import threading
import time
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Callable, Deque, Dict, Iterable, List, MutableMapping, Optional, Sequence, Set

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from config import load_config
from mapper import extract_status_label
from sync import _story_owner_tokens, _collect_frontend_assignees
from tapd_client import TAPDClient
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
MAX_LOG_LINES = 2000


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


JOB_RETENTION_SECONDS = max(_env_int("JOB_RETENTION_SECONDS", 3600), 0)
JOB_CLEANUP_INTERVAL_SECONDS = max(_env_int("JOB_CLEANUP_INTERVAL_SECONDS", 300), 0)
JOB_MAX_CONCURRENT = max(_env_int("JOB_MAX_CONCURRENT", 2), 0)
JOB_QUEUE_LIMIT = max(_env_int("JOB_QUEUE_LIMIT", 50), 0)
STORY_CACHE_TTL_SECONDS = max(_env_int("TAPD_STORY_CACHE_TTL", 200), 0)
STORY_CACHE_MAX_ENTRIES = max(_env_int("TAPD_STORY_CACHE_MAX", 4), 0)
ITERATION_CACHE_TTL_SECONDS = max(_env_int("TAPD_ITERATION_CACHE_TTL", 60), 0)

_story_cache_lock = threading.Lock()
_story_cache: "OrderedDict[str, tuple[float, 'StoryCollectionResponse']]" = OrderedDict()
_iteration_cache_lock = threading.Lock()
_iteration_cache: Dict[str, tuple[float, tuple[Optional[dict], Optional[str], Optional[str]]]] = {}


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
    def __init__(self, max_lines: int = MAX_LOG_LINES) -> None:
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
    pass


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
                process.terminate()
            except ProcessLookupError:
                pass

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
        retention_seconds: int = JOB_RETENTION_SECONDS,
        cleanup_interval: int = JOB_CLEANUP_INTERVAL_SECONDS,
        max_concurrent: int = JOB_MAX_CONCURRENT,
        queue_limit: int = JOB_QUEUE_LIMIT,
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
            await job.append_log("system", "命令已从等待队列取出，准备执行…")
            job.set_task(asyncio.create_task(self._run_job(job)))

    async def _cancel_pending_job(self, job: Job) -> bool:
        removed = False
        async with self._lock:
            if job.id in self._pending_set:
                self._pending = deque(item for item in self._pending if item.id != job.id)
                self._pending_set.discard(job.id)
                removed = True
        if removed:
            job.status = JobStatus.error
            job.finished_at = datetime.now(timezone.utc)
            await job.append_log("system", "命令在启动前被取消，将不会执行。")
        return removed

    async def _mark_job_finished(self, job: Job) -> None:
        async with self._lock:
            self._running.discard(job.id)
        await self._schedule_pending_jobs()

    async def _prune_jobs(self) -> None:
        if self._retention_seconds <= 0:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self._retention_seconds)
        async with self._lock:
            stale_ids = [
                job_id
                for job_id, job in list(self._jobs.items())
                if job.finished_at and job.finished_at < cutoff
            ]
            if not stale_ids:
                return
            stale_set = set(stale_ids)
            self._pending = deque(item for item in self._pending if item.id not in stale_set)
            self._pending_set.difference_update(stale_set)
            for job_id in stale_ids:
                self._jobs.pop(job_id, None)
                self._running.discard(job_id)

    async def _run_job(self, job: Job) -> None:
        try:
            await job.append_log("system", f"Starting command: {job.display_command()}")
            job.started_at = datetime.now(timezone.utc)

            env = os.environ.copy()
            env.update(job.action.env)
            env.setdefault("PYTHONUNBUFFERED", "1")

            if job.cancel_requested:
                job.status = JobStatus.error
                job.finished_at = datetime.now(timezone.utc)
                await job.append_log("system", "命令在启动前已被终止。")
                return

            try:
                process = await asyncio.create_subprocess_exec(
                    *job.command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(REPO_ROOT),
                    env=env,
                )
            except Exception as exc:  # pragma: no cover - defensive path
                job.status = JobStatus.error
                job.finished_at = datetime.now(timezone.utc)
                await job.append_log("system", f"Failed to spawn process: {exc}")
                return

            job.set_process(process)
            job.status = JobStatus.running

            assert process.stdout is not None
            assert process.stderr is not None

            async def read_stream(stream: asyncio.StreamReader, label: str) -> None:
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                    prefix = label if label != "stdout" else "stdout"
                    await job.append_log(prefix, text)

            stdout_task = asyncio.create_task(read_stream(process.stdout, "stdout"))
            stderr_task = asyncio.create_task(read_stream(process.stderr, "stderr"))

            if job.cancel_requested and process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass

            try:
                await asyncio.wait({stdout_task, stderr_task})
                job.exit_code = await process.wait()
            except asyncio.CancelledError:
                if process.returncode is None:
                    process.terminate()
                    job.exit_code = await process.wait()
                else:
                    job.exit_code = process.returncode
            finally:
                job.set_process(None)

            job.finished_at = datetime.now(timezone.utc)
            if job.cancel_requested:
                job.status = JobStatus.error
                await job.append_log("system", "命令已被终止。")
            elif job.exit_code == 0:
                job.status = JobStatus.success
                await job.append_log("system", "Command finished successfully.")
            else:
                job.status = JobStatus.error
                await job.append_log("system", f"Command failed with exit code {job.exit_code}.")
        finally:
            if job.finished_at is None:
                job.finished_at = datetime.now(timezone.utc)
            await self._mark_job_finished(job)
            await self._prune_jobs()

    async def get_job(self, job_id: str) -> Job:
        await self._prune_jobs()
        async with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        return job

    async def terminate_job(self, job_id: str) -> Job:
        job = await self.get_job(job_id)
        if job.status in (JobStatus.success, JobStatus.error):
            return job
        await job.request_cancel()
        if await self._cancel_pending_job(job):
            await self._prune_jobs()
        return job

    async def list_jobs(self) -> List[Job]:
        await self._prune_jobs()
        async with self._lock:
            return sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)


ACTIONS: Dict[str, ActionDefinition] = {
    "pull-to-notion": ActionDefinition(
        id="pull-to-notion",
        title="拉取需求到 Notion",
        description="增量同步 TAPD 需求并写入 Notion（默认当前迭代）。",
        command=("python3", str(SCRIPTS_DIR / "pull")),
        default_args=(),
        options=(
            ActionOption(
                id="execute",
                label="--execute",
                args=("--execute",),
                description="实际写入 Notion（默认 dry-run）。",
                default_selected=True,
            ),
            ActionOption(
                id="current-iteration",
                label="--current-iteration",
                args=("--current-iteration",),
                description="仅同步当前迭代（推荐开启）。",
                default_selected=True,
            ),
            ActionOption(
                id="full",
                label="--full",
                args=("--full",),
                description="执行全量初始化，忽略增量 since 边界。",
            ),
            ActionOption(
                id="by-modules",
                label="--by-modules",
                args=("--by-modules",),
                description="按模块分组同步，兼容多租户过滤键。",
            ),
            ActionOption(
                id="wipe-first",
                label="--wipe-first",
                args=("--wipe-first",),
                description="写入前清空 Notion 数据库（高风险操作）。",
            ),
            ActionOption(
                id="insert-only",
                label="--insert-only",
                args=("--insert-only",),
                description="仅新增记录，已存在页面不更新。",
            ),
            ActionOption(
                id="update-after",
                label="--update-after",
                args=("--update-after",),
                description="同步完成后再执行一次 update-all。",
            ),
            ActionOption(
                id="no-post-update",
                label="--no-post-update",
                args=("--no-post-update",),
                description="禁止同步完成后的自动更新步骤。",
            ),
        ),
        hint="需要前置好 TAPD/Notion 凭证，若仅试运行可去掉 --execute。",
    ),
    "update-requirements": ActionDefinition(
        id="update-requirements",
        title="更新需求状态",
        description="刷新 Notion 中已存在需求的字段，保持与 TAPD 一致。",
        command=("python3", str(SCRIPTS_DIR / "update")),
        default_args=(),
        options=(
            ActionOption(
                id="execute",
                label="--execute",
                args=("--execute",),
                description="实际写入 Notion（默认 dry-run）。",
                default_selected=True,
            ),
            ActionOption(
                id="current-iteration",
                label="--current-iteration",
                args=("--current-iteration",),
                description="仅更新当前迭代的需求（推荐开启）。",
                default_selected=True,
            ),
            ActionOption(
                id="from-notion",
                label="--from-notion",
                args=("--from-notion",),
                description="仅根据 Notion 已有页面执行更新（不会访问 TAPD）。",
            ),
            ActionOption(
                id="create-missing",
                label="--create-missing",
                args=("--create-missing",),
                description="在按 ID 更新时，缺失页面将自动创建。",
            ),
        ),
        hint="默认更新当前迭代；想要 dry-run 可去掉 --execute。",
    ),
    "generate-testcases": ActionDefinition(
        id="generate-testcases",
        title="生成测试用例",
        description="按需求生成测试用例并输出 XMind 附件。",
        command=("python3", str(SCRIPTS_DIR / "testflow")),
        default_args=(),
        options=(
            ActionOption(
                id="execute",
                label="--execute",
                args=("--execute",),
                description="实际写入附件到磁盘（默认 dry-run）。",
                default_selected=True,
            ),
            ActionOption(
                id="ack",
                label="--ack web-ui",
                args=("--ack", "web-ui"),
                description="执行模式下的确认令牌，默认使用 web-ui。",
                default_selected=True,
            ),
            ActionOption(
                id="current-iteration",
                label="--current-iteration",
                args=("--current-iteration",),
                description="仅处理当前迭代的需求（推荐开启）。",
                default_selected=True,
            ),
            ActionOption(
                id="send-mail",
                label="--send-mail",
                args=("--send-mail",),
                description="生成附件后通过邮件发送（需配置邮件服务和 --ack-mail）。",
            ),
        ),
        hint="生成文件保存在 testflow 输出目录；如需邮件发送请手动加 --send-mail。",
    ),
}


def list_action_payloads() -> Iterable[Dict[str, object]]:
    for action in ACTIONS.values():
        yield {
            "id": action.id,
            "title": action.title,
            "description": action.description,
            "defaultArgs": list(action.default_args),
            "commandPreview": action.display_command(),
            "hint": action.hint,
            "options": [
                {
                    "id": option.id,
                    "label": option.label,
                    "args": list(option.args),
                    "description": option.description,
                    "defaultSelected": option.default_selected,
                }
                for option in action.options
            ],
        }


class JobCreateRequest(BaseModel):
    action_id: str = Field(..., alias="actionId")
    extra_args: List[str] = Field(default_factory=list, alias="extraArgs")
    args: Optional[List[str]] = None
    story_ids: List[str] = Field(default_factory=list, alias="storyIds")


class JobResponse(BaseModel):
    id: str
    actionId: str
    title: str
    status: JobStatus
    command: List[str]
    displayCommand: str
    createdAt: datetime
    startedAt: datetime | None
    finishedAt: datetime | None
    exitCode: int | None
    cancelRequested: bool
    logs: List[Dict[str, object]]
    nextCursor: int


class JobSummaryResponse(BaseModel):
    id: str
    actionId: str
    title: str
    status: JobStatus
    createdAt: datetime
    finishedAt: datetime | None
    exitCode: int | None


class StorySummaryResponse(BaseModel):
    id: str
    title: str
    status: Optional[str]
    owners: List[str]
    iteration: Optional[str]
    updatedAt: Optional[str]
    frontend: Optional[str] = None
    url: Optional[str] = None


class StoryOwnerAggregateResponse(BaseModel):
    name: str
    count: int


class StoryQuickOwnerAggregateResponse(BaseModel):
    name: str
    owners: List[str]
    count: int


class StoryCollectionResponse(BaseModel):
    stories: List[StorySummaryResponse]
    total: int
    owners: List[StoryOwnerAggregateResponse]
    quickOwners: List[StoryQuickOwnerAggregateResponse]
    truncated: bool = False


job_manager = JobManager(
    max_concurrent=JOB_MAX_CONCURRENT,
    queue_limit=JOB_QUEUE_LIMIT,
)
app = FastAPI(title="TAPD Workflow Automation")


@app.on_event("startup")
async def _startup_cleanup() -> None:
    job_manager.start_cleanup_task()


@app.on_event("shutdown")
async def _shutdown_cleanup() -> None:
    await job_manager.stop_cleanup_task()


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/actions")
async def get_actions() -> List[Dict[str, object]]:
    return list(list_action_payloads())


@app.post("/api/jobs", response_model=JobResponse, status_code=201)
async def create_job(payload: JobCreateRequest) -> JobResponse:
    action = ACTIONS.get(payload.action_id)
    if not action:
        raise HTTPException(status_code=404, detail="Unknown action")

    if payload.args is not None:
        selected_args = list(payload.args)
    else:
        selected_args = action.default_arguments()
        if payload.extra_args:
            selected_args.extend(payload.extra_args)

    def remove_owner_args(args: List[str]) -> List[str]:
        cleaned: List[str] = []
        skip_next = False
        for token in args:
            if skip_next:
                skip_next = False
                continue
            if token in {"--owner", "-o"}:
                skip_next = True
                continue
            cleaned.append(token)
        return cleaned

    story_ids = [sid.strip() for sid in payload.story_ids if sid and sid.strip()]
    if story_ids:
        joined = ",".join(story_ids)
        if action.id == "pull-to-notion":
            selected_args = remove_owner_args(selected_args)
            selected_args.extend(["--story-ids", joined])
        elif action.id == "update-requirements":
            selected_args = remove_owner_args(selected_args)
            selected_args.extend(["--ids", joined])

    try:
        job = await job_manager.create_job(action, selected_args)
    except JobRejectedError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    logs, cursor = await job.collect_logs(cursor=0)
    return JobResponse(
        **job.snapshot(),
        logs=logs,
        nextCursor=cursor,
    )


@app.get("/api/jobs", response_model=List[JobSummaryResponse])
async def list_jobs() -> List[JobSummaryResponse]:
    jobs = await job_manager.list_jobs()
    return [
        JobSummaryResponse(
            id=job.id,
            actionId=job.action.id,
            title=job.action.title,
            status=job.status,
            createdAt=job.created_at,
            finishedAt=job.finished_at,
            exitCode=job.exit_code,
        )
        for job in jobs
    ]


@app.get("/api/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str, cursor: int = Query(0, ge=0)) -> JobResponse:
    try:
        job = await job_manager.get_job(job_id)
    except KeyError as exc:  # pragma: no cover - defensive path
        raise HTTPException(status_code=404, detail="Job not found") from exc

    logs, next_cursor = await job.collect_logs(cursor=cursor)
    return JobResponse(
        **job.snapshot(),
        logs=logs,
        nextCursor=next_cursor,
    )


@app.post("/api/jobs/{job_id}/terminate", response_model=JobResponse)
async def terminate_job(job_id: str, cursor: int = Query(0, ge=0)) -> JobResponse:
    try:
        job = await job_manager.terminate_job(job_id)
    except KeyError as exc:  # pragma: no cover - defensive path
        raise HTTPException(status_code=404, detail="Job not found") from exc

    logs, next_cursor = await job.collect_logs(cursor=cursor)
    return JobResponse(
        **job.snapshot(),
        logs=logs,
        nextCursor=next_cursor,
    )


def _detect_current_iteration(cfg, tapd: TAPDClient) -> tuple[Optional[dict], Optional[str], Optional[str]]:
    cache_key = cfg.tapd_workspace_id or ""
    ttl = cfg.tapd_iteration_cache_ttl_seconds or ITERATION_CACHE_TTL_SECONDS
    if ttl > 0 and cache_key:
        now = time.time()
        with _iteration_cache_lock:
            cached = _iteration_cache.get(cache_key)
            if cached and now - cached[0] <= ttl:
                cached_meta, cached_iteration_id, cached_detected = cached[1]
                return (
                    copy.deepcopy(cached_meta) if cached_meta is not None else None,
                    cached_iteration_id,
                    cached_detected,
                )
    try:
        current = tapd.get_current_iteration()
    except Exception:
        return None, None, None
    if not current:
        return None, None, None
    raw_id = (
        current.get("id")
        or current.get("iteration_id")
        or current.get("iterationid")
    )
    iteration_id = str(raw_id).strip() if raw_id else None
    if not iteration_id:
        return current, None, None
    candidates = getattr(cfg, "tapd_filter_iteration_id_keys", []) or ["iteration_id"]
    detected: Optional[str] = None
    for key in candidates:
        try:
            probe = tapd._get(  # type: ignore[attr-defined]
                tapd.stories_path,
                params={
                    "workspace_id": cfg.tapd_workspace_id,
                    key: iteration_id,
                    "page": 1,
                    "limit": 1,
                    "with_v_status": 1,
                },
            )
        except Exception:
            continue
        data = probe.get("data") if isinstance(probe, dict) else None
        ok = False
        if isinstance(data, list) and data:
            ok = True
        elif isinstance(data, dict):
            for candidate_key in ("stories", "list", "items"):
                payload = data.get(candidate_key)
                if isinstance(payload, list) and payload:
                    ok = True
                    break
        if ok:
            detected = key
            break
    if not detected and candidates:
        detected = candidates[0]
    if ttl > 0 and cache_key:
        with _iteration_cache_lock:
            _iteration_cache[cache_key] = (
                time.time(),
                (
                    copy.deepcopy(current) if current is not None else None,
                    iteration_id,
                    detected,
                ),
            )
    return current, iteration_id, detected


def _load_current_iteration_stories(
    limit: int | None = None,
    quick_shortcuts: Optional[Sequence[str]] = None,
) -> StoryCollectionResponse:
    cfg = load_config()
    if not cfg.tapd_workspace_id:
        raise RuntimeError("缺少 TAPD_WORKSPACE_ID 配置")
    tapd = TAPDClient(
        cfg.tapd_api_key or "",
        cfg.tapd_api_secret or "",
        cfg.tapd_workspace_id,
        api_user=cfg.tapd_api_user,
        api_password=cfg.tapd_api_password,
        token=cfg.tapd_token,
        api_base=cfg.tapd_api_base,
        stories_path=cfg.tapd_stories_path,
        modules_path=cfg.tapd_modules_path,
        iterations_path=cfg.tapd_iterations_path,
        story_tags_path=getattr(cfg, "tapd_story_tags_path", "/story_tags"),
        story_attachments_path=getattr(cfg, "tapd_story_attachments_path", "/story_attachments"),
        story_comments_path=getattr(cfg, "tapd_story_comments_path", "/story_comments"),
    )
    iteration_meta, iteration_id, iteration_key = _detect_current_iteration(cfg, tapd)
    filters: Dict[str, object] = {}
    if iteration_id and iteration_key:
        filters[iteration_key] = iteration_id

    raw_quick_tokens: Sequence[str] = quick_shortcuts or cfg.story_owner_quick_tokens or ()
    quick_tokens: List[str] = []
    seen_quick: Set[str] = set()
    for token in raw_quick_tokens:
        normalized = str(token).strip()
        if not normalized or normalized in seen_quick:
            continue
        seen_quick.add(normalized)
        quick_tokens.append(normalized)

    max_items = limit if limit and limit > 0 else (cfg.story_fetch_limit if cfg.story_fetch_limit > 0 else None)
    cache_ttl = cfg.tapd_story_cache_ttl_seconds or STORY_CACHE_TTL_SECONDS
    cache_capacity = cfg.tapd_story_cache_max_entries or STORY_CACHE_MAX_ENTRIES
    cache_key: Optional[str] = None
    if cache_ttl > 0:
        cache_key_components = (
            cfg.tapd_workspace_id or "",
            iteration_key or "",
            iteration_id or "",
            tuple(sorted((filters or {}).items())) if filters else (),
            tuple(quick_tokens),
            max_items or 0,
        )
        cache_key = json.dumps(cache_key_components, ensure_ascii=False, sort_keys=True)
        now = time.time()
        with _story_cache_lock:
            cached = _story_cache.get(cache_key)
            if cached and now - cached[0] <= cache_ttl:
                _story_cache.move_to_end(cache_key)
                return cached[1].copy(deep=True)

    owner_counts: Counter[str] = Counter()
    quick_story_counts: Dict[str, int] = {token: 0 for token in quick_tokens}
    quick_owner_sets: Dict[str, Set[str]] = {token: set() for token in quick_tokens}

    stories: List[StorySummaryResponse] = []
    seen: set[str] = set()
    iteration_name = str(iteration_meta.get("name") or iteration_meta.get("iteration_name") or "") if iteration_meta else None
    page_size = cfg.tapd_story_page_size if cfg.tapd_story_page_size > 0 else 200
    truncated = False
    total_count = 0
    default_owner = "未指派"

    for story in tapd.list_stories(filters=filters or None, page_size=page_size):
        sid_raw = story.get("id") or story.get("story_id")
        sid = str(sid_raw).strip() if sid_raw else ""
        if not sid or sid in seen:
            continue
        seen.add(sid)
        total_count += 1

        frontend_assignees = _collect_frontend_assignees(story)

        owners = _story_owner_tokens(story)
        if not owners:
            owners = [default_owner]
        else:
            owners = [owner.strip() or default_owner for owner in owners]
        for owner in owners:
            owner_counts[owner] += 1
        if quick_tokens:
            for token in quick_tokens:
                matched = [owner for owner in owners if token in owner]
                if matched:
                    quick_story_counts[token] += 1
                    quick_owner_sets[token].update(matched)

        status = extract_status_label(story) or str(story.get("status") or "").strip() or None
        updated = (
            story.get("modified")
            or story.get("modified_at")
            or story.get("updated_at")
            or story.get("update_time")
        )
        summary = StorySummaryResponse(
            id=sid,
            title=str(story.get("name") or story.get("title") or f"Story {sid}").strip(),
            status=status,
            owners=owners,
            iteration=iteration_name,
            updatedAt=str(updated).strip() if updated else None,
            frontend=" / ".join(frontend_assignees) if frontend_assignees else None,
            url=str(story.get("url")).strip() if story.get("url") else None,
        )
        if max_items is None or len(stories) < max_items:
            stories.append(summary)
        else:
            truncated = True

    sorted_owners = sorted(owner_counts.items(), key=lambda item: (-item[1], item[0]))
    owners_payload = [
        StoryOwnerAggregateResponse(name=name, count=count)
        for name, count in sorted_owners
    ]

    quick_payload = [
        StoryQuickOwnerAggregateResponse(
            name=token,
            owners=sorted(quick_owner_sets[token]),
            count=quick_story_counts[token],
        )
        for token in quick_tokens
    ]

    response = StoryCollectionResponse(
        stories=stories,
        total=total_count,
        owners=owners_payload,
        quickOwners=quick_payload,
        truncated=truncated,
    )

    if cache_ttl > 0 and cache_key:
        with _story_cache_lock:
            _story_cache[cache_key] = (time.time(), response.copy(deep=True))
            _story_cache.move_to_end(cache_key)
            if cache_capacity > 0:
                while len(_story_cache) > cache_capacity:
                    _story_cache.popitem(last=False)

    return response


@app.get("/api/stories", response_model=StoryCollectionResponse)
async def get_current_iteration_stories(
    limit: int = Query(0, ge=0, le=5000),
    quick: List[str] | None = Query(default=None),
) -> StoryCollectionResponse:
    try:
        effective_limit = limit if limit > 0 else None
        quick_tokens = tuple(quick) if quick else None
        return await asyncio.to_thread(_load_current_iteration_stories, effective_limit, quick_tokens)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail=str(exc)) from exc


__all__ = ["app"]
