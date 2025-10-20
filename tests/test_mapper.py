import pytest

from integrations.notion.mapper import extract_status_label, map_story_to_notion_properties, normalize_status


def test_normalize_status_numeric_suffix_with_underscore() -> None:
    assert normalize_status("status_3") == "已完成"


def test_normalize_status_text_suffix_with_prefix() -> None:
    assert normalize_status("planning") == "规划中"


def test_normalize_status_leading_zero_numeric() -> None:
    assert normalize_status("status_06") == "测试中"


def test_normalize_status_numeric_string() -> None:
    assert normalize_status("6") == "测试中"


def test_extract_status_prefers_label() -> None:
    story = {"id": "123", "name": "demo", "status": "status_2", "v_status": "开发中"}
    props = map_story_to_notion_properties(story)
    assert props["状态"]["select"]["name"] == "开发中"
