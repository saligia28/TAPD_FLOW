from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

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
