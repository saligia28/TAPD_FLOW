#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List, Optional

REPO_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SRC_DIR = os.path.join(REPO_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from core.config import load_config  # type: ignore
from integrations.notion import NotionWrapper  # type: ignore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Notion TAPD_ID diagnostics helper")
    parser.add_argument("--show-props", action="store_true", help="打印 Notion 属性识别结果")
    parser.add_argument("--ids", default="", help="以逗号分隔的 TAPD 需求 ID 列表用于排查")
    parser.add_argument("--file", default=None, help="从文件读取需求 ID（每行一个）")
    parser.add_argument("--list-missing", action="store_true", help="列出 Notion 中 TAPD_ID 为空的页面")
    parser.add_argument("--limit", type=int, default=20, help="--list-missing 时最多展示的页面数")
    return parser.parse_args()


def collect_ids(args: argparse.Namespace) -> List[str]:
    ids: List[str] = []
    if args.ids:
        ids.extend([s.strip() for s in str(args.ids).split(",") if s.strip()])
    if args.file:
        try:
            with open(args.file, "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s:
                        ids.append(s)
        except Exception as exc:
            print(f"[debug] 读取文件失败: {exc}")
    dedup: List[str] = []
    for sid in ids:
        if sid not in dedup:
            dedup.append(sid)
    return dedup


def property_value(notion: NotionWrapper, props: Dict[str, object], name: Optional[str]) -> str:
    if not name or name not in props:
        return ""
    meta = props.get(name)
    if not isinstance(meta, dict):
        return ""
    ptype = meta.get("type")
    if ptype == "rich_text":
        return notion._extract_plain_text(meta.get("rich_text", []))  # type: ignore[attr-defined]
    if ptype == "title":
        return notion._extract_plain_text(meta.get("title", []))  # type: ignore[attr-defined]
    if ptype == "number":
        val = meta.get("number")
        return "" if val is None else str(val)
    if ptype == "url":
        val = meta.get("url")
        return str(val) if val else ""
    return ""


def main() -> None:
    args = parse_args()
    cfg = load_config()
    notion = NotionWrapper(cfg.notion_token or "", cfg.notion_requirement_db_id or "")

    if args.show_props:
        overview = notion.debug_property_overview()
        print("[debug] Notion 属性识别：")
        for key, value in overview.items():
            print(f"  - {key}: {value}")

    ids = collect_ids(args)
    if not ids:
        env_ids = os.getenv("NOTION_DEBUG_IDS", "")
        if env_ids:
            ids = [s.strip() for s in env_ids.split(",") if s.strip()]
    if not ids:
        env_file = os.getenv("NOTION_DEBUG_IDS_FILE")
        if env_file and os.path.exists(env_file):
            try:
                with open(env_file, "r", encoding="utf-8") as f:
                    ids = [line.strip() for line in f if line.strip()]
            except Exception as exc:
                print(f"[debug] 读取 NOTION_DEBUG_IDS_FILE 失败: {exc}")

    if ids:
        existing_idx = notion.existing_index()
        print(f"[debug] 待排查 ID 数量：{len(ids)}")
        for sid in ids:
            diag = notion.debug_lookup(sid)
            idx_hit = existing_idx.get(sid)
            print(f"--- TAPD_ID={sid} ---")
            print(f"  id_prop={diag.get('id_prop')} ({diag.get('id_prop_type')})")
            if diag.get("id_query_filter"):
                print(f"  id_query_filter={diag['id_query_filter']}")
            if diag.get("id_query_error"):
                print(f"  id_query_error={diag['id_query_error']}")
            else:
                print(f"  id_query_count={diag.get('id_query_count')} page={diag.get('id_query_page')}")
            if diag.get("desc_prop"):
                if diag.get("desc_query_error"):
                    print(f"  desc_query_error={diag['desc_query_error']}")
                else:
                    print(f"  desc_query_count={diag.get('desc_query_count')} page={diag.get('desc_query_page')}")
            print(f"  existing_index_page={idx_hit}")

    if args.list_missing:
        missing = []
        total = 0
        for page in notion.iter_database_pages():
            total += 1
            props = page.get("properties", {})
            if not isinstance(props, dict):
                continue
            tapd_val = property_value(notion, props, notion._id_prop)  # type: ignore[attr-defined]
            if tapd_val.strip():
                continue
            title = property_value(notion, props, notion._title_prop)  # type: ignore[attr-defined]
            desc_sample = ""
            if notion._desc_prop and notion._desc_prop in props:  # type: ignore[attr-defined]
                desc_sample = property_value(notion, props, notion._desc_prop)[:80]  # type: ignore[attr-defined]
            missing.append((page.get("id"), title, desc_sample))
            if args.limit and len(missing) >= args.limit:
                break
        print(f"[debug] 数据库页面总数估计：{total}")
        if not missing:
            print("[debug] 未发现 TAPD_ID 为空的页面。")
        else:
            print(f"[debug] TAPD_ID 为空的页面（最多 {args.limit} 条）：")
            for pid, title, desc in missing:
                print(f"  - page_id={pid} title={title} desc_sample={desc}")


if __name__ == "__main__":
    main()
