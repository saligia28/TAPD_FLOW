from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .client import TAPDClient


def fetch_story_attachments(client: "TAPDClient", story_id: str) -> List[Dict[str, Any]]:
    """Fetch story attachments using the legacy TAPD endpoints."""
    attachments: List[Dict[str, Any]] = []
    params_candidates = _attachment_param_candidates(client, story_id)
    for path in client._candidate_paths(client.story_attachments_path, ("story_attachments", "attachments")):
        res = client._fetch_with_variants(path, params_candidates)
        items = client._extract_payload_list(
            res,
            ("Attachment", "StoryAttachment", "story_attachment", "attachment"),
        )
        for item in items:
            if not isinstance(item, dict):
                continue
            att_id = client._first_str(item, ("id", "attachment_id", "file_id", "document_id", "aid")) or ""
            name = client._first_str(item, ("name", "title", "filename", "file_name", "attachment_name")) or ""
            url = client._first_str(item, ("url", "download_url", "preview_url", "attachment_url", "file_url")) or ""
            size_raw = (
                item.get("size")
                or item.get("file_size")
                or item.get("attachment_size")
                or item.get("filesize")
            )
            size = client._to_int(size_raw)
            file_type = client._first_str(
                item,
                ("filetype", "file_type", "type", "mime_type", "extension"),
            ) or ""
            creator = client._first_str(
                item,
                ("creator", "owner", "author", "uploader", "create_user", "created_by"),
            ) or ""
            created = client._first_str(
                item,
                ("created", "created_at", "create_time", "uploaded", "upload_time", "create_at", "createdon"),
            ) or ""
            if not (name or url):
                continue
            attachments.append(
                {
                    "id": att_id,
                    "name": name or f"附件{len(attachments) + 1}",
                    "url": url,
                    "size": size,
                    "file_type": file_type,
                    "creator": creator,
                    "created": created,
                }
            )
        if attachments:
            break
    return attachments


def fetch_story_comments(client: "TAPDClient", story_id: str) -> List[Dict[str, Any]]:
    """Fetch story comments using the legacy TAPD endpoints."""
    comments: List[Dict[str, Any]] = []
    params_candidates = _comment_param_candidates(client, story_id)
    for path in client._candidate_paths(client.story_comments_path, ("story_comments", "comments")):
        res = client._fetch_with_variants(path, params_candidates)
        items = client._extract_payload_list(
            res,
            ("Comment", "StoryComment", "story_comment", "comment"),
        )
        for item in items:
            if not isinstance(item, dict):
                continue
            content_val: Any = (
                item.get("content")
                or item.get("comment")
                or item.get("text")
                or item.get("detail")
                or item.get("body")
                or item.get("description")
            )
            if isinstance(content_val, dict):
                inner = content_val.get("content") or content_val.get("text")
                if inner:
                    content_val = inner
            if isinstance(content_val, (list, tuple)):
                content_val = "\n".join(str(v).strip() for v in content_val if str(v).strip())
            content = str(content_val).strip() if content_val is not None else ""
            author = client._first_str(
                item,
                ("author", "creator", "owner", "commenter", "user", "created_by"),
            ) or ""
            created = client._first_str(
                item,
                ("created", "created_at", "create_time", "added_time", "create_at", "createdon", "time"),
            ) or ""
            comment_id = client._first_str(item, ("id", "comment_id", "cid")) or ""
            if not content and not author:
                continue
            comments.append(
                {
                    "id": comment_id,
                    "author": author,
                    "content": content,
                    "created": created,
                }
            )
        if comments:
            break
    comments.sort(key=lambda x: x.get("created") or "")
    return comments


def _attachment_param_candidates(client: "TAPDClient", story_id: str) -> List[Dict[str, Any]]:
    base = {"workspace_id": client.workspace_id, "limit": 200}
    id_keys = ("entry_id", "story_id", "id", "resource_id", "workitem_id")
    type_values = ("story", "stories", "Story", None)
    candidates: List[Dict[str, Any]] = []
    for key in id_keys:
        for type_val in type_values:
            params = dict(base)
            params[key] = story_id
            if type_val:
                params["type"] = type_val
            candidates.append(params)
    return candidates


def _comment_param_candidates(client: "TAPDClient", story_id: str) -> List[Dict[str, Any]]:
    base = {"workspace_id": client.workspace_id, "limit": 200}
    id_keys = ("entry_id", "story_id", "id", "resource_id", "workitem_id")
    entry_types = ("stories", "story", "Story")
    candidates: List[Dict[str, Any]] = []
    for key in id_keys:
        for entry_type in entry_types:
            params = dict(base)
            params[key] = story_id
            params["entry_type"] = entry_type
            candidates.append(params)
    return candidates
