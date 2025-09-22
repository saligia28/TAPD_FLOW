from __future__ import annotations
from typing import Any, Dict
import os
import re

# Example mappings; real mappings should be configurable
# Common textual status to Chinese
STATUS_MAP = {
    # English/common
    "new": "新建",
    "open": "新建",
    "todo": "新建",
    "in_progress": "进行中",
    "doing": "进行中",
    "wip": "进行中",
    "resolved": "已完成",
    "done": "已完成",
    "closed": "已关闭",
    "reject": "已拒绝",
    "rejected": "已拒绝",
    "pause": "暂停",
    "paused": "暂停",
    "verify": "待验证",
    "review": "待验证",
    # Chinese direct passthrough below will handle native labels
}

# Default numeric status mapping (override via env STATUS_NUM_MAP="1=新建,2=进行中,...")
DEFAULT_STATUS_NUM_MAP = {
    "1": "新建",
    "2": "进行中",
    "3": "已完成",
    "4": "已关闭",
    "5": "暂停",
    "6": "已拒绝",
    "7": "待验证",
}


def _parse_status_num_map() -> Dict[str, str]:
    raw = os.getenv("STATUS_NUM_MAP", "").strip()
    if not raw:
        return DEFAULT_STATUS_NUM_MAP
    m: Dict[str, str] = {}
    for part in raw.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k and v:
                m[k] = v
    # fallback defaults if incomplete
    return {**DEFAULT_STATUS_NUM_MAP, **m}


def normalize_status(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return s
    low = s.lower()
    # textual map
    if low in STATUS_MAP:
        return STATUS_MAP[low]
    # chinese labels - return as-is
    for zh in ("新建", "进行中", "已完成", "已关闭", "暂停", "已拒绝", "待验证"):
        if zh in s:
            return zh
    # status-<num> or <num>
    m = re.match(r"status-?(\d+)$", low)
    if m:
        num = m.group(1)
        return _parse_status_num_map().get(num, f"状态{num}")
    if low.isdigit():
        return _parse_status_num_map().get(low, f"状态{low}")
    # default: return as-is
    return s


def map_story_to_notion_properties(story: Dict[str, Any]) -> Dict[str, Any]:
    """Map TAPD story fields to Notion properties. Skeleton version uses generic keys."""
    title = story.get("name") or story.get("title") or f"TAPD {story.get('id', '')}"
    tapd_id = str(story.get("id", ""))
    status = normalize_status(story.get("status"))
    priority = story.get("priority")
    assignees = story.get("owner") or story.get("assignee")

    props = {
        "Name": {"title": [{"text": {"content": title}}]},
        "TAPD_ID": {"rich_text": [{"text": {"content": tapd_id}}]},
        "状态": {"select": {"name": status}} if status else None,
        "优先级": {"select": {"name": str(priority)}} if priority else None,
        "负责人": {"multi_select": [{"name": a} for a in _ensure_list(assignees)]} if assignees else None,
    }
    # drop None
    return {k: v for k, v in props.items() if v is not None}


def _ensure_list(x):
    if x is None:
        return []
    if isinstance(x, (list, tuple)):
        return list(x)
    return [str(x)]
