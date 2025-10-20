from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import threading
import time
from collections import Counter, OrderedDict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set

SRC_ROOT = Path(__file__).resolve().parents[2]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from core.config import load_config
from integrations.notion import extract_status_label
from integrations.tapd import TAPDClient
from services.sync import collect_frontend_assignees, story_owner_tokens
from .actions import ACTIONS, list_action_payloads
from .jobs import JobManager, JobRejectedError, JobStatus
from .schemas import (
    JobCreateRequest,
    JobResponse,
    JobSummaryResponse,
    StoryCollectionResponse,
    StoryOwnerAggregateResponse,
    StoryQuickOwnerAggregateResponse,
    StorySummaryResponse,
)
REPO_ROOT = SRC_ROOT.parent


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


job_manager = JobManager(
    retention_seconds=JOB_RETENTION_SECONDS,
    cleanup_interval=JOB_CLEANUP_INTERVAL_SECONDS,
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

    if action.id == "debug-notion":
        selected_args = remove_owner_args(selected_args)

    story_ids = [sid.strip() for sid in payload.story_ids if sid and sid.strip()]
    if story_ids:
        joined = ",".join(story_ids)
        if action.id == "pull-to-notion":
            selected_args = remove_owner_args(selected_args)
            selected_args.extend(["--story-ids", joined])
        elif action.id == "update-requirements":
            selected_args = remove_owner_args(selected_args)
            selected_args.extend(["--ids", joined])
        elif action.id == "debug-notion":
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

        frontend_assignees = collect_frontend_assignees(story)

        owners = story_owner_tokens(story)
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
