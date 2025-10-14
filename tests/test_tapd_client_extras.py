import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tapd_client import TAPDClient  # type: ignore  # noqa: E402


def _make_client(**overrides):
    defaults = {
        "api_user": "user",
        "api_password": "pass",
        "api_base": "https://api.tapd.cn",
        "stories_path": "/stories",
        "modules_path": "/modules",
        "iterations_path": "/iterations",
    }
    defaults.update(overrides)
    return TAPDClient(
        "",
        "",
        "101",
        **defaults,
    )


@pytest.mark.parametrize(
    "flags",
    [
        {"include_tags": False, "include_comments": False, "include_attachments": True},
        {"include_tags": False, "include_comments": True, "include_attachments": False},
        {"include_tags": True, "include_comments": False, "include_attachments": False},
    ],
)
def test_fetch_story_extras_uses_workspace(monkeypatch, flags):
    client = _make_client(
        story_tags_path="/story_tags",
        story_attachments_path="/story_attachments",
        story_comments_path="/story_comments",
    )
    calls = []

    def fake_fetch(path, params_candidates):
        calls.append((path, params_candidates))
        return {}

    monkeypatch.setattr(client, "_fetch_with_variants", fake_fetch)
    client.fetch_story_extras("555", **flags)
    for _, params_candidates in calls:
        assert all("workspace_id" in params for params in params_candidates)


def test_fetch_story_extras_provides_attachment_types(monkeypatch):
    client = _make_client(story_attachments_path="/story_attachments")
    calls = []

    def fake_fetch(path, params_candidates):
        calls.append((path, params_candidates))
        return {}

    monkeypatch.setattr(client, "_fetch_with_variants", fake_fetch)
    client.fetch_story_extras("999", include_tags=False, include_comments=False, include_attachments=True)

    paths = [path for path, _ in calls]
    assert "story_attachments" in paths
    assert "attachments" in paths
    assert any("type" in params and params["type"] in {"story", "stories", "Story"} for _, params_list in calls for params in params_list)
    assert any("entry_id" in params or "story_id" in params for _, params_list in calls for params in params_list)


def test_fetch_story_extras_provides_comment_entry_type(monkeypatch):
    client = _make_client(story_comments_path="/story_comments")
    calls = []

    def fake_fetch(path, params_candidates):
        calls.append((path, params_candidates))
        return {}

    monkeypatch.setattr(client, "_fetch_with_variants", fake_fetch)
    client.fetch_story_extras("321", include_tags=False, include_comments=True, include_attachments=False)

    assert any("comments" in path for path, _ in calls)
    assert any("entry_type" in params for _, params_list in calls for params in params_list)


def test_fetch_story_extras_provides_tag_entry_id(monkeypatch):
    client = _make_client(story_tags_path="/story_tags")
    calls = []

    def fake_fetch(path, params_candidates):
        calls.append((path, params_candidates))
        return {}

    monkeypatch.setattr(client, "_fetch_with_variants", fake_fetch)
    client.fetch_story_extras("654", include_tags=True, include_comments=False, include_attachments=False)

    assert any("story_tags" in path or "tags" in path for path, _ in calls)
    assert any("entry_id" in params or "story_id" in params for _, params_list in calls for params in params_list)
