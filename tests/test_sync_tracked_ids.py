import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import services.sync.service as sync  # type: ignore
from core.config import Config  # type: ignore
from core.state import store  # type: ignore


class DummyTapd:
    def __init__(self) -> None:
        self.get_story_calls: list[str] = []
        self.list_calls = 0

    def list_stories(self, updated_since=None, filters=None):  # noqa: ANN001
        self.list_calls += 1
        story = {"id": "456", "owner": "江林", "description": "", "name": "Story 456"}
        return iter([story])

    def get_story(self, sid: str):
        self.get_story_calls.append(sid)
        return {
            "Story": {
                "id": sid,
                "owner": "测试人员",
                "description": "",
                "name": f"Story {sid}",
            }
        }

    def get_current_iteration(self):  # noqa: D401, ANN001
        """Current iteration lookup (unused in test)."""
        return None


class DummyNotion:
    def __init__(self, *_, **__):
        self.client = None
        self.upserts: list[str] = []
        self.created: list[str] = []

    def find_page_by_tapd_id(self, tapd_id: str):
        if tapd_id in {"123", "456"}:
            return f"page-{tapd_id}"
        return None

    def find_page_by_title(self, title: str):  # noqa: ANN001
        return None

    def upsert_story_page(self, story, blocks):  # noqa: ANN001
        sid = str(story.get("id"))
        self.upserts.append(sid)
        return f"page-{sid}"

    def create_story_page(self, story, blocks):  # noqa: ANN001
        sid = str(story.get("id"))
        self.created.append(sid)
        return f"page-{sid}"

    def existing_index(self):
        return {}


@pytest.fixture()
def patched_state(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "STATE_FILE", tmp_path / "state.json")
    store.save_state({"tracked_story_ids": ["123"]})
    yield


def test_run_sync_refreshes_tracked_story(patched_state, monkeypatch, tmp_path):
    dummy_tapd = DummyTapd()
    dummy_notion = DummyNotion()

    monkeypatch.setattr(sync, "TAPDClient", lambda *args, **kwargs: dummy_tapd)
    monkeypatch.setattr(sync, "NotionWrapper", lambda *args, **kwargs: dummy_notion)
    monkeypatch.setattr(sync, "map_story_to_notion_properties", lambda story: {"Name": story.get("name", "")})
    monkeypatch.setattr(sync, "build_page_blocks_from_story", lambda story, **_: [])
    monkeypatch.setattr(sync, "analyze", lambda text: {})

    cfg = Config()
    cfg.notion_token = "token"
    cfg.notion_requirement_db_id = "db"
    cfg.tapd_fetch_tags = False
    cfg.tapd_fetch_attachments = False
    cfg.tapd_fetch_comments = False
    cfg.tapd_track_existing_ids = True
    cfg.testflow_output_dir = str(tmp_path / "xmind")

    sync.run_sync(cfg, dry_run=False, owner="江林")

    assert dummy_tapd.get_story_calls == ["123"]
    assert set(dummy_notion.upserts) == {"123", "456"}
    assert not dummy_notion.created
    assert store.get_tracked_story_ids() == {"123", "456"}


def test_run_sync_with_explicit_story_ids(patched_state, monkeypatch, tmp_path):
    class TapdWithIds(DummyTapd):
        def list_stories(self, updated_since=None, filters=None):  # noqa: ANN001
            self.list_calls += 1
            return iter([])

        def get_story(self, sid: str):
            self.get_story_calls.append(sid)
            return {
                "Story": {
                    "id": sid,
                    "owner": "李四",
                    "description": "",
                    "name": f"Story {sid}",
                }
            }

    dummy_tapd = TapdWithIds()
    dummy_notion = DummyNotion()

    monkeypatch.setattr(sync, "TAPDClient", lambda *args, **kwargs: dummy_tapd)
    monkeypatch.setattr(sync, "NotionWrapper", lambda *args, **kwargs: dummy_notion)
    monkeypatch.setattr(sync, "map_story_to_notion_properties", lambda story: {"Name": story.get("name", "")})
    monkeypatch.setattr(sync, "build_page_blocks_from_story", lambda story, **_: [])
    monkeypatch.setattr(sync, "analyze", lambda text: {})

    store.save_state({"tracked_story_ids": []})

    cfg = Config()
    cfg.notion_token = "token"
    cfg.notion_requirement_db_id = "db"
    cfg.tapd_fetch_tags = False
    cfg.tapd_fetch_attachments = False
    cfg.tapd_fetch_comments = False
    cfg.tapd_track_existing_ids = False
    cfg.testflow_output_dir = str(tmp_path / "xmind")

    result = sync.run_sync(cfg, dry_run=False, owner="江林", story_ids=["789"])

    assert result.total == 1
    assert dummy_tapd.list_calls == 0
    assert dummy_tapd.get_story_calls == ["789"]
    assert dummy_notion.upserts == []
    assert dummy_notion.created == ["789"]


def test_run_update_respects_analysis_flag(monkeypatch):
    dummy_tapd = DummyTapd()
    dummy_notion = DummyNotion()
    captured_flags: list[bool] = []

    def fake_build(story, *, cfg=None, include_analysis=True):
        captured_flags.append(include_analysis)
        return []

    monkeypatch.setattr(sync, "TAPDClient", lambda *args, **kwargs: dummy_tapd)
    monkeypatch.setattr(sync, "NotionWrapper", lambda *args, **kwargs: dummy_notion)
    monkeypatch.setattr(sync, "map_story_to_notion_properties", lambda story: {"Name": story.get("name", "")})
    monkeypatch.setattr(sync, "build_page_blocks_from_story", fake_build)

    cfg = Config()
    cfg.notion_token = "token"
    cfg.notion_requirement_db_id = "db"
    cfg.tapd_fetch_tags = False
    cfg.tapd_fetch_attachments = False
    cfg.tapd_fetch_comments = False
    cfg.tapd_track_existing_ids = False

    sync.run_update(cfg, ["123"], dry_run=True)
    sync.run_update(cfg, ["123"], dry_run=True, re_analyze=True)

    assert captured_flags == [False, True]
