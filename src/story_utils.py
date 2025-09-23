from __future__ import annotations

from typing import Any, Dict, List, Optional


def extract_story_tags(story: Dict[str, Any]) -> List[str]:
    """Return a list of tag names from a TAPD story payload."""
    candidates: List[Any] = []
    for key in (
        "tags",
        "tag_names",
        "tag",
        "tag_list",
        "labels",
        "label",
        "story_tags",
    ):
        if key in story:
            candidates.append(story[key])
    tags: List[str] = []
    for raw in candidates:
        tags.extend(_coerce_tags(raw))
    return _dedup(tags)


def extract_story_attachments(story: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return normalized attachment dicts with keys id/name/url/size/file_type/creator/created."""
    raw = None
    for key in (
        "attachments",
        "attachment_list",
        "files",
        "story_attachments",
    ):
        if key in story:
            raw = story[key]
            break
    if raw is None:
        return []
    items: List[Dict[str, Any]] = []
    for entry in _iter_collection(raw):
        normalized = _normalize_attachment(entry)
        if normalized:
            items.append(normalized)
    return items


def extract_story_comments(story: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return normalized comment dicts with keys id/author/content/created."""
    raw = None
    for key in (
        "comments",
        "comment_list",
        "story_comments",
        "comment",
    ):
        if key in story:
            raw = story[key]
            break
    if raw is None:
        return []
    comments: List[Dict[str, Any]] = []
    for entry in _iter_collection(raw):
        normalized = _normalize_comment(entry)
        if normalized:
            comments.append(normalized)
    # keep chronological order if timestamps exist
    comments.sort(key=lambda x: x.get("created") or "")
    return comments


def summarize_attachment(entry: Dict[str, Any]) -> str:
    """Create a human-readable summary string for an attachment entry."""
    name = str(entry.get("name") or "附件").strip()
    url = str(entry.get("url") or "").strip()
    meta: List[str] = []
    size = entry.get("size")
    if isinstance(size, (int, float)) and size > 0:
        meta.append(_human_readable_size(float(size)))
    file_type = str(entry.get("file_type") or "").strip()
    if file_type:
        meta.append(file_type)
    creator = str(entry.get("creator") or "").strip()
    created = str(entry.get("created") or "").strip()
    if creator and created:
        meta.append(f"{creator} · {created}")
    elif creator:
        meta.append(creator)
    elif created:
        meta.append(created)
    label = name or url or "附件"
    if url:
        label = f"{label} → {url}"
    if meta:
        label = f"{label} ({', '.join(meta)})"
    return label.strip()


def summarize_comment(entry: Dict[str, Any]) -> str:
    """Create a human-readable summary string for a comment entry."""
    author = str(entry.get("author") or "匿名").strip()
    created = str(entry.get("created") or "").strip()
    header = author
    if created:
        header = f"{author} · {created}"
    content = str(entry.get("content") or "").strip().replace("\r", "")
    content = " ".join(content.split())
    return f"{header}: {content}" if content else header


# --- Helpers ---------------------------------------------------------------

def _coerce_tags(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [seg.strip() for seg in raw.replace(";", ",").split(",") if seg.strip()]
    if isinstance(raw, (list, tuple, set)):
        items: List[str] = []
        for item in raw:
            if item is None:
                continue
            if isinstance(item, str):
                items.extend(_coerce_tags(item))
            elif isinstance(item, (int, float)):
                items.append(str(item))
            elif isinstance(item, dict):
                val = (
                    item.get("name")
                    or item.get("tag")
                    or item.get("tag_name")
                    or item.get("label")
                    or item.get("value")
                )
                if val:
                    items.extend(_coerce_tags(val))
        return items
    if isinstance(raw, dict):
        return _coerce_tags(
            raw.get("name")
            or raw.get("tag")
            or raw.get("value")
            or raw.get("label")
            or raw.get("tags")
        )
    try:
        return [str(raw).strip()]
    except Exception:
        return []


def _iter_collection(raw: Any) -> List[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, tuple):
        return list(raw)
    if isinstance(raw, dict):
        # Some APIs wrap payload in capitalized keys (Attachment/Comment)
        for key in ("Attachment", "StoryAttachment", "Comment", "StoryComment"):
            if key in raw and isinstance(raw[key], (list, tuple)):
                return list(raw[key])
            if key in raw and isinstance(raw[key], dict):
                return [raw[key]]
        return [raw]
    if isinstance(raw, str):
        # crude split for textual attachment lists
        lines = [line.strip() for line in raw.replace(";", "\n").splitlines() if line.strip()]
        return lines
    return [raw]


def _normalize_attachment(entry: Any) -> Optional[Dict[str, Any]]:
    if entry is None:
        return None
    if isinstance(entry, str):
        return {
            "id": "",
            "name": entry.strip(),
            "url": entry.strip(),
            "size": None,
            "file_type": "",
            "creator": "",
            "created": "",
        }
    if not isinstance(entry, dict):
        return None
    name = _first_non_empty(entry, ("name", "title", "filename", "file_name", "attachment_name"))
    url = _first_non_empty(entry, ("url", "download_url", "preview_url", "attachment_url", "file_url"))
    if not (name or url):
        return None
    return {
        "id": _first_non_empty(entry, ("id", "attachment_id", "file_id", "document_id", "aid")) or "",
        "name": name or url or "附件",
        "url": url or "",
        "size": _to_int(entry.get("size") or entry.get("file_size") or entry.get("attachment_size") or entry.get("filesize")),
        "file_type": _first_non_empty(entry, ("filetype", "file_type", "type", "mime_type", "extension")) or "",
        "creator": _first_non_empty(entry, ("creator", "owner", "author", "uploader", "created_by")) or "",
        "created": _first_non_empty(entry, ("created", "created_at", "create_time", "upload_time", "uploaded", "createdon", "create_at")) or "",
    }


def _normalize_comment(entry: Any) -> Optional[Dict[str, Any]]:
    if entry is None:
        return None
    if isinstance(entry, str):
        text = entry.strip()
        if not text:
            return None
        return {"id": "", "author": "", "content": text, "created": ""}
    if not isinstance(entry, dict):
        return None
    content = entry.get("content") or entry.get("comment") or entry.get("text") or entry.get("detail") or entry.get("body")
    if isinstance(content, dict):
        content = content.get("content") or content.get("text")
    if isinstance(content, (list, tuple)):
        content = "\n".join(str(v).strip() for v in content if str(v).strip())
    content_str = str(content).strip() if content else ""
    author = _first_non_empty(entry, ("author", "creator", "owner", "commenter", "user", "created_by")) or ""
    created = _first_non_empty(entry, ("created", "created_at", "create_time", "added_time", "createdon", "time", "create_at")) or ""
    if not content_str and not author:
        return None
    return {
        "id": _first_non_empty(entry, ("id", "comment_id", "cid")) or "",
        "author": author,
        "content": content_str,
        "created": created,
    }


def _first_non_empty(entry: Dict[str, Any], keys: tuple[str, ...]) -> Optional[str]:
    for key in keys:
        if key not in entry:
            continue
        val = entry[key]
        if val is None:
            continue
        if isinstance(val, str):
            stripped = val.strip()
            if stripped:
                return stripped
        elif isinstance(val, (int, float)):
            return str(val)
        elif isinstance(val, (list, tuple)):
            for sub in val:
                res = _first_non_empty({"tmp": sub}, ("tmp",))
                if res:
                    return res
        elif isinstance(val, dict):
            res = _first_non_empty(val, ("name", "value", "text"))
            if res:
                return res
    return None


def _to_int(value: Any) -> Optional[int]:
    if value in (None, "", "null", "NULL"):
        return None
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


def _human_readable_size(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    if idx == 0:
        return f"{int(size)}{units[idx]}"
    return f"{size:.1f}{units[idx]}"


def _dedup(items: List[str]) -> List[str]:
    seen: set[str] = set()
    ordered: List[str] = []
    for item in items:
        if not item:
            continue
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered
