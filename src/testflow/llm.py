from __future__ import annotations

import json
import textwrap
from typing import Iterable, List, Optional, Tuple

import requests
from requests import Response

from config import Config
from content import html_to_text

from .models import TestCase
from .testers import TesterRegistry


class LLMGenerationError(RuntimeError):
    pass


def generate_test_cases(
    story: dict,
    tester_names: Iterable[str],
    registry: TesterRegistry,
    cfg: Config,
) -> Tuple[List[TestCase], str]:
    tester_list = list(tester_names)
    prompt = build_prompt(story, tester_list, cfg, registry)
    raw_response = call_ollama(prompt, cfg)
    cases, test_points = parse_response(raw_response, story, registry, tester_list)
    fallback_reason = "LLM 输出为空或格式不正确"
    if test_points:
        overview = TestCase(
            story_id=str(story.get("id") or story.get("story_id") or ""),
            story_title=str(story.get("name") or story.get("title") or "未命名需求"),
            tester=tester_list[0] if tester_list else registry.default().name,
            title="测试点总览",
            summary="AI 建议优先覆盖以下测试点",
            extra={"测试点列表": test_points, "source": "ai-testpoints"},
        )
        cases = [overview, *cases]
    if not cases:
        cases = build_fallback_cases(story, tester_list, registry, reason=fallback_reason)
    return cases, raw_response


def build_prompt(
    story: dict,
    tester_names: Iterable[str],
    cfg: Config,
    registry: TesterRegistry,
) -> str:
    sid = str(story.get("id") or story.get("story_id") or "")
    title = str(story.get("name") or story.get("title") or "未命名需求")
    description = story.get("description") or story.get("description_html") or ""
    if "<" in str(description) and ">" in str(description):
        description = html_to_text(str(description))
    requirements = story.get("acceptance") or story.get("acceptance_criteria") or ""
    modules = story.get("module") or story.get("module_name") or story.get("module_path")
    assigned_testers = list(tester_names)
    testers_line = ", ".join(assigned_testers) or registry.default().name
    template = (
        cfg.testflow_prompt_path and _read_prompt_template(cfg.testflow_prompt_path)
    )
    if template:
        base_prompt = template
    else:
        base_prompt = DEFAULT_PROMPT_TEMPLATE
    return base_prompt.format(
        story_id=sid,
        story_title=title,
        story_description=description.strip(),
        acceptance_criteria=(requirements or ""),
        module=modules or "",
        testers=testers_line,
    )


def call_ollama(prompt: str, cfg: Config) -> str:
    url = cfg.ollama_host.rstrip("/") + "/api/generate"
    payload = {
        "model": cfg.ollama_model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": cfg.ollama_temperature,
        },
    }
    last_error = ""
    for attempt in range(1, cfg.ollama_max_retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=cfg.ollama_timeout)
            resp.raise_for_status()
            return _collect_response_text(resp)
        except Exception as exc:  # pragma: no cover - network failure
            last_error = str(exc)
    raise LLMGenerationError(f"ollama call failed after retries: {last_error}")


def parse_response(
    response_text: str,
    story: dict,
    registry: TesterRegistry,
    tester_names: Iterable[str],
) -> Tuple[List[TestCase], List[str]]:
    if not response_text:
        return [], []
    json_text = _extract_json_block(response_text)
    if not json_text:
        return [], []
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        return [], []
    test_points: List[str] = []
    raw_cases = None
    if isinstance(data, dict):
        raw_cases = data.get("test_cases")
        test_points = _ensure_list(data.get("test_points"))
    else:
        raw_cases = data
    if raw_cases is None:
        if isinstance(data, list):
            raw_cases = data
        else:
            return [], test_points
    cases: List[TestCase] = []
    for item in raw_cases:
        if not isinstance(item, dict):
            continue
        tester_name = str(item.get("tester") or "").strip()
        if not tester_name:
            tester_name = next(iter(tester_names), registry.default().name)
        steps = _ensure_list(item.get("steps"))
        expected = _ensure_list(item.get("expected_results")) or _ensure_list(item.get("expected"))
        preconditions = _ensure_list(item.get("preconditions"))
        summary = item.get("summary") or item.get("description")
        case = TestCase(
            story_id=str(story.get("id") or story.get("story_id") or ""),
            story_title=str(story.get("name") or story.get("title") or "未命名需求"),
            tester=tester_name,
            title=str(item.get("title") or item.get("name") or "待补充测试用例"),
            summary=str(summary) if summary else None,
            preconditions=preconditions,
            steps=steps,
            expected_results=expected,
            priority=_clean_optional(item.get("priority")),
            risk=_clean_optional(item.get("risk")),
            extra={k: v for k, v in item.items() if k not in {"tester", "title", "name", "summary", "description", "preconditions", "steps", "expected", "expected_results", "priority", "risk"}},
        )
        cases.append(case)
    return cases, test_points


