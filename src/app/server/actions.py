"""Predefined CLI actions exposed by the job API."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable

from .jobs import ActionDefinition, ActionOption

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "scripts"

__all__ = ["ACTIONS", "list_action_payloads"]

ACTIONS: Dict[str, ActionDefinition] = {
    "pull-to-notion": ActionDefinition(
        id="pull-to-notion",
        title="同步需求到 Notion",
        description="从 TAPD 拉取需求并写入 Notion 数据库。",
        command=("python3", str(SCRIPTS_DIR / "sync")),
        default_args=("--execute",),
        options=(
            ActionOption(
                id="owner",
                label="--owner 前端",
                args=("--owner", "前端"),
                description="仅同步负责人包含“前端”的需求。",
            ),
            ActionOption(
                id="current-iteration",
                label="--current-iteration",
                args=("--current-iteration",),
                description="仅同步当前迭代的需求（推荐开启）。",
                default_selected=True,
            ),
            ActionOption(
                id="execute",
                label="--execute",
                args=("--execute",),
                description="实际写入 Notion（默认 dry-run）。",
                default_selected=True,
            ),
            ActionOption(
                id="modules",
                label="--by-modules",
                args=("--by-modules",),
                description="按模块遍历工作空间并分别执行同步。",
            ),
            ActionOption(
                id="wipe-first",
                label="--wipe-first",
                args=("--wipe-first",),
                description="写入前清空 Notion 数据库（归档旧记录）并全量重建。",
            ),
            ActionOption(
                id="insert-only",
                label="--insert-only",
                args=("--insert-only",),
                description="仅新增 Notion 中不存在的需求（按 TAPD_ID 判断）。",
            ),
            ActionOption(
                id="no-post-update",
                label="--no-post-update",
                args=("--no-post-update",),
                description="禁止同步完成后的自动更新步骤。",
            ),
        ),
        hint="需要前置好 TAPD/Notion 凭证，若仅试运行可去掉 --execute。",
    ),
    "update-requirements": ActionDefinition(
        id="update-requirements",
        title="更新需求状态",
        description="刷新 Notion 中已存在需求的字段，保持与 TAPD 一致。",
        command=("python3", str(SCRIPTS_DIR / "update")),
        default_args=(),
        options=(
            ActionOption(
                id="execute",
                label="--execute",
                args=("--execute",),
                description="实际写入 Notion（默认 dry-run）。",
                default_selected=True,
            ),
            ActionOption(
                id="current-iteration",
                label="--current-iteration",
                args=("--current-iteration",),
                description="仅更新当前迭代的需求（推荐开启）。",
                default_selected=True,
            ),
            ActionOption(
                id="from-notion",
                label="--from-notion",
                args=("--from-notion",),
                description="仅根据 Notion 已有页面执行更新（不会访问 TAPD）。",
            ),
            ActionOption(
                id="create-missing",
                label="--create-missing",
                args=("--create-missing",),
                description="在按 ID 更新时，缺失页面将自动创建。",
            ),
            ActionOption(
                id="analyze",
                label="--analyze",
                args=("--analyze",),
                description="更新时重新分析需求内容（默认跳过）。",
            ),
        ),
        hint="默认更新当前迭代；想要 dry-run 可去掉 --execute。",
    ),
    "generate-testcases": ActionDefinition(
        id="generate-testcases",
        title="生成测试用例",
        description="按需求生成测试用例并输出 XMind 附件。",
        command=("python3", str(SCRIPTS_DIR / "testflow")),
        default_args=(),
        options=(
            ActionOption(
                id="execute",
                label="--execute",
                args=("--execute",),
                description="实际写入附件到磁盘（默认 dry-run）。",
                default_selected=True,
            ),
            ActionOption(
                id="ack",
                label="--ack web-ui",
                args=("--ack", "web-ui"),
                description="执行模式下的确认令牌，默认使用 web-ui。",
                default_selected=True,
            ),
            ActionOption(
                id="current-iteration",
                label="--current-iteration",
                args=("--current-iteration",),
                description="仅处理当前迭代的需求（推荐开启）。",
                default_selected=True,
            ),
            ActionOption(
                id="send-mail",
                label="--send-mail",
                args=("--send-mail",),
                description="生成附件后通过邮件发送（需配置邮件服务和 --ack-mail）。",
            ),
        ),
        hint="生成文件保存在 testflow 输出目录；如需邮件发送请手动加 --send-mail。",
    ),
    "debug-notion": ActionDefinition(
        id="debug-notion",
        title="Notion 诊断",
        description="检查 Notion 数据库中的 TAPD_ID 映射与缺失页面。",
        command=("python3", str(SCRIPTS_DIR / "notion_debug.py")),
        default_args=(),
        options=(
            ActionOption(
                id="show-props",
                label="--show-props",
                args=("--show-props",),
                description="打印当前识别到的 Notion 属性信息。",
                default_selected=True,
            ),
            ActionOption(
                id="list-missing",
                label="--list-missing",
                args=("--list-missing",),
                description="列出数据库中 TAPD_ID 为空的页面。",
                default_selected=True,
            ),
            ActionOption(
                id="limit-20",
                label="--limit 20",
                args=("--limit", "20"),
                description="限制缺失列表的最大条数（默认 20）。",
                default_selected=True,
            ),
        ),
        hint="可直接在页面选择需求 ID 触发排查；若未选择，可用 NOTION_DEBUG_IDS 环境变量兜底。",
    ),
}


def list_action_payloads() -> Iterable[Dict[str, object]]:
    for action in ACTIONS.values():
        yield {
            "id": action.id,
            "title": action.title,
            "description": action.description,
            "defaultArgs": list(action.default_args),
            "commandPreview": action.display_command(),
            "hint": action.hint,
            "options": [
                {
                    "id": option.id,
                    "label": option.label,
                    "args": list(option.args),
                    "description": option.description,
                    "defaultSelected": option.default_selected,
                }
                for option in action.options
            ],
        }
