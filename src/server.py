from __future__ import annotations

import asyncio
import os
import shlex
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, MutableMapping, Optional, Sequence

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from config import load_config
from mapper import extract_status_label
from sync import _story_owner_tokens
from tapd_client import TAPDClient
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
MAX_LOG_LINES = 2000


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


class Job:
    def __init__(self, action: ActionDefinition, args: Sequence[str] | None = None) -> None:
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
        self._log: List[LogEntry] = []
        self._seq = 0
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
            self._seq += 1
            entry = LogEntry(
                seq=self._seq,
                timestamp=datetime.now(timezone.utc),
                stream=stream,
                text=text,
            )
            self._log.append(entry)
            if len(self._log) > MAX_LOG_LINES:
                self._log = self._log[-MAX_LOG_LINES:]

    async def collect_logs(self, cursor: int) -> tuple[List[Dict[str, object]], int]:
        async with self._lock:
            if cursor <= 0:
                items = [entry.to_payload() for entry in self._log]
            else:
                items = [entry.to_payload() for entry in self._log if entry.seq > cursor]
            next_cursor = self._seq
        return items, next_cursor

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
    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = asyncio.Lock()

    async def create_job(self, action: ActionDefinition, args: Sequence[str] | None = None) -> Job:
        job = Job(action, args)
        async with self._lock:
            self._jobs[job.id] = job
        job.set_task(asyncio.create_task(self._run_job(job)))
        return job

    async def _run_job(self, job: Job) -> None:
        await job.append_log("system", f"Starting command: {job.display_command()}")
        job.started_at = datetime.now(timezone.utc)

        env = os.environ.copy()
        env.update(job.action.env)

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

    async def list_jobs(self) -> List[Job]:
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


job_manager = JobManager()
app = FastAPI(title="TAPD Workflow Automation")


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

    job = await job_manager.create_job(action, selected_args)
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
    return current, iteration_id, detected


def _load_current_iteration_stories() -> List[StorySummaryResponse]:
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

    stories: List[StorySummaryResponse] = []
    seen: set[str] = set()
    iteration_name = str(iteration_meta.get("name") or iteration_meta.get("iteration_name") or "") if iteration_meta else None
    for story in tapd.list_stories(filters=filters or None):
        sid_raw = story.get("id") or story.get("story_id")
        sid = str(sid_raw).strip() if sid_raw else ""
        if not sid or sid in seen:
            continue
        seen.add(sid)
        owners = _story_owner_tokens(story)
        status = extract_status_label(story) or str(story.get("status") or "").strip() or None
        updated = (
            story.get("modified")
            or story.get("modified_at")
            or story.get("updated_at")
            or story.get("update_time")
        )
        stories.append(
            StorySummaryResponse(
                id=sid,
                title=str(story.get("name") or story.get("title") or f"Story {sid}").strip(),
                status=status,
                owners=owners,
                iteration=iteration_name,
                updatedAt=str(updated).strip() if updated else None,
            )
        )
        if len(stories) >= 400:
            break
    return stories


@app.get("/api/stories", response_model=List[StorySummaryResponse])
async def get_current_iteration_stories() -> List[StorySummaryResponse]:
    try:
        return await asyncio.to_thread(_load_current_iteration_stories)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail=str(exc)) from exc


__all__ = ["app"]