def build_fallback_cases(
    story: dict,
    tester_names: Iterable[str],
    registry: TesterRegistry,
    reason: str = "LLM 生成失败",
) -> List[TestCase]:
    fallback: List[TestCase] = []
    testers = list(tester_names) or [registry.default().name]
    for tester in testers:
        fallback.append(
            TestCase(
                story_id=str(story.get("id") or story.get("story_id") or ""),
                story_title=str(story.get("name") or story.get("title") or "未命名需求"),
                tester=tester,
                title=f"[草稿] {story.get('name') or story.get('title') or '未命名需求'}",
                summary=f"{reason}，待测试人员补充",
                preconditions=[],
                steps=["1. 阅读需求描述", "2. 根据业务逻辑拆分测试场景", "3. 补充详细步骤与断言"],
                expected_results=["测试人员根据经验补充"],
                priority=None,
                risk=None,
                extra={"source": "fallback", "fallback_reason": reason},
            )
        )
    return fallback


DEFAULT_PROMPT_TEMPLATE = textwrap.dedent(
    """你是一名高级测试工程师，需要为即将发布的需求拆解测试点并编写结构化的测试用例。\n
    输出 JSON 对象，包含字段：\n
    - \"test_points\": [\"测试点1\", \"测试点2\", ...] —— 先罗列覆盖的测试点，至少包含正向、异常、边界。\n
    - \"test_cases\": [{{ ... }}] —— 每个测试点可对应一个或多个测试用例，字段格式如下：\n
      {{\n        \"tester\": \"测试人员姓名（必须存在于输入 testers 列表中）\",\n        \"title\": \"用例标题\",\n        \"summary\": \"一句话描述\",\n        \"preconditions\": [\"前置条件1\", \"前置条件2\"],\n        \"steps\": [\"步骤1\", \"步骤2\"],\n        \"expected_results\": [\"期望1\", \"期望2\"],\n        \"priority\": \"P0|P1|P2|P3\",\n        \"risk\": \"高|中|低\"\n      }}\n
    所有文本使用简体中文。\n
    ---\n    需求编号：{story_id}\n    需求标题：{story_title}\n    所属模块：{module}\n    需求描述：\n    {story_description}\n
    验收标准：\n    {acceptance_criteria}\n
    可用测试人员：{testers}\n    """
)


def _read_prompt_template(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _collect_response_text(resp: Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text
    if isinstance(data, dict):
        if data.get("done") and data.get("response"):
            return str(data["response"])
        # Some Ollama builds stream as list of events when stream=False
        if "response" in data:
            return str(data.get("response") or "")
    return resp.text


def _extract_json_block(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        if candidate.count("{") == candidate.count("}"):
            return candidate
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        if candidate.count("[") == candidate.count("]"):
            return candidate
    return ""


def _ensure_list(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    if "\n" in text:
        return [line.strip() for line in text.splitlines() if line.strip()]
    if "。" in text and "\n" not in text:
        parts = [p.strip() for p in text.split("。") if p.strip()]
        if parts and not parts[-1].endswith("。"):
            return parts
    return [text]


def _clean_optional(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
