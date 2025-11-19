from __future__ import annotations
import argparse
from typing import Optional

from core.config import load_config, validate_config
from services.sync import run_sync
from integrations.tapd import TAPDClient
from testflow import TestFlowOptions, run_testflow


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TAPD ↔ Notion sync CLI (skeleton)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync", help="同步 TAPD 需求至 Notion")
    p_sync.add_argument("--full", action="store_true", help="全量初始化")
    p_sync.add_argument("--since", default="last", help="增量边界：'last' 或 ISO 时间戳")
    p_sync.add_argument("--execute", action="store_true", help="实际写入（默认 dry-run）")
    p_sync.add_argument("--owner", default=None, help="仅同步该负责人（显示名），覆盖 TAPD_ONLY_OWNER")
    p_sync.add_argument("--creator", default=None, help="仅同步该创建人（显示名），覆盖 TAPD_ONLY_CREATOR")
    p_sync.add_argument("--by-modules", action="store_true", help="按模块分组：遍历工作空间所有模块并拉取其需求")
    p_sync.add_argument("--wipe-first", action="store_true", help="写入前清空 Notion 数据库（归档旧记录）并全量重建")
    p_sync.add_argument("--insert-only", action="store_true", help="仅新增 Notion 中不存在的需求（按 TAPD_ID 判断），已存在的不更新")
    p_sync.add_argument("--current-iteration", action="store_true", help="仅同步当前迭代的需求")
    p_sync.add_argument("--progress", action="store_true", help="输出详细的阶段进度日志（fetch→analyze→notion）")
    # Risk ack removed; writes are allowed when --execute is used

    p_an = sub.add_parser("analyze", help="仅进行内容分析，不写 Notion（演示）")
    p_an.add_argument("--text", default="", help="输入文本（留空则示范文本）")

    p_auth = sub.add_parser("auth-test", help="测试 TAPD API 基础认证是否可用")

    p_mod = sub.add_parser("modules", help="列出当前工作空间的模块")

    p_wipe = sub.add_parser("wipe-notion", help="仅清空 Notion 数据库（归档全部页面）后退出")
    p_wipe.add_argument("--execute", action="store_true", help="实际执行清空；未加此参数只打印提示")
    p_wipe.add_argument("--deep", action="store_true", help="递归清空子页面（child_page），避免残留子页面内容")
    # Risk ack removed for wipe-notion as well

    p_stat = sub.add_parser("status-sync", help="同步 TAPD 状态枚举到 Notion 数据库的状态 select 选项")
    p_stat.add_argument("--pages", type=int, default=10, help="抽样页数（默认 10 页）")
    p_stat.add_argument("--limit", type=int, default=50, help="每页数量（默认 50）")
    p_stat.add_argument("--current-iteration", action="store_true", help="仅抽样当前迭代以收集状态")

    p_upd = sub.add_parser("update", help="按 TAPD 需求 ID 手动更新 Notion 页面（仅手动触发）")
    p_upd.add_argument("--ids", default="", help="以逗号分隔的需求 ID 列表")
    p_upd.add_argument("--id", action="append", default=[], help="重复传多个 --id 用于指定需求 ID")
    p_upd.add_argument("--file", default=None, help="从文件读取需求 ID（每行一个）")
    p_upd.add_argument("--execute", action="store_true", help="实际更新（默认 dry-run）")
    p_upd.add_argument("--create-missing", action="store_true", help="如 Notion 中不存在对应页面则创建新页面")
    p_upd.add_argument("--analyze", action="store_true", help="是否重新分析需求内容（默认不分析）")

    p_upda = sub.add_parser("update-all", help="批量更新（只更新，若 Notion 无对应页则跳过）")
    p_upda.add_argument("--owner", default=None, help="仅更新负责人包含该关键字（可逗号分隔多个）")
    p_upda.add_argument("--creator", default=None, help="仅更新该创建人（显示名）")
    p_upda.add_argument("--current-iteration", action="store_true", help="仅当前迭代")
    p_upda.add_argument("--execute", action="store_true", help="实际更新（默认 dry-run）")
    p_upda.add_argument("--analyze", action="store_true", help="是否重新分析需求内容（默认不分析）")
    # Risk ack removed

    p_updn = sub.add_parser("update-from-notion", help="从 Notion 现有页面出发按 TAPD_ID 逐条更新（只更新）")
    p_updn.add_argument("--limit", type=int, default=None, help="最多更新多少条（默认全部）")
    p_updn.add_argument("--execute", action="store_true", help="实际更新（默认 dry-run）")
    p_updn.add_argument("--analyze", action="store_true", help="是否重新分析需求内容（默认不分析）")
    # Risk ack removed

    p_exp = sub.add_parser("export", help="导出 Notion 中已处理的需求数据（MCP 契约 JSON）")
    p_exp.add_argument("--owner", default=None, help="负责人包含（逗号分隔多个）")
    p_exp.add_argument("--current-iteration", action="store_true", help="仅当前迭代（将通过 TAPD 校验）")
    p_exp.add_argument("--module", default=None, help="模块名称包含（可选）")
    p_exp.add_argument("--limit", type=int, default=50, help="每页数量（默认 50）")
    p_exp.add_argument("--cursor", default=None, help="分页游标（Notion next_cursor）")
    p_exp.add_argument("--out", default=None, help="输出文件路径（省略则打印到 stdout）")

    p_tf = sub.add_parser("testflow", help="生成测试用例并按测试人员导出 XMind")
    p_tf.add_argument("--owner", default=None, help="仅处理该负责人（逗号分隔）")
    p_tf.add_argument("--creator", default=None, help="仅处理该创建人")
    p_tf.add_argument("--limit", type=int, default=None, help="最多处理需求数量")
    p_tf.add_argument("--current-iteration", action="store_true", help="仅当前迭代")
    p_tf.add_argument("--execute", action="store_true", help="实际生成 XMind 文件")
    p_tf.add_argument("--send-mail", action="store_true", help="为测试人员发送邮件")
    p_tf.add_argument("--ack", dest="ack", default=None, help="危险操作确认文案，如：我已知悉：拉取是严重危险操作")
    p_tf.add_argument("--ack-mail", default=None, help="邮件发送确认文案，如：邮件发送风险已知悉")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.cmd == "sync":
        cfg = load_config()
        # No global write guard; writes happen when --execute is set
        missing = validate_config(cfg)
        if missing:
            print(f"[warn] 缺少配置：{missing}（dry-run 仍可执行骨架流程）")
        since = None if args.full else (None if args.since == "last" else args.since)
        # No risk ack required
        if args.by_modules:
            from services.sync import run_sync_by_modules
            run_sync_by_modules(
                cfg,
                full=args.full,
                since=since,
                dry_run=(not args.execute),
                owner=args.owner,
                creator=args.creator,
                wipe_first=args.wipe_first,
                insert_only=args.insert_only,
                current_iteration=args.current_iteration,
                progress=args.progress,
            )
        else:
            run_sync(
                cfg,
                full=args.full,
                since=since,
                dry_run=(not args.execute),
                owner=args.owner,
                creator=args.creator,
                wipe_first=args.wipe_first,
                insert_only=args.insert_only,
                current_iteration=args.current_iteration,
                progress=args.progress,
            )
    elif args.cmd == "analyze":
        from analyzer.rule_based import analyze
        text = args.text or "作为用户，我希望能够从 TAPD 同步需求到 Notion；验收：在 Notion 中可以看到需求、状态与功能点。"
        res = analyze(text)
        print(res)
    elif args.cmd == "auth-test":
        cfg = load_config()
        tapd = TAPDClient(
            cfg.tapd_api_key or "",
            cfg.tapd_api_secret or "",
            cfg.tapd_workspace_id or "",
            api_user=cfg.tapd_api_user,
            api_password=cfg.tapd_api_password,
            token=cfg.tapd_token,
            api_base=cfg.tapd_api_base,
            stories_path=cfg.tapd_stories_path,
        )
        res = tapd.test_auth()
        print(res)
    elif args.cmd == "modules":
        cfg = load_config()
        tapd = TAPDClient(
            cfg.tapd_api_key or "",
            cfg.tapd_api_secret or "",
            cfg.tapd_workspace_id or "",
            api_user=cfg.tapd_api_user,
            api_password=cfg.tapd_api_password,
            token=cfg.tapd_token,
            api_base=cfg.tapd_api_base,
            stories_path=cfg.tapd_stories_path,
            modules_path=cfg.tapd_modules_path,
        )
        for i, m in enumerate(tapd.list_modules(), 1):
            print(f"{i:02d}. {m.get('name')} (id={m.get('id')})")
    elif args.cmd == "wipe-notion":
        cfg = load_config()
        if not args.execute:
            print("[wipe-notion] 干跑模式：未执行。若要实际清空，请追加 --execute")
            return
        from integrations.notion import NotionWrapper
        notion = NotionWrapper(cfg.notion_token or "", cfg.notion_requirement_db_id or "")
        # No ack prompts
        cleared = notion.clear_database(deep=args.deep)
        print(f"[wipe-notion] cleared pages={cleared} | deep={args.deep}")
    elif args.cmd == "status-sync":
        cfg = load_config()
        from integrations.notion import NotionWrapper
        from integrations.tapd import TAPDClient
        tapd = TAPDClient(
            cfg.tapd_api_key or "",
            cfg.tapd_api_secret or "",
            cfg.tapd_workspace_id or "",
            api_user=cfg.tapd_api_user,
            api_password=cfg.tapd_api_password,
            token=cfg.tapd_token,
            api_base=cfg.tapd_api_base,
            stories_path=cfg.tapd_stories_path,
        )
        notion = NotionWrapper(cfg.notion_token or "", cfg.notion_requirement_db_id or "")
        base_filters = None
        if args.current_iteration:
            cur = tapd.get_current_iteration()
            if cur:
                it_id = cur.get('id') or cur.get('iteration_id')
                if it_id:
                    ks = getattr(cfg, 'tapd_filter_iteration_id_keys', []) or ['iteration_id']
                    base_filters = {ks[0]: it_id}
        cnt = notion.sync_status_options_from_tapd(tapd, sample_pages=args.pages, page_size=args.limit, base_filters=base_filters)
        print(f"[status-sync] collected={cnt} and updated Notion status options")
    elif args.cmd == "update":
        cfg = load_config()
        # No write guards / acks
        # collect ids
        ids: list[str] = []
        if args.ids:
            ids.extend([s.strip() for s in str(args.ids).split(',') if s.strip()])
        if args.id:
            for x in args.id:
                ids.append(str(x).strip())
        if args.file:
            try:
                with open(args.file, 'r', encoding='utf-8') as f:
                    for line in f:
                        s = line.strip()
                        if s:
                            ids.append(s)
            except Exception as e:
                print(f"[update] 读取文件失败: {e}")
        # de-dupe
        ids = [x for i,x in enumerate(ids) if x and x not in ids[:i]]
        if not ids:
            print("[update] 未提供任何需求 ID；请使用 --ids 或 --id 或 --file")
            return
        from services.sync import run_update
        run_update(
            cfg,
            ids,
            dry_run=(not args.execute),
            create_missing=args.create_missing,
            re_analyze=args.analyze,
        )
    elif args.cmd == "update-all":
        cfg = load_config()
        # No write guards / acks
        from services.sync import run_update_all
        run_update_all(
            cfg,
            dry_run=(not args.execute),
            owner=args.owner,
            creator=args.creator,
            current_iteration=args.current_iteration,
            re_analyze=args.analyze,
        )
    elif args.cmd == "update-from-notion":
        cfg = load_config()
        # No write guards / acks
        from services.sync import run_update_from_notion
        run_update_from_notion(
            cfg,
            dry_run=(not args.execute),
            limit=args.limit,
            re_analyze=args.analyze,
        )
    elif args.cmd == "testflow":
        cfg = load_config()
        options = TestFlowOptions(
            owner=args.owner,
            creator=args.creator,
            current_iteration=args.current_iteration,
            limit=args.limit,
            execute=args.execute,
            send_mail=args.send_mail,
            ack_pull=args.ack,
            ack_mail=args.ack_mail,
        )
        result = run_testflow(cfg, options)
        print(f"[testflow] {result.summary()}")
        for attachment in result.attachments:
            print(
                f"  {attachment.tester.name}: {attachment.file_path} "
                f"({attachment.case_count} cases)"
            )
        if result.mails:
            print("[testflow] mail statuses:")
            for email, status in result.mails.items():
                print(f"  {email}: {status}")
    elif args.cmd == "export":
        cfg = load_config()
        from services.sync import run_export
        res = run_export(
            cfg,
            owner_contains=args.owner,
            current_iteration=args.current_iteration,
            module_contains=args.module,
            limit=args.limit,
            cursor=args.cursor,
        )
        import json, sys
        data = json.dumps(res, ensure_ascii=False, indent=2)
        if args.out:
            with open(args.out, 'w', encoding='utf-8') as f:
                f.write(data)
            print(f"[export] wrote {len(res.get('items', []))} items to {args.out}; next_cursor={res.get('next_cursor')}")
        else:
            sys.stdout.write(data + "\n")


if __name__ == "__main__":
    main()
