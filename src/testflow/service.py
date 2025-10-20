from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from core.config import Config
from integrations.tapd import TAPDClient

from .exporter import export_suite_to_xmind
from .llm import LLMGenerationError, build_fallback_cases, generate_test_cases
from .mailer import send_mail
from .models import (
    GeneratedAttachment,
    MailJob,
    TestFlowOptions,
    TestFlowResult,
    TesterSuite,
)
from .testers import TesterRegistry, extract_story_testers, load_testers


class TestFlowAckError(RuntimeError):
    pass


def run_testflow(cfg: Config, options: TestFlowOptions) -> TestFlowResult:
    _ensure_ack(options)
    started = datetime.now(timezone.utc)
    registry = load_testers(cfg.testflow_testers_path)
    tapd = _init_tapd(cfg)
    owner_tokens = _split_option(options.owner or cfg.tapd_only_owner)
    creator_filter = options.creator or cfg.tapd_only_creator
    iteration_id, iteration_key = _detect_iteration(cfg, tapd, options.current_iteration)
    fetched_stories = _fetch_stories(
        tapd,
        workspace_id=cfg.tapd_workspace_id or "",
        owner_substrings=owner_tokens,
        creator=creator_filter,
        iteration_filter=(iteration_key, iteration_id) if iteration_key and iteration_id else None,
        limit=options.limit,
    )
    result = generate_testflow_for_stories(cfg, fetched_stories, execute=options.execute, registry=registry)
    attachments = result.attachments
    if options.execute:
        for attachment in attachments:
            print(f"[testflow] generated {attachment.file_path} cases={attachment.case_count}")
    mail_results: Dict[str, str] = {}
    if options.send_mail and result.suites:
        if not options.ack_mail:
            raise TestFlowAckError("邮件发送需要 ack-mail, 使用 --ack-mail 明确确认")
        attachments_map = _group_attachments_by_tester(attachments, options.execute)
        for suite in result.suites:
            files = attachments_map.get(suite.tester.email, [])
            job = _build_mail_job(suite, files)
            send_result = send_mail(job, cfg)
            mail_results[suite.tester.email] = send_result.message
            print(f"[testflow] mail -> {suite.tester.email} status={send_result.message}")
    finished = datetime.now(timezone.utc)
    result.mails = mail_results
    result.started_at = started
    result.finished_at = finished
    return result


def generate_testflow_for_stories(
    cfg: Config,
    stories: List[dict],
    *,
    execute: bool = True,
    registry: Optional[TesterRegistry] = None,
) -> TestFlowResult:
    started = datetime.now(timezone.utc)
    registry = registry or load_testers(cfg.testflow_testers_path)
    suites = _generate_cases(cfg, registry, stories)
    attachments: List[GeneratedAttachment] = []
    if execute and suites:
        output_dir = Path(cfg.testflow_output_dir)
        now = datetime.now(timezone.utc)
        for suite in suites:
            attachment = export_suite_to_xmind(suite, output_dir, now)
            attachments.append(attachment)
    finished = datetime.now(timezone.utc)
    total_cases = sum(len(suite.cases) for suite in suites)
    return TestFlowResult(
        total_stories=len(stories),
        total_cases=total_cases,
        suites=suites,
        attachments=attachments,
        started_at=started,
        finished_at=finished,
    )


def _ensure_ack(options: TestFlowOptions) -> None:
    if options.execute and not options.ack_pull:
        raise TestFlowAckError("需要 --ack 表示已知悉拉取风险")


def _init_tapd(cfg: Config) -> TAPDClient:
    return TAPDClient(
        cfg.tapd_api_key or "",
        cfg.tapd_api_secret or "",
        cfg.tapd_workspace_id or "",
        api_user=cfg.tapd_api_user,
        api_password=cfg.tapd_api_password,
        token=cfg.tapd_token,
        api_base=cfg.tapd_api_base,
        stories_path=cfg.tapd_stories_path,
        modules_path=cfg.tapd_modules_path,
        iterations_path=cfg.tapd_iterations_path,
        story_tags_path=getattr(cfg, "tapd_story_tags_path", "/story_tags"),
        story_attachments_path=getattr(cfg, "tapd_story_attachments_path", "/story_attachments"),
        story_comments_path=getattr(cfg, "tapd_story_comments_path", "/story_comments"),
    )


