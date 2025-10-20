from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from analyzer.llm import _build_prompt  # type: ignore
from core.config import Config  # type: ignore


def test_default_prompt_handles_json_braces() -> None:
    cfg = Config()
    cfg.analysis_llm_prompt_path = None

    prompt = _build_prompt("示例描述", story_title="标题", story_id="ID-123", cfg=cfg)

    assert '{\n  "summary": "..."' in prompt
    assert '"test_points": ["测试点1", "测试点2"]' in prompt
    assert "需求编号: ID-123" in prompt
