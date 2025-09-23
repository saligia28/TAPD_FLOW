import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from content import build_page_blocks_from_story  # type: ignore


def _rich_text(block):
    value = block.get(block.get("type"), {})
    pieces = []
    for fragment in value.get("rich_text", []):
        if isinstance(fragment, dict):
            text = fragment.get("text", {})
            if text:
                pieces.append(text.get("content", ""))
    return "".join(pieces)


def test_build_page_blocks_includes_extras_sections():
    story = {
        "id": "tapd-1",
        "description": "简单描述",
        "tags": ["前端", "接口"],
        "attachments": [
            {
                "name": "原型",
                "url": "https://example.com/proto",
                "size": 2048,
                "file_type": "pdf",
                "creator": "Alice",
                "created": "2024-01-02",
            }
        ],
        "comments": [
            {"author": "Bob", "content": "LGTM", "created": "2024-01-03"}
        ],
    }
    blocks = build_page_blocks_from_story(story)
    headings = [
        _rich_text(block)
        for block in blocks
        if block.get("type") in {"heading_2", "heading_3"}
    ]
    assert "标签" in headings
    assert "附件" in headings
    assert "评论" in headings

    list_items = [_rich_text(block) for block in blocks if block.get("type") == "bulleted_list_item"]
    assert any("前端" in item for item in list_items)
    assert any("接口" in item for item in list_items)
    assert any("https://example.com/proto" in item for item in list_items)
    assert any("Bob" in item and "LGTM" in item for item in list_items)
