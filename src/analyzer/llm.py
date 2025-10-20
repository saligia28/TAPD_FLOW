from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, Optional

import requests

from core.config import Config


class LLMNotConfigured(RuntimeError):
    """Raised when LLM analysis is requested but config is incomplete."""


class LLMAnalysisError(RuntimeError):
    """Raised when the LLM call fails or returns malformed output."""


@dataclass
class LLMAnalysisResult:
    summary: Optional[str]
    key_features: list[str]
    risks: list[str]
    acceptance: list[str]
    test_points: list[str]

    def as_dict(self) -> Dict[str, object]:
        return {
            "summary": self.summary,
            "key_features": self.key_features,
            "risks": self.risks,
            "acceptance": self.acceptance,
            "test_points": self.test_points,
        }


def analyze_with_llm(
    description: str,
    cfg: Config,
    *,
    story_title: str = "",
    story_id: str = "",
) -> Dict[str, object]:
    if not cfg.analysis_use_llm:
        raise LLMNotConfigured("ANALYSIS_USE_LLM disabled")
    if not description.strip():
        return {}
    host = cfg.ollama_host.strip()
    model = (cfg.analysis_llm_model or cfg.ollama_model or "").strip()
    if not host or not model:
        raise LLMNotConfigured("missing Ollama host/model")

    payload = {
        "model": model,
        "prompt": _build_prompt(description, story_title=story_title, story_id=story_id, cfg=cfg),
        "stream": False,
        "options": {
            "temperature": cfg.ollama_temperature,
        },
    }

    url = host.rstrip("/") + "/api/generate"
    last_error = ""
    for attempt in range(1, cfg.ollama_max_retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=cfg.ollama_timeout)
            resp.raise_for_status()
            data = resp.json()
            text = _extract_response_text(data, fallback=resp.text)
            parsed = _parse_json_block(text)
            return parsed.as_dict()
        except LLMNotConfigured:
            raise
        except Exception as exc:  # pragma: no cover - network issues
            last_error = str(exc)
    raise LLMAnalysisError(f"ollama call failed after retries: {last_error}")


def _build_prompt(description: str, *, story_title: str, story_id: str, cfg: Config) -> str:
    template = _load_template(cfg.analysis_llm_prompt_path)
    return template.format(
        story_id=story_id or "未知需求",
        story_title=story_title or "未命名需求",
        story_description=description.strip(),
    )


def _load_template(path: Optional[str]) -> str:
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            pass
    return DEFAULT_PROMPT


def _extract_response_text(data: Dict[str, object], *, fallback: str) -> str:
    if isinstance(data, dict) and data.get("done") and data.get("response"):
        return str(data["response"])
    return fallback


def _parse_json_block(text: str) -> LLMAnalysisResult:
    snippet = text.strip()
    # try to locate JSON between code fences
    if "```" in snippet:
        parts = snippet.split("```")
        # pick the last non-empty block that looks like json
        for part in reversed(parts):
            part = part.strip()
            if part.startswith("{") and part.endswith("}"):
                snippet = part
                break
    try:
        payload = json.loads(snippet)
    except json.JSONDecodeError as exc:
        raise LLMAnalysisError(f"invalid JSON from LLM: {exc}")
    if not isinstance(payload, dict):
        raise LLMAnalysisError("LLM response is not a dict")
    return LLMAnalysisResult(
        summary=_coerce_str(payload.get("summary")),
        key_features=_coerce_list(payload.get("key_features")),
        risks=_coerce_list(payload.get("risks")),
        acceptance=_coerce_list(payload.get("acceptance")),
        test_points=_coerce_list(payload.get("test_points")),
    )


def _coerce_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        items = [seg.strip() for seg in value.replace("；", ";").split(";") if seg.strip()]
        return items
    return []


def _coerce_str(value: object) -> Optional[str]:
    if value is None:
        return None
    return str(value).strip() or None


DEFAULT_PROMPT = """你是一名资深需求分析师，请阅读以下需求描述，输出 JSON 结构，不要添加额外说明。
字段说明：
- summary: 对需求的简明中文总结，50 字以内。
- key_features: 核心功能要点，列表，按重要性排序。
- risks: 实施或验证风险点列表，可为空。
- acceptance: 建议的验收关注点列表。
- test_points: 推荐的测试点列表，覆盖正向、异常与边界场景。

格式示例：
{{
  "summary": "...",
  "key_features": ["要点1", "要点2"],
  "risks": ["风险1"],
  "acceptance": ["验收点1"],
  "test_points": ["测试点1", "测试点2"]
}}

---
需求编号: {story_id}
需求标题: {story_title}
需求描述:
{story_description}
"""
