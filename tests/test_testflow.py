from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from config import Config  # type: ignore
from testflow.exporter import export_suite_to_xmind
from testflow.llm import build_fallback_cases, parse_response
from testflow.models import TestCase, TesterContact, TesterSuite
from testflow.service import generate_testflow_for_stories
from testflow.testers import TesterRegistry, extract_story_testers, load_testers


@pytest.fixture()
def tester_registry(tmp_path: Path) -> TesterRegistry:
    data = {
        "testers": [
            {"name": "张三", "email": "zhangsan@example.com", "aliases": ["Z3"]},
            {"name": "李四", "email": "lisi@example.com"},
        ]
    }
    config_path = tmp_path / "testers.json"
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return load_testers(str(config_path))


def test_extract_story_testers_finds_candidates():
    story = {
        "id": 1,
        "name": "支付需求",
        "custom_fields": {
            "测试人员": "张三, 李四",
        },
        "qa_owner": "王五",
    }
    tokens = extract_story_testers(story)
    assert "张三" in tokens
    assert "李四" in tokens
    assert "王五" in tokens


def test_parse_response_to_cases(tester_registry: TesterRegistry):
    story = {"id": "S-1", "name": "订单导出"}
    payload = {
        "test_cases": [
            {
                "tester": "张三",
                "title": "正常下单",
                "steps": ["打开页面", "填写信息", "提交"],
                "expected_results": ["订单生成成功"],
                "priority": "P1",
            }
        ]
    }
    text = f"LLM 输出如下:\n{json.dumps(payload, ensure_ascii=False)}"
    cases, test_points = parse_response(text, story, tester_registry, ["张三"])
    assert test_points == []
    assert len(cases) == 1
    case = cases[0]
    assert case.tester == "张三"
    assert case.steps == ["打开页面", "填写信息", "提交"]
    assert case.priority == "P1"


def test_fallback_cases_cover_all_testers(tester_registry: TesterRegistry):
    story = {"id": "S-2", "name": "异常处理"}
    cases = build_fallback_cases(story, ["李四"], tester_registry)
    assert len(cases) == 1
    assert cases[0].tester == "李四"
    assert "LLM" in (cases[0].summary or "")


def test_export_suite_writes_valid_xmind(tmp_path: Path):
    contact = TesterContact(name="张三", email="zhangsan@example.com")
    case = TestCase(
        story_id="S-100",
        story_title="库存同步",
        tester="张三",
        title="库存充足",
        summary="基础正向场景",
        steps=["创建订单", "检查库存"],
        expected_results=["库存扣减成功"],
        priority="P1",
    )
    suite = TesterSuite(tester=contact, cases=[case])
    attachment = export_suite_to_xmind(suite, tmp_path, datetime(2024, 1, 1))
    assert attachment.file_path.exists()
    with ZipFile(attachment.file_path) as zf:
        content = json.loads(zf.read("content.json"))
        assert content[0]["rootTopic"]["title"].startswith("张三")
        story_nodes = content[0]["rootTopic"]["children"]["attached"]
        assert story_nodes[0]["children"]["attached"][0]["title"] == "库存充足"


def test_generate_testflow_for_stories_creates_xmind(tmp_path: Path) -> None:
    cfg = Config()
    cfg.testflow_output_dir = str(tmp_path / "xmind")
    testers_path = tmp_path / "testers.json"
    testers_path.write_text(
        json.dumps({"testers": [{"name": "张三", "email": "zhangsan@example.com"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    cfg.testflow_testers_path = str(testers_path)
    cfg.testflow_use_llm = False

    stories = [
        {
            "id": "S-3",
            "name": "支付成功页",
            "description": "当用户支付完成后展示成功页面",
            "custom_fields": {"测试人员": "张三"},
        }
    ]

    result = generate_testflow_for_stories(cfg, stories, execute=True)

    assert result.total_stories == 1
    assert result.total_cases >= 1
    assert len(result.attachments) == 1
    assert result.attachments[0].file_path.exists()
