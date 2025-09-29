from __future__ import annotations
from typing import Any, Dict

# Direct status mapping provided by TAPD workflow
STATUS_VALUE_MAP: Dict[str, str] = {
    "planning": "规划中",
    "status_8": "待开发",
    "status_2": "开发中",
    "status_4": "联调中",
    "status_5": "联调完成",
    "status_12": "已提测",
    "status_6": "测试中",
    "status_7": "测试完成",
    "status_10": "产品验收",
    "status_3": "已完成",
    "status_11": "关闭",
}


STATUS_LABEL_FIELDS = (
    "v_status",
    "status_label",
    "status_name",
    "status_text",
    "workflow_status_name",
    "workflow_status_label",
    "status_stage_label",
)

STATUS_CODE_FIELDS = (
    "status_id",
    "status_key",
    "status_value",
    "workflow_status_id",
    "workflow_status_key",
    "status_stage",
    "status_stage_id",
)


def _normalize_status_key(raw: Any) -> str:
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    low = text.lower()
    if low in STATUS_VALUE_MAP:
        return low
    for prefix in ("status_", "status-", "status "):
        if low.startswith(prefix):
            tail = low[len(prefix):].strip()
            tail = tail.replace("-", "_").replace(" ", "")
            if tail.isdigit():
                tail = str(int(tail))
                return f"status_{tail}"
            return f"status_{tail}" if tail else ""
    if low.isdigit():
        return f"status_{int(low)}"
    return low


def _lookup_status(value: Any) -> str:
    key = _normalize_status_key(value)
    if key in STATUS_VALUE_MAP:
        return STATUS_VALUE_MAP[key]
    text = str(value).strip() if value is not None else ""
    if text and text in STATUS_VALUE_MAP.values():
        return text
    return ""


def normalize_status(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return s
    mapped = _lookup_status(s)
    return mapped or s


def extract_status_label(story: Dict[str, Any]) -> str:
    for key in STATUS_LABEL_FIELDS:
        if key not in story:
            continue
        val = story.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if not text:
            continue
        mapped = _lookup_status(text)
        return mapped or text
    for key in STATUS_CODE_FIELDS:
        if key not in story:
            continue
        label = _lookup_status(story.get(key))
        if label:
            return label
    for key in ("status", "current_status"):
        raw = story.get(key)
        if raw is None:
            continue
        label = normalize_status(raw)
        if label:
            return label
    return ""


def map_story_to_notion_properties(story: Dict[str, Any]) -> Dict[str, Any]:
    """Map TAPD story fields to Notion properties. Skeleton version uses generic keys."""
    title = story.get("name") or story.get("title") or f"TAPD {story.get('id', '')}"
    tapd_id = str(story.get("id", ""))
    status = extract_status_label(story)
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
