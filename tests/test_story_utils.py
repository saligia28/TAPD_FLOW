import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from story_utils import (
    extract_story_attachments,
    extract_story_comments,
    extract_story_tags,
    summarize_attachment,
    summarize_comment,
)


def test_extract_story_tags_dedup_and_merge():
    story = {
        "tags": ["UI", "后端", "UI"],
        "labels": "测试, UI",
    }
    assert extract_story_tags(story) == ["UI", "后端", "测试"]


def test_extract_story_attachments_normalization():
    story = {
        "attachments": [
            {
                "id": "123",
                "name": "设计稿",
                "url": "https://example.com/design.png",
                "size": 2048,
                "file_type": "png",
                "creator": "Alice",
                "created": "2024-01-02",
            }
        ]
    }
    attachments = extract_story_attachments(story)
    assert len(attachments) == 1
    att = attachments[0]
    assert att["id"] == "123"
    assert att["name"] == "设计稿"
    assert att["url"] == "https://example.com/design.png"
    assert att["size"] == 2048
    assert att["file_type"] == "png"
    assert att["creator"] == "Alice"
    assert att["created"] == "2024-01-02"

    story_text = {"attachments": "https://example.com/doc.pdf"}
    attachments_text = extract_story_attachments(story_text)
    assert len(attachments_text) == 1
    assert attachments_text[0]["url"] == "https://example.com/doc.pdf"


def test_extract_story_comments_filters_empty_and_sorts():
    story = {
        "comments": [
            {"comment_id": "2", "author": "Bob", "comment": "第二条", "created": "2024-01-03"},
            {"comment_id": "1", "author": "Alice", "comment": "第一条", "created": "2024-01-01"},
            {"comment": "   ", "author": ""},
        ]
    }
    comments = extract_story_comments(story)
    assert [c["id"] for c in comments] == ["1", "2"]
    assert comments[0]["author"] == "Alice"
    assert comments[0]["content"] == "第一条"


def test_summarize_attachment_formats_meta():
    entry = {
        "name": "接口文档",
        "url": "https://example.com/api.docx",
        "size": 4096,
        "file_type": "docx",
        "creator": "Charlie",
        "created": "2024-01-05",
    }
    summary = summarize_attachment(entry)
    assert "接口文档" in summary
    assert "https://example.com/api.docx" in summary
    assert "4.0KB" in summary
    assert "docx" in summary
    assert "Charlie" in summary
    assert "2024-01-05" in summary


def test_summarize_comment_compact_text():
    entry = {"author": "Dana", "content": "需要补充验收", "created": "2024-01-06"}
    summary = summarize_comment(entry)
    assert summary.startswith("Dana · 2024-01-06")
    assert "需要补充验收" in summary
