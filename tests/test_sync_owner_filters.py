import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sync import _story_matches_owner  # type: ignore


def test_owner_match_via_frontend_field_with_separator_char():
    story = {"前端": "江林"}
    assert _story_matches_owner(story, ["江林"]) is True


def test_owner_match_via_nested_frontend_label():
    story = {
        "custom_fields": [
            {"label": "前端", "value": "江林"},
            {"label": "后端", "value": "张三"},
        ]
    }
    assert _story_matches_owner(story, ["江林"]) is True


def test_owner_match_via_custom_field_key():
    story = {"custom_field_four": "江林"}
    assert _story_matches_owner(story, ["江林"]) is True


def test_owner_match_via_custom_fields_dict():
    story = {"custom_fields": {"custom_field_four": "江林", "custom_field_four_label": "前端"}}
    assert _story_matches_owner(story, ["江林"]) is True


def test_owner_match_negative_when_not_owner_or_frontend():
    story = {"owner": "李四", "前端": "张三"}
    assert _story_matches_owner(story, ["江林"]) is False


def test_owner_match_via_handler_field():
    story = {"处理人": "江林"}
    assert _story_matches_owner(story, ["江林"]) is True


def test_owner_match_via_current_handler_field():
    story = {"当前处理人": "江林"}
    assert _story_matches_owner(story, ["江林"]) is True
