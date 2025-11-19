"""Utility helpers shared across sync flows."""

from __future__ import annotations

import sys
import time
from typing import Any, Dict, Optional

from core.config import Config
from integrations.tapd.client import TAPDClient

__all__ = [
    "story_tapd_id",
    "enrich_story_with_extras",
    "unwrap_story_payload",
    "ProgressReporter",
]


class ProgressReporter:
    """Lightweight progress logger for sync operations.

    Only outputs when enabled=True to avoid noise in default mode.
    Provides stage-based logging for fetch, analyze, and notion phases.
    """

    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self._stage_start_time: Optional[float] = None
        self._stage_name: Optional[str] = None

    def stage(
        self,
        name: str,
        idx: Optional[int] = None,
        total: Optional[int] = None,
        tapd_id: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Log progress for a specific stage.

        Args:
            name: Stage name (e.g., 'fetch', 'analyze', 'notion')
            idx: Current item index (1-based)
            total: Total items count (if known)
            tapd_id: Story ID being processed
            **kwargs: Additional context to include in log
        """
        if not self.enabled:
            return

        parts = [f"stage={name}"]

        if idx is not None:
            if total is not None:
                parts.append(f"idx={idx}/{total}")
            else:
                parts.append(f"idx={idx}")

        if tapd_id:
            parts.append(f"tapd_id={tapd_id}")

        for key, value in kwargs.items():
            if value is not None:
                parts.append(f"{key}={value}")

        print(f"[sync] progress {' '.join(parts)}")
        sys.stdout.flush()

    def stage_start(self, name: str, **kwargs: Any) -> None:
        """Mark the start of a stage."""
        if not self.enabled:
            return
        self._stage_name = name
        self._stage_start_time = time.time()
        self.stage(name, action="start", **kwargs)

    def stage_end(self, name: Optional[str] = None, **kwargs: Any) -> None:
        """Mark the end of a stage and log duration."""
        if not self.enabled:
            return
        stage_name = name or self._stage_name
        if not stage_name:
            return

        duration = None
        if self._stage_start_time is not None:
            duration = time.time() - self._stage_start_time
            self._stage_start_time = None

        if duration is not None:
            self.stage(stage_name, action="end", duration_s=f"{duration:.2f}", **kwargs)
        else:
            self.stage(stage_name, action="end", **kwargs)

        self._stage_name = None


def story_tapd_id(story: Dict[str, Any]) -> str:
    raw_id = story.get("id") or story.get("story_id") or story.get("tapd_id")
    if raw_id is None:
        return ""
    sid = str(raw_id).strip()
    return sid


def enrich_story_with_extras(
    tapd: TAPDClient,
    cfg: Config,
    story: Dict[str, Any],
    *,
    cache: Optional[Dict[str, Dict[str, Any]]] = None,
    ctx: str = "sync",
) -> None:
    fetch_tags = getattr(cfg, "tapd_fetch_tags", False)
    fetch_attachments = getattr(cfg, "tapd_fetch_attachments", False)
    fetch_comments = getattr(cfg, "tapd_fetch_comments", False)
    if not (fetch_tags or fetch_attachments or fetch_comments):
        return
    raw_id = story.get("id") or story.get("story_id") or story.get("tapd_id")
    sid = str(raw_id).strip() if raw_id is not None else ""
    if not sid:
        return
    extras: Dict[str, Any]
    if cache is not None and sid in cache:
        extras = cache[sid]
    else:
        extras = tapd.fetch_story_extras(
            sid,
            include_tags=fetch_tags,
            include_attachments=fetch_attachments,
            include_comments=fetch_comments,
        )
        if cache is not None:
            cache[sid] = extras
    errors = extras.pop("_errors", None)
    if errors:
        msg = "; ".join(str(e) for e in errors)
        print(f"[{ctx}] extras warning id={sid}: {msg}")
    for key, value in extras.items():
        if key.startswith("_"):
            continue
        # Assign sanitized extras; copy lists/dicts to avoid accidental mutation
        if isinstance(value, list):
            story[key] = [dict(item) if isinstance(item, dict) else item for item in value]
        elif isinstance(value, dict):
            story[key] = dict(value)
        else:
            story[key] = value


def unwrap_story_payload(obj: dict) -> Optional[dict]:
    """Unwrap TAPD get_story/list payloads to a raw story dict."""
    if not isinstance(obj, dict):
        return None
    if "Story" in obj and isinstance(obj["Story"], dict):
        return obj["Story"]
    if "data" in obj:
        data = obj.get("data")
        if isinstance(data, dict):
            if "Story" in data and isinstance(data["Story"], dict):
                return data["Story"]
            return data if "id" in data else None
        if isinstance(data, list) and data:
            it = data[0]
            if isinstance(it, dict):
                return it.get("Story") if "Story" in it else it
        return None
    return obj if "id" in obj else None
