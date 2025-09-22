from __future__ import annotations
from typing import Any, Dict, Iterable, Optional
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
