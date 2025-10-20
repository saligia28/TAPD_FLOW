import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from integrations.notion import client as notion_client  # type: ignore  # noqa: E402


class _DummyPages:
    def __init__(self) -> None:
        self.last_args = None

    def create(self, *, parent, properties, children):
        self.last_args = {
            "parent": parent,
            "properties": properties,
            "children": children,
        }
        return {"id": "page-123"}


class _DummyBlocksChildren:
    def append(self, **kwargs):
        return None


class _DummyBlocks:
    def __init__(self) -> None:
        self.children = _DummyBlocksChildren()

    def children(self, **kwargs):  # pragma: no cover - not used but kept for compatibility
        return self

    def list(self, **kwargs):
        return {"results": [], "has_more": False}

    def update(self, **kwargs):
        return None


class _DummyClient:
    def __init__(self, **kwargs) -> None:
        self.pages = _DummyPages()
        self.blocks = _DummyBlocks()
        self.databases = SimpleNamespace(retrieve=lambda *args, **kwargs: {"properties": {}})
        self.users = SimpleNamespace(list=lambda *args, **kwargs: {"results": []})


@pytest.fixture()
def dummy_wrapper(monkeypatch):
    monkeypatch.setattr(notion_client, "Client", lambda **kwargs: _DummyClient())
    wrapper = notion_client.NotionWrapper(token="token", database_id="db")
    # Configure schema detection manually for predictable behavior
    wrapper._title_prop = "Name"  # type: ignore[attr-defined]
    wrapper._tag_prop = "标签"  # type: ignore[attr-defined]
    wrapper._tag_prop_type = "multi_select"  # type: ignore[attr-defined]
    wrapper._attachment_prop = "附件"  # type: ignore[attr-defined]
    wrapper._attachment_prop_type = "files"  # type: ignore[attr-defined]
    wrapper._comment_prop = "评论"  # type: ignore[attr-defined]
    wrapper._comment_prop_type = "rich_text"  # type: ignore[attr-defined]
    return wrapper


def test_apply_extended_story_properties_enriches_payload(monkeypatch):
    monkeypatch.setattr(notion_client, "Client", None)
    wrapper = notion_client.NotionWrapper(token="", database_id="")
    wrapper._tag_prop = "标签"  # type: ignore[attr-defined]
    wrapper._tag_prop_type = "multi_select"  # type: ignore[attr-defined]
    wrapper._attachment_prop = "附件"  # type: ignore[attr-defined]
    wrapper._attachment_prop_type = "files"  # type: ignore[attr-defined]
    wrapper._comment_prop = "评论"  # type: ignore[attr-defined]
    wrapper._comment_prop_type = "rich_text"  # type: ignore[attr-defined]

    story = {
        "tags": ["A", "B"],
        "attachments": [
            {"name": "设计稿", "url": "https://example.com/design.png", "creator": "Alice", "created": "2024-01-01"},
            {"name": "需求文档", "url": "https://example.com/spec.pdf"},
        ],
        "comments": [
            {"author": "Bob", "content": "看过了", "created": "2024-01-02"},
            {"author": "Carol", "content": "需要补充用例"},
        ],
    }
    props: dict = {}

    wrapper._apply_extended_story_properties(props, story)

    assert props["标签"]["multi_select"] == [{"name": "A"}, {"name": "B"}]
    files_payload = props["附件"]["files"]
    assert files_payload[0]["name"] == "设计稿"
    assert files_payload[0]["external"]["url"] == "https://example.com/design.png"
    comment_text = props["评论"]["rich_text"][0]["text"]["content"]
    assert "Bob" in comment_text
    assert "Carol" in comment_text


def test_create_story_page_carries_story_extras_to_notion(dummy_wrapper):
    story = {
        "id": "123",
        "name": "测试需求",
        "tags": ["标签1", "标签2"],
        "attachments": [
            {"name": "原型", "url": "https://example.com/prototype"},
            {"name": "API", "url": "https://example.com/api"},
        ],
        "comments": [
            {"author": "QA", "content": "需补充测试案例", "created": "2024-03-01"},
        ],
    }

    dummy_wrapper.create_story_page(story, blocks=None)

    recorded = dummy_wrapper.client.pages.last_args  # type: ignore[attr-defined]
    props = recorded["properties"]
    assert "标签" in props
    assert props["标签"]["multi_select"] == [{"name": "标签1"}, {"name": "标签2"}]
    assert "附件" in props
    assert props["附件"]["files"][0]["external"]["url"] == "https://example.com/prototype"
    assert "评论" in props
    assert "QA" in props["评论"]["rich_text"][0]["text"]["content"]