def _detect_iteration(
    cfg: Config,
    tapd: TAPDClient,
    current_iteration_flag: bool,
) -> Tuple[Optional[str], Optional[str]]:
    if not (current_iteration_flag or getattr(cfg, "tapd_use_current_iteration", False)):
        return None, None
    iteration = tapd.get_current_iteration()
    if not iteration:
        return None, None
    iteration_id = str(
        iteration.get("id")
        or iteration.get("iteration_id")
        or iteration.get("iterationid")
        or ""
    ).strip()
    if not iteration_id:
        return None, None
    candidates = getattr(cfg, "tapd_filter_iteration_id_keys", []) or ["iteration_id"]
    return iteration_id, candidates[0]


def _fetch_stories(
    tapd: TAPDClient,
    *,
    workspace_id: str,
    owner_substrings: List[str],
    creator: Optional[str],
    iteration_filter: Optional[Tuple[str, str]],
    limit: Optional[int],
) -> List[dict]:
    filters: Dict[str, object] = {"workspace_id": workspace_id}
    if creator:
        filters["creator"] = creator
    if iteration_filter:
        key, value = iteration_filter
        filters[key] = value
    collected: List[dict] = []
    for story in tapd.list_stories(filters=filters):
        if owner_substrings and not _owner_matches(story, owner_substrings):
            continue
        collected.append(story)
        if limit and len(collected) >= limit:
            break
    print(f"[testflow] fetched stories={len(collected)}")
    return collected


def _generate_cases(
    cfg: Config,
    registry: TesterRegistry,
    stories: List[dict],
) -> List[TesterSuite]:
    suites_by_email: Dict[str, TesterSuite] = {}
    for story in stories:
        tester_tokens = extract_story_testers(story)
        assigned_contacts = registry.resolve_tokens(tester_tokens)
        if not assigned_contacts:
            assigned_contacts = [registry.default()]
        tester_names = [contact.name for contact in assigned_contacts]
        cases = []
        fallback_reason = "LLM 生成失败"
        if getattr(cfg, "testflow_use_llm", False):
            try:
                cases, _ = generate_test_cases(story, tester_names, registry, cfg)
            except LLMGenerationError as exc:
                fallback_reason = f"LLM 调用失败: {exc}"
                print(f"[testflow] LLM failure for story {story.get('id')}: {exc}")
        else:
            fallback_reason = "未启用 LLM 生成"
            print("[testflow] LLM generation disabled; set TESTFLOW_USE_LLM=1 to enable.")
        if not cases:
            cases = build_fallback_cases(story, tester_names, registry, reason=fallback_reason)
        for case in cases:
            contact = registry.lookup(case.tester) or assigned_contacts[0]
            suite = suites_by_email.setdefault(
                contact.email,
                TesterSuite(tester=contact),
            )
            case.tester = contact.name
            suite.add_case(case)
    return list(suites_by_email.values())


def _group_attachments_by_tester(
    attachments: List[GeneratedAttachment],
    executed: bool,
) -> Dict[str, List[Path]]:
    mapping: Dict[str, List[Path]] = defaultdict(list)
    if not executed:
        return mapping
    for attachment in attachments:
        mapping[attachment.tester.email].append(attachment.file_path)
    return mapping


def _build_mail_job(suite: TesterSuite, attachments: List[Path]) -> MailJob:
    case_count = len(suite.cases)
    unique_stories = {case.story_id: case.story_title for case in suite.cases}
    lines = [
        f"你好 {suite.tester.name}，",
        "以下为自动生成的测试用例，供快速评审：",
        f"- 用例总数：{case_count}",
        "- 覆盖需求：",
    ]
    for sid, title in unique_stories.items():
        lines.append(f"  • {title} ({sid})")
    lines.append("\n如需调整，请直接在 XMind 中补充。")
    body = "\n".join(lines)
    subject = f"[TestFlow] {suite.tester.name} 测试用例 {case_count} 条"
    return MailJob(contact=suite.tester, subject=subject, body=body, attachments=attachments)


def _split_option(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _owner_matches(story: dict, substrings: Iterable[str]) -> bool:
    if not substrings:
        return True
    haystack = " ".join(_owner_tokens(story))
    for sub in substrings:
        if sub and sub in haystack:
            return True
    return False


def _owner_tokens(story: dict) -> List[str]:
    tokens: List[str] = []
    for key in ("owner", "assignee", "负责人", "处理人", "当前处理人"):
        value = story.get(key)
        if isinstance(value, (list, tuple)):
            tokens.extend(str(v).strip() for v in value if v)
        elif value:
            tokens.append(str(value).strip())
    return [t for t in tokens if t]
