"""Helpers for detecting frontend owners and filtering stories."""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Sequence, Set

__all__ = [
    "collect_frontend_assignees",
    "story_owner_tokens",
    "story_matches_owner",
]

FRONTEND_KEY_TOKENS = ("前端", "frontend")
DEFAULT_FRONTEND_FIELD_KEYS = ("custom_field_four",)
FRONTEND_LABEL_SUFFIXES = ("_label", "_name", "_display", "_display_name", "_title")
FRONTEND_LABEL_KEYS = (
    "name",
    "label",
    "title",
    "field",
    "field_name",
    "field_label",
    "display_name",
    "key",
)
FRONTEND_VALUE_KEYS = (
    "value",
    "values",
    "text",
    "content",
    "display_value",
    "display",
    "user",
    "assignee",
    "owner",
)
OWNER_KEY_CANDIDATES = (
    "owner",
    "assignee",
    "current_owner",
    "owners",
    "负责人",
    "处理人",
    "当前处理人",
    "处理人员",
    "经办人",
    "执行人",
)


def _normalize_match_str(value: str) -> str:
    return "".join(ch for ch in value if ch.isalnum())


def _flatten_strings(value: Any, depth: int = 0, limit: int = 4) -> List[str]:
    if depth > limit:
        return []
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, (int, float, bool)):
        return [str(value)]
    if isinstance(value, (list, tuple, set)):
        out: List[str] = []
        for item in value:
            out.extend(_flatten_strings(item, depth + 1, limit))
        return out
    if isinstance(value, dict):
        out: List[str] = []
        for val in value.values():
            out.extend(_flatten_strings(val, depth + 1, limit))
        return out
    try:
        text = str(value).strip()
    except Exception:
        return []
    return [text] if text else []


def _dedup_preserve_order(items: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for item in items:
        if item is None:
            continue
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _key_matches_frontend(name: str) -> bool:
    if not name:
        return False
    low = name.lower()
    for token in FRONTEND_KEY_TOKENS:
        if token == token.lower():
            if token in low:
                return True
        else:
            if token in name:
                return True
    return False


def _extract_labeled_frontend_values(entry: Dict[str, Any]) -> List[str]:
    for label_key in FRONTEND_LABEL_KEYS:
        label = entry.get(label_key)
        if isinstance(label, str) and _key_matches_frontend(label):
            values: List[str] = []
            for value_key in FRONTEND_VALUE_KEYS:
                if value_key in entry:
                    values.extend(_flatten_strings(entry[value_key]))
            if values:
                return values
            fallback: List[str] = []
            for key, val in entry.items():
                if key in FRONTEND_LABEL_KEYS or key in FRONTEND_VALUE_KEYS:
                    continue
                fallback.extend(_flatten_strings(val))
            return fallback
    return []


def _parse_csv_env(name: str, default: Sequence[str] = ()) -> List[str]:
    raw = os.getenv(name)
    if raw is None:
        return list(default)
    cleaned = raw.strip()
    if not cleaned:
        return list(default)
    if cleaned.lower() in {"none", "null"}:
        return []
    return [segment.strip() for segment in raw.split(",") if segment.strip()]


def _frontend_config() -> tuple[List[str], List[str]]:
    keys = _parse_csv_env("TAPD_FRONTEND_FIELD_KEYS", DEFAULT_FRONTEND_FIELD_KEYS)
    labels = _parse_csv_env("TAPD_FRONTEND_FIELD_LABELS", ("前端",))
    return keys, labels


def _collect_from_mapping(mapping: Dict[str, Any], keys: Set[str], labels: Set[str]) -> List[str]:
    collected: List[str] = []
    if not isinstance(mapping, dict):
        return collected
    for key in keys:
        if key in mapping:
            collected.extend(_flatten_strings(mapping[key]))
    if labels:
        for name, label in mapping.items():
            if not isinstance(name, str):
                continue
            label_text = str(label).strip()
            if not label_text or label_text not in labels:
                continue
            base = name
            for suffix in FRONTEND_LABEL_SUFFIXES:
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
                    break
            for candidate in (
                base,
                f"{base}_value",
                f"{base}_values",
                f"{base}_text",
                f"{base}_content",
                f"{base}_display",
                f"{base}_display_value",
                f"{base}_assignee",
                f"{base}_user",
            ):
                if candidate in mapping:
                    collected.extend(_flatten_strings(mapping[candidate]))
                    break
    return collected


def collect_frontend_assignees(story: Dict[str, Any]) -> List[str]:
    values: List[str] = []
    config_keys, config_labels = _frontend_config()
    key_set = {key for key in config_keys if key}
    label_set = {label for label in config_labels if label}
    if key_set or label_set:
        values.extend(_collect_from_mapping(story, key_set, label_set))
        custom_fields = story.get("custom_fields")
        if isinstance(custom_fields, dict):
            values.extend(_collect_from_mapping(custom_fields, key_set, label_set))
        elif isinstance(custom_fields, list):
            for entry in custom_fields:
                if not isinstance(entry, dict):
                    continue
                entry_keys = [
                    str(entry.get(candidate)).strip()
                    for candidate in ("field", "field_name", "field_key", "key", "name")
                    if entry.get(candidate)
                ]
                if any(candidate_key in key_set for candidate_key in entry_keys):
                    for value_key in FRONTEND_VALUE_KEYS:
                        if value_key in entry:
                            values.extend(_flatten_strings(entry[value_key]))
                            break
                    continue
                label_text = str(
                    entry.get("label")
                    or entry.get("display_name")
                    or entry.get("field_label")
                    or entry.get("title")
                    or ""
                ).strip()
                if label_text and label_text in label_set:
                    for value_key in FRONTEND_VALUE_KEYS:
                        if value_key in entry:
                            values.extend(_flatten_strings(entry[value_key]))
                            break
    seen: Set[int] = set()
    stack: List[Any] = [story]
    while stack:
        current = stack.pop()
        obj_id = id(current)
        if obj_id in seen:
            continue
        seen.add(obj_id)
        if isinstance(current, dict):
            labeled = _extract_labeled_frontend_values(current)
            if labeled:
                values.extend(labeled)
            for key, val in current.items():
                if isinstance(key, str) and _key_matches_frontend(key):
                    values.extend(_flatten_strings(val))
                if isinstance(val, (dict, list, tuple, set)):
                    stack.append(val)
        elif isinstance(current, (list, tuple, set)):
            for item in current:
                if isinstance(item, (dict, list, tuple, set)):
                    stack.append(item)
    return _dedup_preserve_order(values)


def story_owner_tokens(story: Dict[str, Any]) -> List[str]:
    tokens: List[str] = []
    for key in OWNER_KEY_CANDIDATES:
        tokens.extend(_flatten_strings(story.get(key)))
    tokens.extend(collect_frontend_assignees(story))
    return _dedup_preserve_order(tokens)


def story_matches_owner(story: Dict[str, Any], substrings: List[str]) -> bool:
    if not substrings:
        return True
    candidates = story_owner_tokens(story)
    if not candidates:
        return False
    hay_raw = " ".join(candidates)
    normalized_tokens = [_normalize_match_str(token) for token in candidates]
    hay_norm = " ".join([token for token in normalized_tokens if token])
    for sub in substrings:
        if not sub:
            continue
        if sub in hay_raw:
            return True
        norm_sub = _normalize_match_str(sub)
        if norm_sub and norm_sub in hay_norm:
            return True
    return False
