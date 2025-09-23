from __future__ import annotations
from typing import Any, Dict, Iterable, List, Optional, Tuple
from datetime import datetime, timezone
from urllib.parse import urljoin
import time
import random

import requests
from requests import exceptions as req_exc


class TAPDClient:
    """Minimal TAPD API client.

    Supports per-user Basic Auth (api_user/api_password) as described in
    https://o.tapd.cn/document/api-doc/API文档/API配置指引.html
    and keeps placeholders for AppKey/Secret.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        workspace_id: str,
        *,
        api_user: Optional[str] = None,
        api_password: Optional[str] = None,
        token: Optional[str] = None,
        api_base: str = "https://api.tapd.cn",
        stories_path: str = "/stories",
        modules_path: str = "/modules",
        iterations_path: str = "/iterations",
        story_tags_path: str = "/story_tags",
        story_attachments_path: str = "/story_attachments",
        story_comments_path: str = "/story_comments",
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.workspace_id = workspace_id
        self.api_user = api_user
        self.api_password = api_password
        self.token = token
        self.api_base = api_base.rstrip("/") + "/"
        self.stories_path = stories_path.lstrip("/")
        self.modules_path = modules_path.lstrip("/")
        self.iterations_path = iterations_path.lstrip("/")
        self.story_tags_path = story_tags_path.lstrip("/") if story_tags_path else None
        self.story_attachments_path = story_attachments_path.lstrip("/") if story_attachments_path else None
        self.story_comments_path = story_comments_path.lstrip("/") if story_comments_path else None

    # --- HTTP helpers -----------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "tapd-sync-skeleton/0.1",
        }
        if self.token:
            # If token-based auth is needed later, set header here
            headers.setdefault("Authorization", f"Bearer {self.token}")
        return headers

    def _auth(self):
        # Prefer Basic Auth if api_user/password provided
        if self.api_user and self.api_password:
            return (self.api_user, self.api_password)
        return None

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """GET with light retry on transient network/5xx/429/SSL errors."""
        url = urljoin(self.api_base, path)
        max_tries = 5
        for attempt in range(1, max_tries + 1):
            try:
                r = requests.get(
                    url,
                    params=params or {},
                    headers=self._headers(),
                    auth=self._auth(),
                    timeout=20,
                )
                # Retry on 429/5xx
                if r.status_code in (429, 502, 503, 504) or 500 <= r.status_code < 600:
                    raise req_exc.HTTPError(f"HTTP {r.status_code}", response=r)
                r.raise_for_status()
                try:
                    return r.json()  # TAPD typically returns {status, data, info}
                except Exception:
                    return {"raw": r.text}
            except (req_exc.SSLError, req_exc.ConnectionError, req_exc.Timeout, req_exc.HTTPError) as e:
                # If non-retriable 4xx (except 429), re-raise immediately
                resp = getattr(e, "response", None)
                if resp is not None and 400 <= resp.status_code < 500 and resp.status_code != 429:
                    raise
                if attempt >= max_tries:
                    raise
                # exponential backoff with jitter
                sleep = (2 ** (attempt - 1)) * 0.8 + random.uniform(0, 0.4)
                time.sleep(sleep)

    # --- Resources ---------------------------------------------------------
    def test_auth(self) -> Dict[str, Any]:
        return self._get("quickstart/testauth")

    def list_modules(self) -> Iterable[Dict[str, Any]]:
        """Yield module dicts via /modules endpoint.

        Normalizes payload by unwrapping {'Module': {...}} if present.
        """
        params = {"workspace_id": self.workspace_id}
        res = self._get(self.modules_path, params=params)
        data = res.get("data") if isinstance(res, dict) else None
        if not data:
            return []
        for it in data:
            mod = it.get("Module") if isinstance(it, dict) and "Module" in it else it
            if isinstance(mod, dict):
                yield mod

    def list_iterations(self) -> Iterable[Dict[str, Any]]:
        """Yield iteration/sprint dicts via /iterations endpoint.

        Normalizes payload by unwrapping {'Iteration': {...}} if present.
        """
        params = {"workspace_id": self.workspace_id}
        res = self._get(self.iterations_path, params=params)
        data = res.get("data") if isinstance(res, dict) else None
        if not data:
            return []
        for it in data:
            itr = it.get("Iteration") if isinstance(it, dict) and "Iteration" in it else it
            if isinstance(itr, dict):
                yield itr

    def get_current_iteration(self) -> Optional[Dict[str, Any]]:
        """Best-effort pick a 'current' iteration.

        Priority:
        1) item with truthy field among ['is_current', 'current', 'active']
        2) item whose time window covers 'now' using fields in common names
           e.g., start_date/start_time/begin_date and end_date/end_time/finish_date
        Returns None if no iterations available.
        """
        # collect candidates
        iters = list(self.list_iterations())
        if not iters:
            return None
        # 1) explicit flag
        for it in iters:
            for key in ("is_current", "current", "active"):
                val = it.get(key)
                if isinstance(val, (bool, int)) and bool(val):
                    return it
                if isinstance(val, str) and val.lower() in {"1", "true", "yes", "on"}:
                    return it
            # status hints
            st = str(it.get('status', '')).lower()
            if st in {"doing", "in_progress", "processing"}:
                return it
        # 2) by time window
        now = datetime.now(timezone.utc)
        def _parse_dt(v: Any) -> Optional[datetime]:
            if not v:
                return None
            s = str(v)
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
                try:
                    dt = datetime.strptime(s, fmt)
                    # assume local time, make it aware as UTC for compare (approx)
                    return dt.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
            return None
        best = None
        for it in iters:
            s = it.get("start") or it.get("start_time") or it.get("start_date") or it.get("begin") or it.get("begin_date") or it.get("startdate")
            e = it.get("end") or it.get("end_time") or it.get("end_date") or it.get("finish") or it.get("finish_date") or it.get("enddate")
            sd = _parse_dt(s)
            ed = _parse_dt(e)
            if sd and ed and sd <= now <= ed:
                best = it
                break
        return best or (iters[0] if iters else None)

    def list_stories(
        self,
        updated_since: Optional[str] = None,
        page_size: int = 50,
        filters: Optional[Dict[str, Any]] = None,
    ) -> Iterable[Dict[str, Any]]:
        """Yield story dicts via /stories endpoint with Basic Auth.

        Notes:
        - Endpoint and payload shape may differ; we try common shapes.
        - If the endpoint differs in your TAPD tenant, override via env TAPD_STORIES_PATH.
        - Pagination params are conventional: page/limit.
        """
        page = 1
        while True:
            params: Dict[str, Any] = {
                "workspace_id": self.workspace_id,
                "page": page,
                "limit": page_size,
            }
            # Include additional filters (e.g., owner/creator)
            if filters:
                for k, v in filters.items():
                    if v is None:
                        continue
                    if isinstance(v, (list, tuple)):
                        params[k] = ",".join(map(str, v))
                    else:
                        params[k] = v
            # Return Chinese status labels when available
            params.setdefault("with_v_status", 1)
            # Some TAPD APIs support time filters; keep it optional
            if updated_since:
                params["modified"] = updated_since  # may need adjustment per official docs

            res = self._get(self.stories_path, params=params)

            data = res.get("data") if isinstance(res, dict) else None
            if not data:
                break

            # Normalize list of items
            items = data
            if isinstance(items, dict):
                # Some endpoints return dict with list under a key
                for key in ("stories", "list", "items"):
                    if key in items and isinstance(items[key], list):
                        items = items[key]
                        break
            if not isinstance(items, list):
                break

            count = 0
            for it in items:
                # Common TAPD payload pattern: each item wraps entity in capitalized key
                story = (
                    it.get("Story")
                    if isinstance(it, dict) and "Story" in it
                    else it
                )
                if isinstance(story, dict):
                    yield story
                    count += 1
            if count < page_size:
                break
            page += 1

    def get_story(self, story_id: str) -> Dict[str, Any]:
        # Try conventional path; override path via env if differs in your tenant.
        return self._get(f"{self.stories_path}/{story_id}")

    # --- Story extras ----------------------------------------------------
    def fetch_story_extras(
        self,
        story_id: str,
        *,
        include_tags: bool = True,
        include_attachments: bool = True,
        include_comments: bool = True,
    ) -> Dict[str, Any]:
        extras: Dict[str, Any] = {}
        errors: List[str] = []
        sid = str(story_id).strip()
        if not sid:
            return extras

        if include_tags and self.story_tags_path:
            try:
                tags = self._fetch_story_tags(sid)
                if tags:
                    extras["tags"] = tags
            except Exception as exc:  # pragma: no cover - network failure path
                errors.append(f"tags: {exc}")
        if include_attachments and self.story_attachments_path:
            try:
                attachments = self._fetch_story_attachments(sid)
                if attachments:
                    extras["attachments"] = attachments
            except Exception as exc:  # pragma: no cover - network failure path
                errors.append(f"attachments: {exc}")
        if include_comments and self.story_comments_path:
            try:
                comments = self._fetch_story_comments(sid)
                if comments:
                    extras["comments"] = comments
            except Exception as exc:  # pragma: no cover - network failure path
                errors.append(f"comments: {exc}")

        if errors:
            extras["_errors"] = errors
        return extras

    def _fetch_story_tags(self, story_id: str) -> List[str]:
        params_candidates = self._build_param_candidates(story_id)
        res = self._fetch_with_variants(self.story_tags_path, params_candidates)
        items = self._extract_payload_list(res, ("Tag", "StoryTag", "story_tag", "tag"))
        tags: List[str] = []
        for item in items:
            if isinstance(item, dict):
                for key in ("name", "tag", "tag_name", "label", "title", "value"):
                    val = item.get(key)
                    if val is None:
                        continue
                    if isinstance(val, str):
                        val = val.strip()
                        if val:
                            tags.append(val)
                            break
                    elif isinstance(val, (int, float)):
                        tags.append(str(val))
                        break
                    elif isinstance(val, (list, tuple)):
                        for v in val:
                            s = str(v).strip()
                            if s:
                                tags.append(s)
                        break
            elif isinstance(item, str):
                segments = item.replace(";", ",").split(",")
                tags.extend([seg.strip() for seg in segments if seg.strip()])
        if not tags and isinstance(res, dict):
            fallback = res.get("tags") or res.get("tag") or res.get("data")
            if isinstance(fallback, str):
                tags.extend([seg.strip() for seg in fallback.replace(";", ",").split(",") if seg.strip()])
        return self._dedup_preserve(tags)

    def _fetch_story_attachments(self, story_id: str) -> List[Dict[str, Any]]:
        params_candidates = self._build_param_candidates(story_id)
        res = self._fetch_with_variants(
            self.story_attachments_path,
            params_candidates,
        )
        items = self._extract_payload_list(
            res,
            ("Attachment", "StoryAttachment", "story_attachment", "attachment"),
        )
        attachments: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            att_id = self._first_str(item, ("id", "attachment_id", "file_id", "document_id", "aid")) or ""
            name = self._first_str(item, ("name", "title", "filename", "file_name", "attachment_name")) or ""
            url = self._first_str(item, ("url", "download_url", "preview_url", "attachment_url", "file_url")) or ""
            size_raw = item.get("size") or item.get("file_size") or item.get("attachment_size") or item.get("filesize")
            size = self._to_int(size_raw)
            file_type = self._first_str(item, ("filetype", "file_type", "type", "mime_type", "extension")) or ""
            creator = self._first_str(item, ("creator", "owner", "author", "uploader", "create_user", "created_by")) or ""
            created = self._first_str(item, ("created", "created_at", "create_time", "uploaded", "upload_time", "create_at", "createdon")) or ""
            # Skip completely empty attachment entries
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
        return attachments

    def _fetch_story_comments(self, story_id: str) -> List[Dict[str, Any]]:
        params_candidates = self._build_param_candidates(story_id)
        res = self._fetch_with_variants(
            self.story_comments_path,
            params_candidates,
        )
        items = self._extract_payload_list(
            res,
            ("Comment", "StoryComment", "story_comment", "comment"),
        )
        comments: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            content_val = (
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
            author = self._first_str(item, ("author", "creator", "owner", "commenter", "user", "created_by")) or ""
            created = self._first_str(item, ("created", "created_at", "create_time", "added_time", "create_at", "createdon", "time")) or ""
            comment_id = self._first_str(item, ("id", "comment_id", "cid")) or ""
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
        comments.sort(key=lambda x: x.get("created") or "")
        return comments

    def _extract_payload_list(
        self,
        obj: Any,
        wrapper_keys: Tuple[str, ...] = (),
        list_keys: Tuple[str, ...] = (
            "data",
            "list",
            "items",
            "stories",
            "attachments",
            "comments",
            "tags",
            "result",
            "Results",
        ),
    ) -> List[Any]:
        payload = obj
        if isinstance(payload, dict):
            data_candidate = None
            for key in ("data", "Data"):
                if key in payload and isinstance(payload[key], (list, dict)):
                    data_candidate = payload[key]
                    break
            payload = data_candidate if data_candidate is not None else payload
        if isinstance(payload, dict):
            for key in list_keys:
                if key in payload and isinstance(payload[key], (list, dict)):
                    payload = payload[key]
                    break
        if isinstance(payload, list):
            candidates = payload
        elif isinstance(payload, dict):
            candidates = [payload]
        else:
            return []

        items: List[Any] = []
        for entry in candidates:
            candidate = entry
            if isinstance(candidate, dict):
                for key in wrapper_keys:
                    if key in candidate and isinstance(candidate[key], dict):
                        candidate = candidate[key]
                        break
            items.append(candidate)
        return items

    @staticmethod
    def _first_str(data: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[str]:
        for key in keys:
            if key not in data:
                continue
            val = data[key]
            if val is None:
                continue
            if isinstance(val, str):
                s = val.strip()
                if s:
                    return s
            elif isinstance(val, (int, float)):
                return str(val)
            elif isinstance(val, (list, tuple)):
                joined = ", ".join(str(v).strip() for v in val if str(v).strip())
                if joined:
                    return joined
            elif isinstance(val, dict):
                # common patterns like {'name': 'xxx'}
                inner = val.get("name") or val.get("value")
                if isinstance(inner, str) and inner.strip():
                    return inner.strip()
        return None

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        if value in (None, "", "null", "NULL"):
            return None
        try:
            return int(str(value).strip())
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _dedup_preserve(items: List[str]) -> List[str]:
        seen: set[str] = set()
        ordered: List[str] = []
        for item in items:
            if not item:
                continue
            if item not in seen:
                seen.add(item)
                ordered.append(item)
        return ordered

    def _build_param_candidates(self, story_id: str) -> List[Dict[str, Any]]:
        base = {"workspace_id": self.workspace_id}
        keys = (
            "story_id",
            "id",
            "storyId",
            "storyID",
            "story_ids",
            "object_id",
            "resource_id",
        )
        candidates: List[Dict[str, Any]] = []
        for key in keys:
            params = dict(base)
            params[key] = story_id
            candidates.append(params)
        return candidates

    def _fetch_with_variants(
        self,
        path: Optional[str],
        params_candidates: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not path:
            return {}
        candidate_paths = [path]
        if not path.endswith(".json"):
            candidate_paths.append(f"{path.rstrip('/')}" + ".json")
        last_exc: Optional[Exception] = None
        for variant_path in candidate_paths:
            for params in params_candidates:
                try:
                    res = self._get(variant_path, params=params)
                except Exception as exc:  # pragma: no cover - network failure path
                    last_exc = exc
                    continue
                # If we received meaningful payload (non-empty), return immediately
                if self._payload_has_data(res):
                    return res
                # Otherwise keep the result but continue trying other param variants
                if not isinstance(res, dict) or res:
                    # Return the first non-empty dict/list even if we cannot detect data keys
                    return res
        if last_exc:
            raise last_exc
        return {}

    def _payload_has_data(self, payload: Any) -> bool:
        if not payload:
            return False
        if isinstance(payload, list):
            return len(payload) > 0
        if isinstance(payload, dict):
            for key in ("data", "Data", "list", "items", "attachments", "comments", "tags"):
                if key in payload and payload[key]:
                    return True
            # Some APIs return {'Attachment': {...}}
            for key in ("Attachment", "StoryAttachment", "Comment", "StoryComment", "Tag", "StoryTag"):
                if key in payload and payload[key]:
                    return True
        return False
