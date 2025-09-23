from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set

DATA_DIR = Path("data")
STATE_FILE = DATA_DIR / "state.json"


def ensure_dirs() -> None:
    (DATA_DIR / "cache").mkdir(parents=True, exist_ok=True)


def load_state() -> Dict[str, Any]:
    ensure_dirs()
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state: Dict[str, Any]) -> None:
    ensure_dirs()
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)


def get_last_sync_at() -> Optional[str]:
    state = load_state()
    return state.get("last_sync_at")


def set_last_sync_at(ts: str) -> None:
    state = load_state()
    state["last_sync_at"] = ts
    save_state(state)


def _normalize_ids(ids: Iterable[str]) -> Set[str]:
    normalized: Set[str] = set()
    for raw in ids:
        try:
            text = str(raw).strip()
        except Exception:
            continue
        if text:
            normalized.add(text)
    return normalized


def get_tracked_story_ids() -> Set[str]:
    state = load_state()
    raw = state.get("tracked_story_ids", [])
    if isinstance(raw, list):
        return _normalize_ids(raw)
    if raw:
        return _normalize_ids([raw])
    return set()


def set_tracked_story_ids(ids: Iterable[str]) -> None:
    state = load_state()
    cleaned = sorted(_normalize_ids(ids))
    if cleaned:
        state["tracked_story_ids"] = cleaned
    else:
        state.pop("tracked_story_ids", None)
    save_state(state)


def add_tracked_story_ids(ids: Iterable[str]) -> None:
    new_ids = _normalize_ids(ids)
    if not new_ids:
        return
    existing = get_tracked_story_ids()
    if new_ids.issubset(existing):
        return
    set_tracked_story_ids(existing.union(new_ids))


def remove_tracked_story_ids(ids: Iterable[str]) -> None:
    drop_ids = _normalize_ids(ids)
    if not drop_ids:
        return
    existing = get_tracked_story_ids()
    remaining = existing.difference(drop_ids)
    if remaining == existing:
        return
    set_tracked_story_ids(remaining)
