"""Utility helpers shared across sync flows."""

from __future__ import annotations

from typing import Any, Dict, Optional

from core.config import Config
from integrations.tapd.client import TAPDClient

__all__ = [
    "story_tapd_id",
    "enrich_story_with_extras",
    "unwrap_story_payload",
]


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
