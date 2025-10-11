from __future__ import annotations

import json
from typing import TYPE_CHECKING, Optional

import requests


DEFAULT_TIMEOUT = 8.0


if TYPE_CHECKING:  # pragma: no cover - import for typing only
    from sync import SyncResult, UpdateAllResult


def send_wecom_markdown(webhook_url: Optional[str], content: str, *, timeout: float = DEFAULT_TIMEOUT) -> bool:
    """Send a markdown message to WeCom robot.

    Returns True when the message is accepted by WeCom, False otherwise.
    Failures are logged to stdout but do not raise, allowing callers to retry
    或继续后续步骤而不中断流水线。
    """

    if not webhook_url:
        return True
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=timeout)
    except Exception as exc:  # pragma: no cover - network failure branch
        print(f"[notify] send failed: {exc}")
        return False
    if resp.status_code != 200:
        print(f"[notify] unexpected status: {resp.status_code} body={resp.text[:200]}")
        return False
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        print("[notify] non-JSON response from WeCom webhook")
        return False
    if data.get("errcode") != 0:
        print(f"[notify] WeCom error: {data}")
        return False
    return True


def format_sync_markdown(
    stats: "SyncResult",
    *,
    owner: Optional[str],
    creator: Optional[str],
    current_iteration: bool,
    since: Optional[str],
    full: bool,
    by_modules: bool,
) -> str:
    lines: list[str] = []
    header = "**拉取完成**"
    if stats.dry_run:
        header += " (dry-run)"
    if by_modules:
        header += " [按模块]"
    lines.append(header)
    scope_bits: list[str] = []
    if full:
        scope_bits.append("全量")
    else:
        scope_bits.append(f"since={since or 'last'}")
    if owner:
        scope_bits.append(f"owner={owner}")
    if creator:
        scope_bits.append(f"creator={creator}")
    scope_bits.append(f"当前迭代={'是' if current_iteration else '否'}")
    lines.append(f"- 范围：{' | '.join(scope_bits)}")
    lines.append(f"- 总数：{stats.total}")
    lines.append(f"- 新增：{stats.created}")
    lines.append(f"- 已存在：{stats.existing}")
    if stats.skipped:
        lines.append(f"- 跳过：{stats.skipped}")
    lines.append(f"- 耗时：{_format_duration(stats.duration)}")
    return "\n".join(lines)


def format_update_markdown(
    stats: "UpdateAllResult",
    *,
    owner: Optional[str],
    creator: Optional[str],
    current_iteration: bool,
) -> str:
    lines: list[str] = []
    header = "**更新完成**"
    if stats.dry_run:
        header += " (dry-run)"
    lines.append(header)
    scope_bits: list[str] = []
    if owner:
        scope_bits.append(f"owner={owner}")
    if creator:
        scope_bits.append(f"creator={creator}")
    scope_bits.append(f"当前迭代={'是' if current_iteration else '否'}")
    lines.append(f"- 范围：{' | '.join(scope_bits)}")
    lines.append(f"- 总扫描：{stats.scanned}")
    lines.append(f"- 更新：{stats.updated}")
    if stats.skipped:
        lines.append(f"- 跳过：{stats.skipped}")
    lines.append(f"- 耗时：{_format_duration(stats.duration)}")
    return "\n".join(lines)


def _format_duration(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    if seconds < 60:
        return f"{seconds:.1f}秒"
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    if minutes >= 60:
        hours = minutes // 60
        minutes = minutes % 60
        return f"{hours}小时{minutes}分{remainder:.1f}秒"
    return f"{minutes}分{remainder:.1f}秒"
