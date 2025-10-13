from __future__ import annotations
from typing import Any, Dict, Iterable, Optional, Tuple, List
import re as _re
import html as _html
import os

# Import the official Notion SDK package. Our module name avoids clashing with it.
try:
    from notion_client import Client  # type: ignore
except Exception:  # pragma: no cover - optional in skeleton
    Client = None  # type: ignore

# Late import to avoid hard dependency in minimal flows
try:
    from mapper import (
        map_story_to_notion_properties,
        normalize_status,
        extract_status_label,
    )  # type: ignore
except Exception:
    def map_story_to_notion_properties(_: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore
        return {}
    def normalize_status(x):  # type: ignore
        return str(x) if x is not None else ""
    def extract_status_label(_: Dict[str, Any]) -> str:  # type: ignore
        return ""

from story_utils import (
    extract_story_attachments,
    extract_story_comments,
    extract_story_tags,
    summarize_attachment,
    summarize_comment,
)


class NotionWrapper:
    """Wrapper over Notion SDK with safe fallbacks for skeleton stage."""

    def __init__(self, token: str, database_id: str) -> None:
        self.token = token
        self.database_id = database_id
        self.client = Client(auth=token) if Client else None
        # Resolve property names dynamically to adapt to user's DB schema
        self._title_prop: Optional[str] = None
        self._status_prop: Optional[str] = None
        self._desc_prop: Optional[str] = None
        self._id_prop: Optional[str] = None
        self._id_prop_type: Optional[str] = None
        self._id_prop_meta: Optional[Dict[str, Any]] = None
        self._module_prop: Optional[str] = None
        self._module_is_multi: bool = False
        # Extra properties
        self._owner_prop: Optional[str] = None
        self._owner_is_people: bool = False
        self._priority_prop: Optional[str] = None
        self._iteration_prop: Optional[str] = None
        self._creator_prop: Optional[str] = None
        self._created_at_prop: Optional[str] = None
        self._updated_at_prop: Optional[str] = None
        self._type_prop: Optional[str] = None
        self._severity_prop: Optional[str] = None
        self._url_prop: Optional[str] = None
        self._planned_start_prop: Optional[str] = None
        self._planned_end_prop: Optional[str] = None
        self._planned_range_prop: Optional[str] = None
        self._fe_hours_prop: Optional[str] = None
        self._owner_prop: Optional[str] = None
        self._priority_prop: Optional[str] = None
        self._tag_prop: Optional[str] = None
        self._tag_prop_type: Optional[str] = None
        self._attachment_prop: Optional[str] = None
        self._attachment_prop_type: Optional[str] = None
        self._comment_prop: Optional[str] = None
        self._comment_prop_type: Optional[str] = None
        if self.client and self.database_id:
            self._inspect_database()

    def _inspect_database(self) -> None:
        """Discover property names from the target database.

        - title prop (type=title)
        - a select prop (first type=select) as status (best effort)
        - detect an ID property: 'TAPD_ID' or names containing 'ID'/'编号'/'需求ID' (types: rich_text/title/number/url)
        - a rich_text prop for description: prefer names containing '内容'/'描述'/'说明'/'详情' (excluding the ID prop), else first rich_text not used as ID
        - a '模块' select/multi_select property if present
        - a '负责人' people/multi_select property if present
        - a '优先级' select property if present
        - '预计时间/预计开始/计划开始' 等 date 属性作为开始日期
        - '预计结束/计划结束/结束时间' 等 date 属性作为结束日期
        - 若存在一个 date 属性如 '预计时间'，用作范围属性（start/end）
        - '前端工时/工时' number 属性作为工时（小时）
        """
        try:
            db = self.client.databases.retrieve(self.database_id)  # type: ignore
            props: Dict[str, Any] = db.get("properties", {})
            # Reset cached property metadata before re-detecting
            self._id_prop = None
            self._id_prop_type = None
            self._id_prop_meta = None
            # title prop
            for name, meta in props.items():
                if meta.get("type") == "title":
                    self._title_prop = name
                    break
            # status/select prop (heuristic: prefer name containing '状态')
            for name, meta in props.items():
                if meta.get("type") == "select" and ("状态" in name or self._status_prop is None):
                    self._status_prop = name
                    if "状态" in name:
                        break
            # Detect ID prop
            allowed_types = {"rich_text", "title", "number", "url"}
            id_candidates: List[Tuple[str, Dict[str, Any]]] = []
            for name, meta in props.items():
                t = meta.get("type")
                if t not in allowed_types:
                    continue
                n = str(name)
                if n == "TAPD_ID" or n == "需求ID" or ("ID" in n) or ("编号" in n):
                    id_candidates.append((name, meta))
            chosen: Optional[Tuple[str, Dict[str, Any]]] = None
            for name, meta in id_candidates:
                if name == "TAPD_ID":
                    chosen = (name, meta)
                    break
            if not chosen and id_candidates:
                chosen = id_candidates[0]
            if chosen:
                self._id_prop, self._id_prop_meta = chosen
                self._id_prop_type = self._id_prop_meta.get("type")
            # rich_text desc prop (prefer keywords, exclude id prop)
            for name, meta in props.items():
                if meta.get("type") == "rich_text" and name != self._id_prop and any(k in name for k in ("内容", "描述", "说明", "详情")):
                    self._desc_prop = name
                    break
            if not self._desc_prop:
                for name, meta in props.items():
                    if meta.get("type") == "rich_text" and name != self._id_prop:
                        self._desc_prop = name
                        break
            # Module prop: prefer name containing '模块'; accept select or multi_select
            for name, meta in props.items():
                if "模块" in name and meta.get("type") in {"select", "multi_select"}:
                    self._module_prop = name
                    self._module_is_multi = (meta.get("type") == "multi_select")
                    break
            # Owner prop: prefer name containing '负责人'
            for name, meta in props.items():
                if ("负责人" in name or "owner" in name.lower()) and meta.get("type") in {"multi_select", "people"}:
                    self._owner_prop = name
                    self._owner_is_people = (meta.get("type") == "people")
                    break
            # Priority prop
            for name, meta in props.items():
                if ("优先级" in name or "priority" in name.lower()) and meta.get("type") == "select":
                    self._priority_prop = name
                    break
            # Iteration / creator / timestamps / type / severity / link
            for name, meta in props.items():
                t = meta.get("type")
                n = str(name)
                if ("迭代" in n or "sprint" in n.lower()) and t in {"select", "multi_select"} and not self._iteration_prop:
                    self._iteration_prop = name
                if ("创建人" in n or "creator" in n.lower()) and t in {"people", "rich_text", "multi_select"} and not self._creator_prop:
                    self._creator_prop = name
                if ("创建时间" in n or "created" in n.lower()) and t == "date" and not self._created_at_prop:
                    self._created_at_prop = name
                if ("更新时间" in n or "updated" in n.lower()) and t == "date" and not self._updated_at_prop:
                    self._updated_at_prop = name
                if ("类型" in n or "需求类型" in n or "type" in n.lower()) and t == "select" and not self._type_prop:
                    self._type_prop = name
                if ("严重" in n or "severity" in n.lower()) and t == "select" and not self._severity_prop:
                    self._severity_prop = name
                if ("链接" in n or "url" in n.lower()) and t == "url" and not self._url_prop:
                    self._url_prop = name
            # Planned dates
            for name, meta in props.items():
                if meta.get("type") == "date":
                    n = str(name)
                    if any(k in n for k in ("预计结束", "计划结束", "结束时间", "结束")) and not self._planned_end_prop:
                        self._planned_end_prop = name
                    elif any(k in n for k in ("预计开始", "预计时间", "计划开始", "开始时间", "开始")) and not self._planned_start_prop:
                        self._planned_start_prop = name
            # If there's a single date property named like '预计时间' and no separate start/end, use as range
            if not self._planned_end_prop and not self._planned_range_prop:
                for name, meta in props.items():
                    if meta.get("type") == "date" and ("预计时间" in name or "计划时间" in name):
                        self._planned_range_prop = name
                        break
            # Frontend hours property detection (prefer explicit FE over generic "工时")
            try:
                forced = os.getenv("NOTION_FE_HOURS_PROP", "").strip()
            except Exception:
                forced = ""
            if forced and forced in props and props[forced].get("type") == "number":
                self._fe_hours_prop = forced
            else:
                # Score candidates: +100 if contains '前端'/'FE'/'frontend'; -50 if contains '后端'/'BE'/'backend';
                # +10 if contains generic hours terms; pick highest > 0
                def _score(name: str) -> int:
                    n = name.lower()
                    score = 0
                    if ("前端" in name) or ("frontend" in n) or (" fe" in f" {n}") or n.startswith("fe") or ("fe工时" in n):
                        score += 100
                    if ("后端" in name) or ("backend" in n) or (" be" in f" {n}"):
                        score -= 50
                    if any(k in name for k in ("工时", "小时", "工作量", "预估工时", "预计工时")) or any(k in n for k in ("hours", "hour", "estimate")):
                        score += 10
                    # exact friendly names
                    if name in ("前端工时", "FE工时", "Frontend Hours"):
                        score += 50
                    return score
                best_name: Optional[str] = None
                best_score = 0
                for name, meta in props.items():
                    if meta.get("type") != "number":
                        continue
                    sc = _score(str(name))
                    if sc > best_score:
                        best_score = sc
                        best_name = name
                if best_score > 0 and best_name:
                    self._fe_hours_prop = best_name
            # Extended metadata fields for tags/attachments/comments
            for name, meta in props.items():
                t = meta.get("type")
                lower = str(name).lower()
                if (
                    self._tag_prop is None
                    and t in {"multi_select", "select", "rich_text"}
                    and ("标签" in str(name) or "tag" in lower)
                ):
                    self._tag_prop = name
                    self._tag_prop_type = t
                if (
                    self._attachment_prop is None
                    and t in {"files", "rich_text", "url", "multi_select", "select"}
                    and ("附件" in str(name) or "attachment" in lower or ("file" in lower and t == "files"))
                ):
                    self._attachment_prop = name
                    self._attachment_prop_type = t
                if (
                    self._comment_prop is None
                    and t in {"rich_text", "multi_select"}
                    and ("评论" in str(name) or "comment" in lower)
                ):
                    self._comment_prop = name
                    self._comment_prop_type = t
        except Exception:
            # Keep defaults None if schema fetch fails
            pass

    # --- Utilities over Database -----------------------------------------
    def _extract_plain_text(self, rich: Any) -> str:
        # Concatenate plain_text from rich_text/title arrays
        try:
            if isinstance(rich, list):
                return "".join([x.get("plain_text", "") for x in rich if isinstance(x, dict)])
        except Exception:
            pass
        return ""

    def _html_to_text(self, html: str) -> str:
        # Light-weight converter mirroring content.html_to_text behavior
        s = html or ""
        s = _re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", s)
        s = _re.sub(r"(?i)</\s*p\s*>", "\n\n", s)
        s = _re.sub(r"(?i)<\s*p\s*>", "", s)
        s = _re.sub(r"(?i)<\s*li\s*>\s*", "- ", s)
        s = _re.sub(r"(?i)</\s*li\s*>", "\n", s)
        s = _re.sub(r"<[^>]+>", "", s)
        s = _html.unescape(s)
        s = _re.sub(r"\r\n?|\r", "\n", s)
        s = _re.sub(r"\n{3,}", "\n\n", s)
        return s.strip()

    def _parse_date_str(self, v: Any) -> Optional[str]:
        if not v:
            return None
        s = str(v).strip()
        if not s:
            return None
        # Accept common formats; output YYYY-MM-DD
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                from datetime import datetime
                dt = datetime.strptime(s, fmt)
                return dt.strftime("%Y-%m-%d")
            except Exception:
                continue
        # Fallback: digits pattern YYYY-MM-DD
        m = _re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
        if m:
            y, mth, d = m.groups()
            return f"{y}-{int(mth):02d}-{int(d):02d}"
        return None

    def _extract_dates(self, story: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
        # Heuristics: begin/start/start_time/start_date -> start; due/end/end_time/end_date -> end
        start = story.get("begin") or story.get("start") or story.get("start_time") or story.get("start_date") or story.get("plan_start")
        end = story.get("due") or story.get("end") or story.get("end_time") or story.get("end_date") or story.get("plan_end")
        return self._parse_date_str(start), self._parse_date_str(end)

    def _extract_fe_hours(self, story: Dict[str, Any]) -> Optional[float]:
        """Extract frontend hours from TAPD story with safe precedence.

        Precedence:
        1) Explicit env override TAPD_FE_HOURS_KEYS (CSV) first-match.
        2) FE-specific keys only (contain 前端/FE/frontend or exact known names).
        3) Heuristic scan preferring keys with 前端/FE and excluding 后端/BE/backend.
        Generic keys like '工时/预计工时/预估工时' are NOT used unless they also include FE hints.

        Units: h/hour/小时 -> hours; m/min/分钟 -> minutes/60; d/day/天/人天 -> *8 hours.
        """
        # 1) Env override
        try:
            env_keys = os.getenv("TAPD_FE_HOURS_KEYS", "")
            fe_env_keys = [s.strip() for s in env_keys.split(',') if s.strip()]
        except Exception:
            fe_env_keys = []
        def _get_first(keys: List[str]) -> Optional[Any]:
            for k in keys:
                if k in story and story.get(k) not in (None, ""):
                    return story.get(k)
            return None
        val = _get_first(fe_env_keys) if fe_env_keys else None
        if val is None:
            # 2) FE-specific common keys
            specific_keys = [
                "frontend_hours", "front_end_hours", "fe_hours", "fe_hour", "fe_workload",
                "fe_estimate", "fe_effort",
                "前端工时", "前端估算", "FE工时",
            ]
            val = _get_first(specific_keys)
        if val is None:
            # 3) Heuristic scan: prefer keys indicating FE, exclude BE/backend
            best_k = None
            for k, v in story.items():
                if v in (None, ""):
                    continue
                ks = str(k)
                low = ks.lower()
                if ("后端" in ks) or ("backend" in low) or (low.startswith("be_")) or (" be" in f" {low}"):
                    continue
                # require FE hint
                if ("前端" in ks) or ("fe" in low) or ("frontend" in low):
                    best_k = k
                    val = v
                    break
        if val is None:
            return None
        s = str(val).strip()
        # direct float
        try:
            return float(s)
        except Exception:
            pass
        # parse number + optional unit
        m = _re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z\u4e00-\u9fa5]*)", s)
        if not m:
            return None
        num = float(m.group(1))
        unit = (m.group(2) or '').strip().lower()
        if not unit:
            return num
        # days -> hours (assume 8h/day)
        if unit in {"d", "day", "days", "天", "人天"}:
            return num * 8.0
        if unit in {"h", "hr", "hrs", "hour", "hours", "小时"}:
            return num
        if unit in {"m", "min", "mins", "minute", "minutes", "分钟"}:
            return num / 60.0
        # fallback: return numeric part
        return num

    def _apply_extended_story_properties(self, props: Dict[str, Any], story: Dict[str, Any]) -> None:
        tags = extract_story_tags(story)
        if self._tag_prop and tags:
            prop_type = self._tag_prop_type or ""
            if prop_type == "multi_select":
                options = [{"name": tag[:100]} for tag in tags[:100]]
                if options:
                    props[self._tag_prop] = {"multi_select": options}
            elif prop_type == "select":
                props[self._tag_prop] = {"select": {"name": tags[0][:100]}}
            elif prop_type == "rich_text":
                joined = ", ".join(tags)[:1800]
                props[self._tag_prop] = {"rich_text": [{"text": {"content": joined}}]}

        attachments = extract_story_attachments(story)
        if self._attachment_prop and attachments:
            prop_type = self._attachment_prop_type or ""
            if prop_type == "files":
                files_payload = []
                for att in attachments[:20]:
                    url = str(att.get("url") or "").strip()
                    if not url:
                        continue
                    name = str(att.get("name") or "附件").strip() or "附件"
                    files_payload.append(
                        {
                            "name": name[:100],
                            "type": "external",
                            "external": {"url": url},
                        }
                    )
                if files_payload:
                    props[self._attachment_prop] = {"files": files_payload}
            elif prop_type == "rich_text":
                lines = [summarize_attachment(att) for att in attachments[:10]]
                if lines:
                    joined = "\n".join(lines)[:1900]
                    props[self._attachment_prop] = {"rich_text": [{"text": {"content": joined}}]}
            elif prop_type == "url":
                first_url = next((att.get("url") for att in attachments if att.get("url")), None)
                if first_url:
                    props[self._attachment_prop] = {"url": str(first_url)}
            elif prop_type == "multi_select":
                options = []
                for att in attachments[:50]:
                    label = str(att.get("name") or att.get("url") or "附件").strip()
                    if label:
                        options.append({"name": label[:100]})
                if options:
                    props[self._attachment_prop] = {"multi_select": options}
            elif prop_type == "select":
                label = str(attachments[0].get("name") or attachments[0].get("url") or "附件").strip()
                if label:
                    props[self._attachment_prop] = {"select": {"name": label[:100]}}

        comments = extract_story_comments(story)
        if self._comment_prop and comments:
            prop_type = self._comment_prop_type or ""
            if prop_type == "rich_text":
                lines = [summarize_comment(cm) for cm in comments[:10]]
                if lines:
                    joined = "\n".join(lines)[:1800]
                    props[self._comment_prop] = {"rich_text": [{"text": {"content": joined}}]}
            elif prop_type == "multi_select":
                options = []
                for cm in comments[:25]:
                    summary = summarize_comment(cm)
                    if summary:
                        options.append({"name": summary[:100]})
                if options:
                    props[self._comment_prop] = {"multi_select": options}

    # --- Detail subpage helpers ------------------------------------------
    def _archive_detail_subpages(self, parent_page_id: str) -> None:
        """Archive old detail child_pages to avoid confusion after moving content to main page."""
        if not self.client:
            return
        title = os.getenv("DETAIL_SUBPAGE_TITLE", "需求详情")
        try:
            start_cursor: Optional[str] = None
            while True:
                kwargs: Dict[str, Any] = {"block_id": parent_page_id}
                if start_cursor:
                    kwargs["start_cursor"] = start_cursor
                res = self.client.blocks.children.list(**kwargs)  # type: ignore
                for blk in res.get("results", []):
                    try:
                        if blk.get("type") == "child_page":
                            cp = blk.get("child_page", {})
                            if isinstance(cp, dict) and cp.get("title") == title:
                                self.client.blocks.update(block_id=blk.get("id"), archived=True)  # type: ignore
                    except Exception:
                        pass
                if not res.get("has_more"):
                    break
                start_cursor = res.get("next_cursor")
        except Exception:
            pass

    def _status_from_story(self, story: Dict[str, Any]) -> Optional[str]:
        # Prefer human-readable labels when provided by TAPD API
        label = extract_status_label(story)
        if label:
            return str(label)
        raw = story.get("status") or story.get("current_status")
        lab = normalize_status(raw)
        return lab or (str(raw) if raw is not None else None)

    def iter_database_pages(self, page_size: int = 100) -> Iterable[Dict[str, Any]]:
        """Yield pages in the target database with properties (paginated)."""
        if not self.client:
            return []
        start_cursor: Optional[str] = None
        while True:
            try:
                kwargs: Dict[str, Any] = {
                    "database_id": self.database_id,
                    "page_size": page_size,
                }
                if start_cursor:
                    kwargs["start_cursor"] = start_cursor
                res = self.client.databases.query(**kwargs)  # type: ignore
                results = res.get("results", [])
                for pg in results:
                    yield pg
                if not res.get("has_more"):
                    break
                start_cursor = res.get("next_cursor")
            except Exception:
                break

    def existing_index(self) -> Dict[str, str]:
        """Return mapping of TAPD_ID -> page_id for current database.

        Prefers dedicated TAPD_ID property; fallback to parse ID marker inside description.
        """
        idx: Dict[str, str] = {}
        if not self.client:
            return idx
        id_prop = self._id_prop
        desc_prop = self._desc_prop
        for pg in self.iter_database_pages():
            try:
                pid = pg.get("id")
                props: Dict[str, Any] = pg.get("properties", {})
                tapd_id_val: Optional[str] = None
                if id_prop and id_prop in props:
                    meta = props[id_prop]
                    t = meta.get("type")
                    if t == "rich_text":
                        tapd_id_val = self._extract_plain_text(meta.get("rich_text", []))
                    elif t == "title":
                        tapd_id_val = self._extract_plain_text(meta.get("title", []))
                    elif t == "number":
                        v = meta.get("number")
                        tapd_id_val = str(v) if v is not None else None
                    elif t == "url":
                        v = meta.get("url")
                        tapd_id_val = str(v) if v else None
                if not tapd_id_val and desc_prop and desc_prop in props:
                    # Fallback parse from description text
                    meta = props[desc_prop]
                    if meta.get("type") == "rich_text":
                        text = self._extract_plain_text(meta.get("rich_text", []))
                        # Look for marker "TAPD_ID: <id>"
                        import re
                        m = re.search(r"TAPD_ID:\s*([\w-]+)", text)
                        if m:
                            tapd_id_val = m.group(1)
                if tapd_id_val and pid:
                    idx[str(tapd_id_val)] = pid
            except Exception:
                continue
        return idx

    def clear_database(self, *, deep: bool = False) -> int:
        """Archive all pages in the database. Returns number of pages affected.

        When deep=True, also recursively archive child pages under each page
        (i.e., blocks of type 'child_page') to avoid leftover subpages.
        """
        if not self.client:
            return 0
        n = 0
        for pg in self.iter_database_pages():
            try:
                pid = pg.get("id")
                if deep and pid:
                    n += self._archive_page_and_children(pid)
                else:
                    self.client.pages.update(page_id=pid, archived=True)  # type: ignore
                    n += 1
            except Exception:
                continue
        return n

    def _archive_page_and_children(self, page_id: str) -> int:
        """Recursively archive a page and all child pages.

        - Walk block children; for each 'child_page' block, archive that page
          (and recurse). For 'child_database', archive the block to hide it.
        - Finally archive the current page.
        Returns number of pages archived (including children and self).
        """
        if not self.client:
            return 0
        archived = 0
        try:
            # List child blocks in pages
            start_cursor: Optional[str] = None
            while True:
                kwargs: Dict[str, Any] = {"block_id": page_id}
                if start_cursor:
                    kwargs["start_cursor"] = start_cursor
                res = self.client.blocks.children.list(**kwargs)  # type: ignore
                for blk in res.get("results", []):
                    try:
                        btype = blk.get("type")
                        bid = blk.get("id")
                        if btype == "child_page" and bid:
                            # child page id == block id; archive recursively
                            archived += self._archive_page_and_children(bid)
                        elif btype == "child_database" and bid:
                            # we cannot delete database; archive the block so it's hidden
                            try:
                                self.client.blocks.update(block_id=bid, archived=True)  # type: ignore
                            except Exception:
                                pass
                    except Exception:
                        continue
                if not res.get("has_more"):
                    break
                start_cursor = res.get("next_cursor")
        except Exception:
            pass
        try:
            self.client.pages.update(page_id=page_id, archived=True)  # type: ignore
            archived += 1
        except Exception:
            pass
        return archived

    # --- Status enum sync -------------------------------------------------
    def _db_properties(self) -> Dict[str, Any]:
        try:
            db = self.client.databases.retrieve(self.database_id)  # type: ignore
            return db.get("properties", {})
        except Exception:
            return {}

    def sync_status_options(self, options: Iterable[str]) -> bool:
        """Ensure status select options include the provided names.

        - No-op if status property not detected
        - Merge new options with existing ones; preserve existing colors
        - Assign heuristic colors to new ones
        Returns True on success
        """
        if not self.client or not self._status_prop:
            return False
        prop_name = self._status_prop
        props = self._db_properties()
        meta = props.get(prop_name, {})
        if meta.get("type") != "select":
            return False
        existing = {opt.get("name"): opt.get("color", "default") for opt in meta.get("select", {}).get("options", [])}

        def color_for(name: str) -> str:
            n = name.strip().lower()
            if any(k in n for k in ("完成", "done", "已完成", "resolved", "passed")):
                return "green"
            if any(k in n for k in ("关闭", "close", "已关闭", "rejected", "reject")):
                return "red"
            if any(k in n for k in ("新建", "new", "open")):
                return "blue"
            if any(k in n for k in ("进行", "doing", "progress", "处理中")):
                return "yellow"
            return "default"

        desired = dict(existing)
        for name in options:
            if not name:
                continue
            name = str(name)
            if name not in desired:
                desired[name] = color_for(name)
        try:
            self.client.databases.update(  # type: ignore
                database_id=self.database_id,
                properties={
                    prop_name: {
                        "select": {
                            "options": [{"name": k, "color": v or "default"} for k, v in desired.items()]
                        }
                    }
                },
            )
            return True
        except Exception:
            return False

    def sync_status_options_from_tapd(
        self,
        tapd_client,
        sample_pages: int = 10,
        page_size: int = 50,
        base_filters: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Collect status labels from TAPD stories and sync to Notion DB options.

        Returns number of distinct labels discovered.
        """
        if not self.client:
            return 0
        labels = set()
        page = 1
        while page <= sample_pages:
            params: Dict[str, Any] = {
                "workspace_id": tapd_client.workspace_id,
                "page": page,
                "limit": page_size,
                "with_v_status": 1,
            }
            if base_filters:
                params.update(base_filters)
            try:
                res = tapd_client._get(tapd_client.stories_path, params=params)
                data = res.get("data") if isinstance(res, dict) else None
                if not data:
                    break
                items = data
                if isinstance(items, dict):
                    for key in ("stories", "list", "items"):
                        if key in items and isinstance(items[key], list):
                            items = items[key]
                            break
                if not isinstance(items, list):
                    break
                for it in items:
                    story = it.get("Story") if isinstance(it, dict) and "Story" in it else it
                    if not isinstance(story, dict):
                        continue
                    # Prefer v_status/status_label
                    label = story.get("v_status") or story.get("status_label") or story.get("status_name")
                    if not label:
                        raw = story.get("status")
                        label = str(raw) if raw is not None else None
                    if label:
                        labels.add(str(label))
                if len(items) < page_size:
                    break
            except Exception:
                break
            page += 1
        self.sync_status_options(sorted(labels))
        return len(labels)

    # --- Export helpers ---------------------------------------------------
    def get_page_blocks(self, page_id: str, page_size: int = 100) -> List[Dict[str, Any]]:
        blocks: List[Dict[str, Any]] = []
        if not self.client:
            return blocks
        try:
            start_cursor: Optional[str] = None
            while True:
                kwargs: Dict[str, Any] = {"block_id": page_id, "page_size": page_size}
                if start_cursor:
                    kwargs["start_cursor"] = start_cursor
                res = self.client.blocks.children.list(**kwargs)  # type: ignore
                blocks.extend(res.get("results", []))
                if not res.get("has_more"):
                    break
                start_cursor = res.get("next_cursor")
        except Exception:
            pass
        return blocks

    def page_url(self, page_id: str) -> str:
        # Notion canonical URL without workspace context (best-effort)
        return f"https://www.notion.so/{page_id.replace('-', '')}"

    def _build_id_prop_payload(self, tapd_id: str) -> Optional[Dict[str, Any]]:
        if not self._id_prop or not tapd_id:
            return None
        prop_type = self._id_prop_type or "rich_text"
        if prop_type == "rich_text":
            return {"rich_text": [{"text": {"content": tapd_id}}]}
        if prop_type == "title":
            return {"title": [{"text": {"content": tapd_id}}]}
        if prop_type == "number":
            if tapd_id.isdigit():
                try:
                    return {"number": int(tapd_id)}
                except ValueError:
                    return None
            try:
                num_val = float(tapd_id)
            except ValueError:
                return None
            return {"number": num_val}
        if prop_type == "url":
            return {"url": tapd_id}
        return None

    def find_page_by_tapd_id(self, tapd_id: str, *, suppress_errors: bool = True) -> Optional[str]:
        """Return Notion page id if exists.

        Prefer dedicated TAPD_ID property; fallback to search in a rich_text property
        (contains) if no dedicated id prop is present.
        """
        if not self.client or not tapd_id:
            return None
        # First try dedicated id property
        if self._id_prop:
            tapd_id_str = str(tapd_id)
            prop_type = self._id_prop_type or "rich_text"
            filter_payload: Optional[Dict[str, Any]] = None
            if prop_type == "rich_text":
                filter_payload = {
                    "property": self._id_prop,
                    "rich_text": {"equals": tapd_id_str},
                }
            elif prop_type == "title":
                filter_payload = {
                    "property": self._id_prop,
                    "title": {"equals": tapd_id_str},
                }
            elif prop_type == "number":
                number_value: Optional[object] = None
                if tapd_id_str.isdigit():
                    try:
                        number_value = int(tapd_id_str)
                    except ValueError:
                        number_value = None
                if number_value is None:
                    try:
                        number_value = float(tapd_id_str)
                    except ValueError:
                        number_value = None
                if number_value is not None:
                    filter_payload = {
                        "property": self._id_prop,
                        "number": {"equals": number_value},
                    }
            elif prop_type == "url":
                filter_payload = {
                    "property": self._id_prop,
                    "url": {"equals": tapd_id_str},
                }
            if filter_payload:
                try:
                    res = self.client.databases.query(
                        database_id=self.database_id,
                        filter=filter_payload,
                        page_size=1,
                    )
                    results = res.get("results", [])
                    if results:
                        return results[0]["id"]
                except Exception as exc:
                    print(
                        f"[notion] TAPD_ID lookup failed for property {self._id_prop}"
                        f" ({prop_type}): {exc}"
                    )
                    if not suppress_errors:
                        raise
        # Fallback: search in description rich_text
        if self._desc_prop:
            try:
                res = self.client.databases.query(
                    database_id=self.database_id,
                    filter={
                        "property": self._desc_prop,
                        "rich_text": {"contains": f"TAPD_ID: {tapd_id}"},
                    },
                    page_size=1,
                )
                results = res.get("results", [])
                if results:
                    return results[0]["id"]
            except Exception as exc:
                if not suppress_errors:
                    raise
        return None

    def find_page_by_title(self, title: str, *, suppress_errors: bool = True) -> Optional[str]:
        if not self.client or not title or not self._title_prop:
            return None
        try:
            res = self.client.databases.query(  # type: ignore
                database_id=self.database_id,
                filter={
                    "property": self._title_prop,
                    "title": {"equals": str(title)},
                },
                page_size=1,
            )
            results = res.get("results", [])
            if results:
                return results[0]["id"]
        except Exception as exc:
            if not suppress_errors:
                raise
        return None

    def update_story_page_if_exists(self, story: Dict[str, Any], blocks: Optional[list] = None) -> Optional[str]:
        """Only update existing page; never create.

        Match by TAPD_ID first, then fallback to title equality.
        Returns page_id if updated, else None.
        """
        tapd_id = str(story.get("id", ""))
        page_id = self.find_page_by_tapd_id(tapd_id) if tapd_id else None
        if not page_id:
            title = story.get("name") or story.get("title")
            if title:
                page_id = self.find_page_by_title(str(title))
        if not page_id or not self.client:
            return None
        # Build properties and update
        props: Dict[str, Any] = {}
        title = story.get("name") or story.get("title") or (f"TAPD {tapd_id}" if tapd_id else "")
        if self._title_prop and title:
            props[self._title_prop] = {"title": [{"text": {"content": title}}]}
        status_val = self._status_from_story(story)
        if self._status_prop and status_val:
            props[self._status_prop] = {"select": {"name": str(status_val)}}
        if self._id_prop and tapd_id:
            id_payload = self._build_id_prop_payload(str(tapd_id))
            if id_payload is not None:
                props[self._id_prop] = id_payload
        if self._desc_prop:
            desc = story.get("description") or ""
            if isinstance(desc, str) and ("<" in desc and ">" in desc):
                desc = self._html_to_text(desc)
            summary = (f"TAPD_ID: {tapd_id}\n" if tapd_id else "") + (desc[:1800] if isinstance(desc, str) else str(desc))
            props[self._desc_prop] = {"rich_text": [{"text": {"content": summary}}]}
        # Module label (best-effort from multiple possible keys)
        mod_name = (
            story.get("module")
            or story.get("module_name")
            or story.get("category")
            or story.get("module_path")
            or story.get("module_label")
        )
        if self._module_prop and mod_name:
            if self._module_is_multi:
                props[self._module_prop] = {"multi_select": [{"name": str(mod_name)}]}
            else:
                props[self._module_prop] = {"select": {"name": str(mod_name)}}
        # Owner/Assignees
        if self._owner_prop:
            owners = []
            for key in ("owner", "assignee", "current_owner", "owners"):
                v = story.get(key)
                if v is None:
                    continue
                if isinstance(v, (list, tuple, set)):
                    owners.extend([str(x) for x in v if str(x)])
                else:
                    owners.append(str(v))
            owners = [o for i, o in enumerate(owners) if o and o not in owners[:i]]
            if owners:
                if getattr(self, "_owner_is_people", False):
                    # Attempt to resolve people by exact name match
                    try:
                        people = []
                        cache = getattr(self, "_people_cache_by_name", None)
                        if cache is None:
                            cache = {}
                            setattr(self, "_people_cache_by_name", cache)
                            # Build cache: name(lower) -> id
                            res = self.client.users.list()  # type: ignore
                            for u in res.get("results", []):
                                nm = (u.get("name") or "").strip()
                                uid = u.get("id")
                                if nm and uid:
                                    cache[nm.lower()] = uid
                        for nm in owners:
                            uid = cache.get(str(nm).lower())
                            if uid:
                                people.append({"id": uid})
                        if people:
                            props[self._owner_prop] = {"people": people}
                    except Exception:
                        # silently ignore if we cannot resolve people ids
                        pass
                else:
                    props[self._owner_prop] = {"multi_select": [{"name": o} for o in owners]}
        # Priority
        if self._priority_prop:
            pr = story.get("priority") or story.get("priority_label") or story.get("priority_name")
            if pr is not None and str(pr).strip():
                props[self._priority_prop] = {"select": {"name": str(pr)}}
        # Iteration / creator / timestamps / type / severity / url
        if self._iteration_prop:
            itname = story.get("iteration_name") or story.get("sprint_name") or story.get("iteration")
            itid = story.get("iteration_id") or story.get("sprint_id")
            val = itname or itid
            if val:
                if self._module_is_multi and isinstance(val, (list, tuple)):
                    props[self._iteration_prop] = {"multi_select": [{"name": str(x)} for x in val]}
                else:
                    props[self._iteration_prop] = {"select": {"name": str(val)}}
        if self._creator_prop:
            creator = story.get("creator") or story.get("created_by") or story.get("author")
            if creator:
                if self._owner_is_people and isinstance(creator, str):
                    try:
                        cache = getattr(self, "_people_cache_by_name", None)
                        if cache is None:
                            cache = {}
                            setattr(self, "_people_cache_by_name", cache)
                            resu = self.client.users.list()  # type: ignore
                            for u in resu.get("results", []):
                                nm = (u.get("name") or "").strip()
                                uid = u.get("id")
                                if nm and uid:
                                    cache[nm.lower()] = uid
                        uid = cache.get(str(creator).lower())
                        if uid:
                            props[self._creator_prop] = {"people": [{"id": uid}]}
                    except Exception:
                        pass
                else:
                    props[self._creator_prop] = {"rich_text": [{"text": {"content": str(creator)}}]}
        def _dt(v: Any) -> Optional[str]:
            if not v:
                return None
            s = str(v)
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
                try:
                    from datetime import datetime
                    return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
                except Exception:
                    continue
            return None
        if self._created_at_prop:
            d = _dt(story.get("created")) or _dt(story.get("create_time")) or _dt(story.get("created_at"))
            if d:
                props[self._created_at_prop] = {"date": {"start": d}}
        if self._updated_at_prop:
            d = _dt(story.get("modified")) or _dt(story.get("update_time")) or _dt(story.get("updated_at"))
            if d:
                props[self._updated_at_prop] = {"date": {"start": d}}
        if self._type_prop:
            tp = story.get("type") or story.get("story_type")
            if tp:
                props[self._type_prop] = {"select": {"name": str(tp)}}
        if self._severity_prop:
            sv = story.get("severity") or story.get("level")
            if sv is not None and str(sv).strip():
                props[self._severity_prop] = {"select": {"name": str(sv)}}
        if self._url_prop:
            url = story.get("url") or story.get("link")
            if url and isinstance(url, str):
                props[self._url_prop] = {"url": url}
        # Iteration / creator / timestamps / type / severity / url
        if self._iteration_prop:
            itname = story.get("iteration_name") or story.get("sprint_name") or story.get("iteration")
            itid = story.get("iteration_id") or story.get("sprint_id")
            val = itname or itid
            if val:
                if self._module_is_multi and isinstance(val, (list, tuple)):
                    props[self._iteration_prop] = {"multi_select": [{"name": str(x)} for x in val]}
                else:
                    props[self._iteration_prop] = {"select": {"name": str(val)}}
        if self._creator_prop:
            creator = story.get("creator") or story.get("created_by") or story.get("author")
            if creator:
                if self._owner_is_people and isinstance(creator, str):
                    # try map single creator to a person
                    try:
                        cache = getattr(self, "_people_cache_by_name", None)
                        if cache is None:
                            cache = {}
                            setattr(self, "_people_cache_by_name", cache)
                            resu = self.client.users.list()  # type: ignore
                            for u in resu.get("results", []):
                                nm = (u.get("name") or "").strip()
                                uid = u.get("id")
                                if nm and uid:
                                    cache[nm.lower()] = uid
                        uid = cache.get(str(creator).lower())
                        if uid:
                            props[self._creator_prop] = {"people": [{"id": uid}]}
                    except Exception:
                        pass
                else:
                    props[self._creator_prop] = {"rich_text": [{"text": {"content": str(creator)}}]}
        def _dt(v: Any) -> Optional[str]:
            if not v:
                return None
            s = str(v)
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
                try:
                    from datetime import datetime
                    return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
                except Exception:
                    continue
            return None
        if self._created_at_prop:
            d = _dt(story.get("created")) or _dt(story.get("create_time")) or _dt(story.get("created_at"))
            if d:
                props[self._created_at_prop] = {"date": {"start": d}}
        if self._updated_at_prop:
            d = _dt(story.get("modified")) or _dt(story.get("update_time")) or _dt(story.get("updated_at"))
            if d:
                props[self._updated_at_prop] = {"date": {"start": d}}
        if self._type_prop:
            tp = story.get("type") or story.get("story_type")
            if tp:
                props[self._type_prop] = {"select": {"name": str(tp)}}
        if self._severity_prop:
            sv = story.get("severity") or story.get("level")
            if sv is not None and str(sv).strip():
                props[self._severity_prop] = {"select": {"name": str(sv)}}
        if self._url_prop:
            url = story.get("url") or story.get("link")
            if url and isinstance(url, str):
                props[self._url_prop] = {"url": url}
        # Planned dates & frontend hours
        self._apply_extended_story_properties(props, story)
        self._apply_extended_story_properties(props, story)
        self._apply_extended_story_properties(props, story)
        start_d, end_d = self._extract_dates(story)
        if self._planned_range_prop and (start_d or end_d):
            props[self._planned_range_prop] = {"date": {"start": start_d or end_d, "end": (end_d if start_d else None)}}
        else:
            if self._planned_start_prop and start_d:
                props[self._planned_start_prop] = {"date": {"start": start_d}}
            if self._planned_end_prop and end_d:
                props[self._planned_end_prop] = {"date": {"start": end_d}}
        feh = self._extract_fe_hours(story)
        if self._fe_hours_prop and feh is not None:
            props[self._fe_hours_prop] = {"number": feh}
        try:
            self.client.pages.update(page_id=page_id, properties=props)  # type: ignore
            if blocks:
                try:
                    self._replace_children(page_id, blocks)
                except Exception:
                    self.client.blocks.children.append(block_id=page_id, children=blocks)
            return page_id
        except Exception:
            return None

    def upsert_story_page(self, story: Dict[str, Any], blocks: Optional[list] = None) -> str:
        """Create or update a Notion page from a TAPD story.

        - Finds by `TAPD_ID` rich_text property.
        - Updates properties when page exists; appends blocks if provided.
        - Creates new page otherwise, with children blocks.
        Falls back to dummy id if SDK unavailable.
        """
        raw_id = story.get("id")
        tapd_id = str(raw_id) if raw_id is not None else ""
        if not tapd_id or tapd_id in {"", "None", "unknown"}:
            return "skip-no-id"
        if not self.client:
            return f"dummy-page-{tapd_id}"

        # Build properties against user's DB schema
        props: Dict[str, Any] = {}
        title = story.get("name") or story.get("title") or f"TAPD {tapd_id}"
        if not self._title_prop and self.client:
            # Retry inspect to get title property; if still missing, skip to avoid blank pages
            try:
                self._inspect_database()
            except Exception:
                pass
        if self._title_prop:
            props[self._title_prop] = {"title": [{"text": {"content": title}}]}
        else:
            return "skip-missing-title"
        # Status/select if present
        status_val = self._status_from_story(story)
        if self._status_prop and status_val:
            # Keep the label as-is; mapping table can be added later
            props[self._status_prop] = {"select": {"name": str(status_val)}}
        # Optional TAPD_ID prop
        if self._id_prop:
            id_payload = self._build_id_prop_payload(str(tapd_id))
            if id_payload is not None:
                props[self._id_prop] = id_payload
        # Description summary into rich_text prop (also embed TAPD_ID marker for fallback matching)
        if self._desc_prop:
            desc = story.get("description") or ""
            if isinstance(desc, str) and ("<" in desc and ">" in desc):
                desc = self._html_to_text(desc)
            summary = f"TAPD_ID: {tapd_id}\n" + (desc[:1800] if isinstance(desc, str) else str(desc))
            props[self._desc_prop] = {"rich_text": [{"text": {"content": summary}}]}
        # Module property if present
        mod_name = (
            story.get("module")
            or story.get("module_name")
            or story.get("category")
            or story.get("module_path")
            or story.get("module_label")
        )
        if self._module_prop and mod_name:
            if self._module_is_multi:
                props[self._module_prop] = {"multi_select": [{"name": str(mod_name)}]}
            else:
                props[self._module_prop] = {"select": {"name": str(mod_name)}}
        # Owner/Assignees
        if self._owner_prop:
            owners = []
            for key in ("owner", "assignee", "current_owner", "owners"):
                v = story.get(key)
                if v is None:
                    continue
                if isinstance(v, (list, tuple, set)):
                    owners.extend([str(x) for x in v if str(x)])
                else:
                    owners.append(str(v))
            owners = [o for i, o in enumerate(owners) if o and o not in owners[:i]]
            if owners:
                if getattr(self, "_owner_is_people", False):
                    try:
                        people = []
                        cache = getattr(self, "_people_cache_by_name", None)
                        if cache is None:
                            cache = {}
                            setattr(self, "_people_cache_by_name", cache)
                            res = self.client.users.list()  # type: ignore
                            for u in res.get("results", []):
                                nm = (u.get("name") or "").strip()
                                uid = u.get("id")
                                if nm and uid:
                                    cache[nm.lower()] = uid
                        for nm in owners:
                            uid = cache.get(str(nm).lower())
                            if uid:
                                people.append({"id": uid})
                        if people:
                            props[self._owner_prop] = {"people": people}
                    except Exception:
                        pass
                else:
                    props[self._owner_prop] = {"multi_select": [{"name": o} for o in owners]}
        # Priority
        if self._priority_prop:
            pr = story.get("priority") or story.get("priority_label") or story.get("priority_name")
            if pr is not None and str(pr).strip():
                props[self._priority_prop] = {"select": {"name": str(pr)}}
        # Planned dates & FE hours
        start_d, end_d = self._extract_dates(story)
        if self._planned_range_prop and (start_d or end_d):
            props[self._planned_range_prop] = {"date": {"start": start_d or end_d, "end": (end_d if start_d else None)}}
        else:
            if self._planned_start_prop and start_d:
                props[self._planned_start_prop] = {"date": {"start": start_d}}
            if self._planned_end_prop and end_d:
                props[self._planned_end_prop] = {"date": {"start": end_d}}
        feh = self._extract_fe_hours(story)
        if self._fe_hours_prop and feh is not None:
            props[self._fe_hours_prop] = {"number": feh}
        page_id = self.find_page_by_tapd_id(tapd_id)
        # Fallback: try find by title equality within the database
        if not page_id and self._title_prop and self.client:
            try:
                title = story.get("name") or story.get("title")
                if title:
                    res = self.client.databases.query(  # type: ignore
                        database_id=self.database_id,
                        filter={
                            "property": self._title_prop,
                            "title": {"equals": str(title)},
                        },
                        page_size=1,
                    )
                    results = res.get("results", [])
                    if results:
                        page_id = results[0]["id"]
            except Exception:
                pass
        try:
            if page_id:
                # Update properties
                self.client.pages.update(page_id=page_id, properties=props)
                # Replace content blocks on the main page (no extra child page)
                if blocks:
                    try:
                        self._replace_children(page_id, blocks)
                    except Exception:
                        # Fallback to append
                        self.client.blocks.children.append(block_id=page_id, children=blocks)
                # Archive legacy detail subpages if present
                self._archive_detail_subpages(page_id)
                return page_id
            # Create new page
            res = self.client.pages.create(
                parent={"database_id": self.database_id},
                properties=props,
                children=[],
            )
            new_page_id = res.get("id")
            if blocks and new_page_id:
                try:
                    self._replace_children(new_page_id, blocks)
                except Exception:
                    self.client.blocks.children.append(block_id=new_page_id, children=blocks)
            return new_page_id or f"created-page-{tapd_id}"
        except Exception:
            # Return dummy id on error to keep pipeline running in skeleton
            return f"error-page-{tapd_id}"

    def create_story_page(self, story: Dict[str, Any], blocks: Optional[list] = None) -> str:
        """Create a Notion page from a TAPD story (no update path)."""
        raw_id = story.get("id")
        tapd_id = str(raw_id) if raw_id is not None else ""
        if not tapd_id or tapd_id in {"", "None", "unknown"}:
            return "skip-no-id"
        if not self.client:
            return f"dummy-page-{tapd_id}"
        props: Dict[str, Any] = {}
        title = story.get("name") or story.get("title") or f"TAPD {tapd_id}"
        if not self._title_prop and self.client:
            try:
                self._inspect_database()
            except Exception:
                pass
        if self._title_prop:
            props[self._title_prop] = {"title": [{"text": {"content": title}}]}
        else:
            return "skip-missing-title"
        status_val = self._status_from_story(story)
        if self._status_prop and status_val:
            props[self._status_prop] = {"select": {"name": str(status_val)}}
        if self._id_prop:
            # Respect actual property type (rich_text/title/number/url) to avoid API errors
            id_payload = self._build_id_prop_payload(str(tapd_id))
            if id_payload is not None:
                props[self._id_prop] = id_payload
        if self._desc_prop:
            desc = story.get("description") or ""
            if isinstance(desc, str) and ("<" in desc and ">" in desc):
                desc = self._html_to_text(desc)
            summary = f"TAPD_ID: {tapd_id}\n" + (desc[:1800] if isinstance(desc, str) else str(desc))
            props[self._desc_prop] = {"rich_text": [{"text": {"content": summary}}]}
        # Module label (best-effort)
        mod_name = (
            story.get("module")
            or story.get("module_name")
            or story.get("category")
            or story.get("module_path")
            or story.get("module_label")
        )
        if self._module_prop and mod_name:
            if self._module_is_multi:
                props[self._module_prop] = {"multi_select": [{"name": str(mod_name)}]}
            else:
                props[self._module_prop] = {"select": {"name": str(mod_name)}}
        # Owner/Assignees
        if self._owner_prop:
            owners = []
            for key in ("owner", "assignee", "current_owner", "owners"):
                v = story.get(key)
                if v is None:
                    continue
                if isinstance(v, (list, tuple, set)):
                    owners.extend([str(x) for x in v if str(x)])
                else:
                    owners.append(str(v))
            owners = [o for i, o in enumerate(owners) if o and o not in owners[:i]]
            if owners:
                if getattr(self, "_owner_is_people", False):
                    try:
                        people = []
                        cache = getattr(self, "_people_cache_by_name", None)
                        if cache is None:
                            cache = {}
                            setattr(self, "_people_cache_by_name", cache)
                            res = self.client.users.list()  # type: ignore
                            for u in res.get("results", []):
                                nm = (u.get("name") or "").strip()
                                uid = u.get("id")
                                if nm and uid:
                                    cache[nm.lower()] = uid
                        for nm in owners:
                            uid = cache.get(str(nm).lower())
                            if uid:
                                people.append({"id": uid})
                        if people:
                            props[self._owner_prop] = {"people": people}
                    except Exception:
                        pass
                else:
                    props[self._owner_prop] = {"multi_select": [{"name": o} for o in owners]}
        # Priority
        if self._priority_prop:
            pr = story.get("priority") or story.get("priority_label") or story.get("priority_name")
            if pr is not None and str(pr).strip():
                props[self._priority_prop] = {"select": {"name": str(pr)}}
        # Planned dates & FE hours
        start_d, end_d = self._extract_dates(story)
        if self._planned_range_prop and (start_d or end_d):
            props[self._planned_range_prop] = {"date": {"start": start_d or end_d, "end": (end_d if start_d else None)}}
        else:
            if self._planned_start_prop and start_d:
                props[self._planned_start_prop] = {"date": {"start": start_d}}
            if self._planned_end_prop and end_d:
                props[self._planned_end_prop] = {"date": {"start": end_d}}
        feh = self._extract_fe_hours(story)
        if self._fe_hours_prop and feh is not None:
            props[self._fe_hours_prop] = {"number": feh}
        try:
            res = self.client.pages.create(  # type: ignore
                parent={"database_id": self.database_id},
                properties=props,
                children=[],
            )
            new_page_id = res.get("id")
            if blocks and new_page_id:
                try:
                    self._replace_children(new_page_id, blocks)
                except Exception:
                    self.client.blocks.children.append(block_id=new_page_id, children=blocks)
            return new_page_id or f"created-page-{tapd_id}"
        except Exception:
            return f"error-page-{tapd_id}"

    def _replace_children(self, page_id: str, children: list) -> None:
        """Replace page children blocks by archiving existing and appending new ones.

        Notion doesn't support direct replace, so we archive current child blocks
        then append new blocks.
        """
        if not self.client:
            return
        # Archive existing children
        try:
            start_cursor = None
            while True:
                kwargs = {"block_id": page_id}
                if start_cursor:
                    kwargs["start_cursor"] = start_cursor
                res = self.client.blocks.children.list(**kwargs)  # type: ignore
                for blk in res.get("results", []):
                    bid = blk.get("id")
                    try:
                        self.client.blocks.update(block_id=bid, archived=True)  # type: ignore
                    except Exception:
                        pass
                if not res.get("has_more"):
                    break
                start_cursor = res.get("next_cursor")
        except Exception:
            pass
        # Append new children (cap to 100 to stay within limits)
        if children:
            self.client.blocks.children.append(block_id=page_id, children=children[:100])
