from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from analyzer.rule_based import analyze
from core.config import Config
from core.state import store
from integrations.notion import (
    NotionWrapper,
    build_page_blocks_from_story,
    map_story_to_notion_properties,
)
from integrations.tapd import TAPDClient
from services.sync.frontend import story_matches_owner
from services.sync.results import SyncResult, UpdateAllResult
from services.sync.utils import (
    enrich_story_with_extras,
    story_tapd_id,
    unwrap_story_payload,
)
from testflow.service import generate_testflow_for_stories


def run_sync(
    cfg: Config,
    full: bool = False,
    since: Optional[str] = None,
    dry_run: bool = True,
    owner: Optional[str] = None,
    creator: Optional[str] = None,
    wipe_first: bool = False,
    insert_only: bool = False,
    current_iteration: bool = False,
    story_ids: Optional[Sequence[str]] = None,
) -> SyncResult:
    start_ts = time.perf_counter()
    last = None if full else (since or store.get_last_sync_at())
    focus_ids = [str(s).strip() for s in (story_ids or []) if str(s).strip()]
    restrict_to_ids = bool(focus_ids)
    focus_count = len(focus_ids) if restrict_to_ids else 0
    print(
        f"[sync] start | full={full} | since={last} | wipe_first={wipe_first} | "
        f"insert_only={insert_only} | story_ids={focus_count}"
    )

    tapd = TAPDClient(
        cfg.tapd_api_key or "",
        cfg.tapd_api_secret or "",
        cfg.tapd_workspace_id or "",
        api_user=cfg.tapd_api_user,
        api_password=cfg.tapd_api_password,
        token=cfg.tapd_token,
        api_base=cfg.tapd_api_base,
        stories_path=cfg.tapd_stories_path,
        modules_path=cfg.tapd_modules_path,
        iterations_path=cfg.tapd_iterations_path,
        story_tags_path=getattr(cfg, "tapd_story_tags_path", "/story_tags"),
        story_attachments_path=getattr(cfg, "tapd_story_attachments_path", "/story_attachments"),
        story_comments_path=getattr(cfg, "tapd_story_comments_path", "/story_comments"),
    )
    notion = NotionWrapper(cfg.notion_token or "", cfg.notion_requirement_db_id or "")
    extras_cache: Dict[str, Dict[str, Any]] = {}
    tracked_enabled = getattr(cfg, "tapd_track_existing_ids", True)
    tracked_ids: Set[str] = set()
    existing_idx: Dict[str, str] = {}
    existing_idx_loaded = False
    if tracked_enabled and not restrict_to_ids:
        try:
            tracked_ids = store.get_tracked_story_ids()
        except Exception as exc:
            print(f"[sync] tracked-state load failed: {exc}")
            tracked_ids = set()
        if not tracked_ids and notion.client:
            try:
                existing_idx = notion.existing_index()
                existing_idx_loaded = True
                bootstrap_ids = list(existing_idx.keys())
                if bootstrap_ids:
                    tracked_ids.update(bootstrap_ids)
                    print(f"[sync] bootstrap tracked stories from notion index count={len(tracked_ids)}")
            except Exception as exc:
                print(f"[sync] tracked bootstrap failed: {exc}")

    # Build filters: CLI overrides > config
    filters: Dict[str, object] = {}
    only_owner = None if restrict_to_ids else (owner or cfg.tapd_only_owner)
    only_creator = creator or cfg.tapd_only_creator
    # NOTE: owner is filtered locally via substring matching, avoid narrowing server results
    if only_creator and not restrict_to_ids:
        filters["creator"] = only_creator
    # Current iteration filter
    cur_iter = None
    detected_iter_key: Optional[str] = None
    if current_iteration or getattr(cfg, 'tapd_use_current_iteration', False):
        try:
            cur_iter = tapd.get_current_iteration()
        except Exception:
            cur_iter = None
        if cur_iter:
            it_id = cur_iter.get('id') or cur_iter.get('iteration_id')
            if it_id:
                # Probe which iteration filter key works (limit=1 to avoid rate)
                candidates = getattr(cfg, 'tapd_filter_iteration_id_keys', []) or ['iteration_id']
                for k in candidates:
                    try:
                        probe = tapd._get(tapd.stories_path, params={
                            'workspace_id': cfg.tapd_workspace_id,
                            k: it_id,
                            'page': 1,
                            'limit': 1,
                            'with_v_status': 1,
                        })
                        data = probe.get('data') if isinstance(probe, dict) else None
                        ok = False
                        if isinstance(data, list) and len(data) > 0:
                            ok = True
                        elif isinstance(data, dict):
                            for key in ('stories','list','items'):
                                if isinstance(data.get(key), list) and data.get(key):
                                    ok = True
                                    break
                        if ok:
                            detected_iter_key = k
                            break
                    except Exception:
                        continue
                # fall back to first candidate if none detected
                (detected_iter_key := detected_iter_key or (candidates[0] if candidates else None))
                if detected_iter_key and not restrict_to_ids:
                    filters[detected_iter_key] = it_id

    owner_subs: list[str] = []
    if only_owner:
        owner_subs = [s.strip() for s in str(only_owner).split(',') if s.strip()]

    def owner_matches(story: dict) -> bool:
        if restrict_to_ids:
            return True
        return story_matches_owner(story, owner_subs)

    def iteration_matches(story: dict) -> bool:
        if restrict_to_ids:
            return True
        if not cur_iter:
            return True
        # server-side filter may already apply; keep local guard
        want = str(cur_iter.get('id') or cur_iter.get('iteration_id') or '')
        if not want:
            return True
        for key in ('iteration_id', 'sprint_id', 'iteration'):
            v = story.get(key)
            if v is None:
                continue
            if str(v) == want:
                return True
        return False

    # If we are going to wipe, enforce full fetch to rebuild database
    if wipe_first and not dry_run:
        print("[sync] wipe-first enabled: clearing Notion database (archiving all pages) and switching to full fetch")
        try:
            cleared = notion.clear_database()
            print(f"[sync] cleared pages={cleared}")
        except Exception as e:
            print(f"[sync] clear failed: {e}")
        last = None  # ignore since

    # Build existing index once when insert-only
    if insert_only and notion.client:
        if not existing_idx_loaded:
            try:
                existing_idx = notion.existing_index()
                existing_idx_loaded = True
            except Exception as e:
                print(f"[sync] build existing index failed: {e}")
        if existing_idx:
            print(f"[sync] insert-only: existing TAPD_ID count={len(existing_idx)}")

    # Strict pipeline: 1) fetch all stories 2) analyze/normalize 3) write to Notion
    all_stories: List[dict] = []
    notion_candidates: List[dict] = []
    notion_seen: Set[str] = set()
    fetched_ids: Set[str] = set()
    def iterate_source_stories() -> Iterable[dict]:
        if restrict_to_ids:
            seen_targets: Set[str] = set()
            for sid in focus_ids:
                if not sid or sid in seen_targets:
                    continue
                seen_targets.add(sid)
                try:
                    res = tapd.get_story(sid)
                except Exception as exc:
                    print(f"[sync] fetch by id failed id={sid}: {exc}")
                    continue
                story = unwrap_story_payload(res)
                if not isinstance(story, dict):
                    print(f"[sync] unexpected payload when fetching id={sid}")
                    continue
                story.setdefault("id", sid)
                yield story
        else:
            for raw in tapd.list_stories(updated_since=last, filters=filters or None):
                story = unwrap_story_payload(raw) or raw
                if isinstance(story, dict):
                    yield story

    for story in iterate_source_stories():
        sid = story_tapd_id(story)
        matches_iteration = iteration_matches(story)
        if matches_iteration:
            all_stories.append(story)
            if sid:
                fetched_ids.add(sid)
        is_tracked_story = tracked_enabled and sid and sid in tracked_ids
        matches_owner = owner_matches(story)
        if not matches_iteration and not is_tracked_story:
            continue
        if is_tracked_story or matches_owner:
            if sid and sid in notion_seen:
                continue
            notion_candidates.append(story)
            if sid:
                notion_seen.add(sid)

    if tracked_enabled and not restrict_to_ids:
        missing_tracked = [sid for sid in tracked_ids if sid and sid not in fetched_ids]
        if missing_tracked:
            print(f"[sync] refreshing tracked stories count={len(missing_tracked)}")
        for sid in missing_tracked:
            try:
                res = tapd.get_story(sid)
            except Exception as exc:
                print(f"[sync] refresh failed TAPD_ID={sid}: {exc}")
                continue
            story = unwrap_story_payload(res)
            if not isinstance(story, dict):
                print(f"[sync] refresh skip TAPD_ID={sid} (unexpected payload)")
                continue
            story.setdefault("id", sid)
            all_stories.append(story)
            if sid:
                fetched_ids.add(sid)
                if sid not in notion_seen:
                    notion_candidates.append(story)
                    notion_seen.add(sid)

    if all_stories:
        execute_testflow = not dry_run
        tf_result = generate_testflow_for_stories(cfg, all_stories, execute=execute_testflow)
        print(
            f"[sync] testflow | stories={tf_result.total_stories} "
            f"cases={tf_result.total_cases} attachments={len(tf_result.attachments)} "
            f"execute={execute_testflow}"
        )
    else:
        tf_result = None

    count = 0
    created_count = 0
    existing_count = 0
    skipped_count = 0
    synced_ids: Set[str] = set()
    # Creation guard configuration (enforced only when creating new pages)
    creation_owner = cfg.creation_owner_substr or only_owner
    creation_owner_subs: list[str] = []
    if creation_owner and not restrict_to_ids:
        creation_owner_subs = [s.strip() for s in str(creation_owner).split(',') if s.strip()]
    require_cur_iter_for_create = False if restrict_to_ids else getattr(cfg, 'creation_require_current_iteration', True)
    # ensure cur_iter if required for create
    if require_cur_iter_for_create and not cur_iter:
        try:
            cur_iter = tapd.get_current_iteration()
        except Exception:
            cur_iter = None

    def owner_matches_creation(story: dict) -> bool:
        return story_matches_owner(story, creation_owner_subs)

    def iteration_matches_creation(story: dict) -> bool:
        if not require_cur_iter_for_create:
            return True
        if not cur_iter:
            return False
        want = str(cur_iter.get('id') or cur_iter.get('iteration_id') or '')
        if not want:
            return False
        for key in ('iteration_id', 'sprint_id', 'iteration'):
            v = story.get(key)
            if v is None:
                continue
            if str(v) == want:
                return True
        return False

    for story in notion_candidates:
        count += 1
        enrich_story_with_extras(tapd, cfg, story, cache=extras_cache, ctx="sync")
        # Some TAPD records may carry description=None; coerce to empty string for analyzers
        text = story.get("description") or ""
        res = analyze(text)
        props = map_story_to_notion_properties(story)
        blocks = build_page_blocks_from_story(story, cfg=cfg)
        tapd_id = story_tapd_id(story)
        if not tapd_id or tapd_id in ('', 'None', 'unknown'):
            print("[sync] skip story without valid TAPD id")
            continue
        if insert_only and tapd_id:
            # fast check if known
            exists = tapd_id in existing_idx if existing_idx else bool(notion.find_page_by_tapd_id(tapd_id))
            if exists:
                print(f"[sync] skip existing TAPD_ID={tapd_id}")
                existing_count += 1
                continue
            # enforce creation guard
            if not owner_matches_creation(story) or not iteration_matches_creation(story):
                print(f"[sync] skip create TAPD_ID={tapd_id} (not owned/current-iter)")
                skipped_count += 1
                continue
            if dry_run:
                print(f"[sync] would create TAPD_ID={tapd_id} title={props.get('Name')}")
                created_count += 1
            else:
                page_id = notion.create_story_page(story, blocks)
                synced_ids.add(tapd_id)
                print(f"[sync] created page {page_id}")
                created_count += 1
        else:
            # For general upsert: update if exists; create only if meets creation guard
            if dry_run:
                existing_page = notion.find_page_by_tapd_id(tapd_id) or notion.find_page_by_title(story.get('name') or story.get('title') or '')
                if existing_page:
                    print(f"[sync] would update TAPD_ID={tapd_id} title={props.get('Name')}")
                    existing_count += 1
                elif owner_matches_creation(story) and iteration_matches_creation(story):
                    print(f"[sync] would create TAPD_ID={tapd_id} title={props.get('Name')}")
                    created_count += 1
                else:
                    print(f"[sync] skip create TAPD_ID={tapd_id} (not owned/current-iter)")
                    skipped_count += 1
            else:
                # detect existence
                existing_page = notion.find_page_by_tapd_id(tapd_id) or notion.find_page_by_title(story.get('name') or story.get('title') or '')
                if existing_page:
                    page_id = notion.upsert_story_page(story, blocks)
                    synced_ids.add(tapd_id)
                    print(f"[sync] upserted page {page_id}")
                    existing_count += 1
                else:
                    if owner_matches_creation(story) and iteration_matches_creation(story):
                        page_id = notion.create_story_page(story, blocks)
                        synced_ids.add(tapd_id)
                        print(f"[sync] created page {page_id}")
                        created_count += 1
                    else:
                        print(f"[sync] skip create TAPD_ID={tapd_id} (not owned/current-iter)")
                        skipped_count += 1

    if not dry_run and tracked_enabled and synced_ids:
        try:
            store.add_tracked_story_ids(synced_ids)
        except Exception as exc:
            print(f"[sync] tracked-state update failed: {exc}")

    print(f"[sync] done | items={count} | created={created_count} | existing={existing_count} | skipped={skipped_count}")
    duration = time.perf_counter() - start_ts
    # In real run: store.set_last_sync_at(now)
    return SyncResult(
        total=count,
        created=created_count,
        existing=existing_count,
        skipped=skipped_count,
        duration=duration,
        dry_run=dry_run,
    )
def run_update(
    cfg: Config,
    ids: List[str],
    *,
    dry_run: bool = True,
    create_missing: bool = False,
    re_analyze: bool = False,
) -> None:
    """Manually update pages by TAPD story IDs.

    - Fetch each story via TAPDClient.get_story
    - Match Notion page by TAPD_ID and update properties + content blocks
    - If create_missing=False, skip when page not found; else create
    """
    print(f"[update] start | ids={len(ids)} | dry_run={dry_run} | create_missing={create_missing}")
    tapd = TAPDClient(
        cfg.tapd_api_key or "",
        cfg.tapd_api_secret or "",
        cfg.tapd_workspace_id or "",
        api_user=cfg.tapd_api_user,
        api_password=cfg.tapd_api_password,
        token=cfg.tapd_token,
        api_base=cfg.tapd_api_base,
        stories_path=cfg.tapd_stories_path,
        modules_path=cfg.tapd_modules_path,
        iterations_path=cfg.tapd_iterations_path,
        story_tags_path=getattr(cfg, "tapd_story_tags_path", "/story_tags"),
        story_attachments_path=getattr(cfg, "tapd_story_attachments_path", "/story_attachments"),
        story_comments_path=getattr(cfg, "tapd_story_comments_path", "/story_comments"),
    )
    notion = NotionWrapper(cfg.notion_token or "", cfg.notion_requirement_db_id or "")

    updated = 0
    skipped = 0
    extras_cache: Dict[str, Dict[str, Any]] = {}
    tracked_enabled = getattr(cfg, "tapd_track_existing_ids", True)
    synced_ids: Set[str] = set()
    for sid in ids:
        sid = str(sid).strip()
        if not sid:
            continue
        try:
            res = tapd.get_story(sid)
        except Exception as e:
            print(f"[update] fetch failed id={sid}: {e}")
            skipped += 1
            continue
        story = unwrap_story_payload(res)
        if not isinstance(story, dict):
            print(f"[update] unexpected payload id={sid}")
            skipped += 1
            continue
        # Ensure id present and consistent
        story.setdefault("id", sid)
        enrich_story_with_extras(tapd, cfg, story, cache=extras_cache, ctx="update")

        # Build blocks with latest analyzers
        props = map_story_to_notion_properties(story)
        blocks = build_page_blocks_from_story(
            story,
            cfg=cfg,
            include_analysis=re_analyze,
        )

        try:
            page_id = notion.find_page_by_tapd_id(sid, suppress_errors=False)
        except Exception as exc:
            print(f"[update] lookup failed id={sid}: {exc}")
            skipped += 1
            continue
        if not page_id:
            title = story.get("name") or story.get("title")
            if title:
                try:
                    page_id = notion.find_page_by_title(str(title), suppress_errors=False)
                except Exception as exc:
                    print(f"[update] title lookup failed id={sid}: {exc}")
                    skipped += 1
                    continue
        if not page_id and not create_missing:
            try:
                diag = notion.debug_lookup(sid)  # type: ignore[attr-defined]
                print(
                    "[update] debug",
                    f"TAPD_ID={sid}",
                    f"id_prop={diag.get('id_prop')}",
                    f"id_query_count={diag.get('id_query_count')}",
                    f"desc_query_count={diag.get('desc_query_count')}",
                    f"id_query_filter={diag.get('id_query_filter')}",
                )
            except Exception:
                pass
            print(f"[update] skip id={sid} (page not found; use --create-missing to create)")
            skipped += 1
            continue

        if dry_run:
            action = "update" if page_id else "create"
            print(f"[update] would {action} id={sid} title={props.get('Name')}")
            updated += 1
        else:
            if page_id:
                pid = notion.upsert_story_page(story, blocks)
            else:
                pid = notion.create_story_page(story, blocks)
            if tracked_enabled:
                synced_ids.add(sid)
            print(f"[update] {('updated' if page_id else 'created')} page {pid}")
            updated += 1

    if not dry_run and tracked_enabled and synced_ids:
        try:
            store.add_tracked_story_ids(synced_ids)
        except Exception as exc:
            print(f"[update] tracked-state update failed: {exc}")

    print(f"[update] done | processed={updated} | skipped={skipped}")


def run_update_all(
    cfg: Config,
    *,
    dry_run: bool = True,
    owner: Optional[str] = None,
    creator: Optional[str] = None,
    current_iteration: bool = False,
    re_analyze: bool = False,
) -> UpdateAllResult:
    """Update-only pipeline over TAPD stories.

    - Iterate TAPD list_stories with optional filters
    - For each story, find Notion page by TAPD_ID; if exists update content/properties
    - If Notion page does not exist, skip (never create)
    """
    start_ts = time.perf_counter()
    print(f"[update-all] start | dry_run={dry_run}")
    tapd = TAPDClient(
        cfg.tapd_api_key or "",
        cfg.tapd_api_secret or "",
        cfg.tapd_workspace_id or "",
        api_user=cfg.tapd_api_user,
        api_password=cfg.tapd_api_password,
        token=cfg.tapd_token,
        api_base=cfg.tapd_api_base,
        stories_path=cfg.tapd_stories_path,
        modules_path=cfg.tapd_modules_path,
        iterations_path=cfg.tapd_iterations_path,
        story_tags_path=getattr(cfg, "tapd_story_tags_path", "/story_tags"),
        story_attachments_path=getattr(cfg, "tapd_story_attachments_path", "/story_attachments"),
        story_comments_path=getattr(cfg, "tapd_story_comments_path", "/story_comments"),
    )
    notion = NotionWrapper(cfg.notion_token or "", cfg.notion_requirement_db_id or "")

    # Server-side filters: keep owner local (supports substring later) and build iteration filter if requested
    filters: Dict[str, object] = {}
    only_owner = owner or cfg.tapd_only_owner
    only_creator = creator or cfg.tapd_only_creator
    if only_creator:
        filters["creator"] = only_creator
    cur_iter = None
    if current_iteration or getattr(cfg, 'tapd_use_current_iteration', False):
        try:
            cur_iter = tapd.get_current_iteration()
        except Exception:
            cur_iter = None
        if cur_iter:
            it_id = cur_iter.get('id') or cur_iter.get('iteration_id')
            if it_id:
                candidates = getattr(cfg, 'tapd_filter_iteration_id_keys', []) or ['iteration_id']
                filters[candidates[0]] = it_id

    owner_subs: list[str] = []
    if only_owner:
        owner_subs = [s.strip() for s in str(only_owner).split(',') if s.strip()]

    def owner_matches(story: dict) -> bool:
        return story_matches_owner(story, owner_subs)

    def iteration_matches(story: dict) -> bool:
        if not cur_iter:
            return True
        want = str(cur_iter.get('id') or cur_iter.get('iteration_id') or '')
        if not want:
            return True
        for key in ('iteration_id', 'sprint_id', 'iteration'):
            v = story.get(key)
            if v is None:
                continue
            if str(v) == want:
                return True
        return False

    updated = 0
    skipped = 0
    scanned = 0
    extras_cache: Dict[str, Dict[str, Any]] = {}
    tracked_enabled = getattr(cfg, "tapd_track_existing_ids", True)
    synced_ids: Set[str] = set()
    for story in tapd.list_stories(updated_since=None, filters=filters or None):
        scanned += 1
        if not owner_matches(story) or not iteration_matches(story):
            continue
        raw_id = story.get('id')
        tapd_id = str(raw_id) if raw_id is not None else ''
        if not tapd_id:
            continue
        enrich_story_with_extras(tapd, cfg, story, cache=extras_cache, ctx="update-all")
        props = map_story_to_notion_properties(story)
        blocks = build_page_blocks_from_story(
            story,
            cfg=cfg,
            include_analysis=re_analyze,
        )
        # Update-only: try match by TAPD_ID or title (compute blocks first for update path)
        if dry_run:
            page_id = notion.find_page_by_tapd_id(tapd_id) or notion.find_page_by_title(story.get('name') or story.get('title') or '')
        else:
            page_id = notion.update_story_page_if_exists(story, blocks)
            if tracked_enabled and page_id:
                synced_ids.add(tapd_id)
        if not page_id:
            skipped += 1
            continue
        if dry_run:
            print(f"[update-all] would update id={tapd_id} title={props.get('Name')}")
            updated += 1
        else:
            print(f"[update-all] updated page {page_id}")
            updated += 1

    duration = time.perf_counter() - start_ts
    print(
        f"[update-all] done | scanned={scanned} | updated={updated} | "
        f"skipped_not_found={skipped} | duration={duration:.2f}s"
    )

    if not dry_run and tracked_enabled and synced_ids:
        try:
            store.add_tracked_story_ids(synced_ids)
        except Exception as exc:
            print(f"[update-all] tracked-state update failed: {exc}")
    return UpdateAllResult(
        scanned=scanned,
        updated=updated,
        skipped=skipped,
        duration=duration,
        dry_run=dry_run,
    )

def run_update_from_notion(
    cfg: Config,
    *,
    dry_run: bool = True,
    limit: Optional[int] = None,
    re_analyze: bool = False,
) -> None:
    """Update pages by traversing existing Notion records (safer, no creations).

    - Enumerate Notion pages with TAPD_ID
    - Fetch each story by ID from TAPD
    - Update properties and detail subpage blocks
    """
    print(f"[update-from-notion] start | dry_run={dry_run} | limit={limit}")
    tapd = TAPDClient(
        cfg.tapd_api_key or "",
        cfg.tapd_api_secret or "",
        cfg.tapd_workspace_id or "",
        api_user=cfg.tapd_api_user,
        api_password=cfg.tapd_api_password,
        token=cfg.tapd_token,
        api_base=cfg.tapd_api_base,
        stories_path=cfg.tapd_stories_path,
        modules_path=cfg.tapd_modules_path,
        iterations_path=cfg.tapd_iterations_path,
        story_tags_path=getattr(cfg, "tapd_story_tags_path", "/story_tags"),
        story_attachments_path=getattr(cfg, "tapd_story_attachments_path", "/story_attachments"),
        story_comments_path=getattr(cfg, "tapd_story_comments_path", "/story_comments"),
    )
    notion = NotionWrapper(cfg.notion_token or "", cfg.notion_requirement_db_id or "")

    index = notion.existing_index()
    ids = list(index.keys())
    if limit is not None:
        ids = ids[: max(0, int(limit))]
    updated = 0
    extras_cache: Dict[str, Dict[str, Any]] = {}
    tracked_enabled = getattr(cfg, "tapd_track_existing_ids", True)
    synced_ids: Set[str] = set()
    for sid in ids:
        try:
            res = tapd.get_story(sid)
        except Exception as e:
            print(f"[update-from-notion] fetch failed id={sid}: {e}")
            continue
        story = unwrap_story_payload(res) or {}
        story.setdefault("id", sid)
        enrich_story_with_extras(tapd, cfg, story, cache=extras_cache, ctx="update-from-notion")
        props = map_story_to_notion_properties(story)
        blocks = build_page_blocks_from_story(
            story,
            cfg=cfg,
            include_analysis=re_analyze,
        )
        if dry_run:
            print(f"[update-from-notion] would update id={sid} title={props.get('Name')}")
            updated += 1
        else:
            pid = notion.update_story_page_if_exists(story, blocks)
            if pid:
                print(f"[update-from-notion] updated page {pid}")
                if tracked_enabled:
                    synced_ids.add(sid)
                updated += 1
            else:
                # Should not happen since we got ids from index; still log
                print(f"[update-from-notion] skip id={sid} (page not found)")
    print(f"[update-from-notion] done | updated={updated} | available={len(index)}")

    if not dry_run and tracked_enabled and synced_ids:
        try:
            store.add_tracked_story_ids(synced_ids)
        except Exception as exc:
            print(f"[update-from-notion] tracked-state update failed: {exc}")


def run_export(
    cfg: Config,
    *,
    owner_contains: Optional[str] = None,
    current_iteration: bool = False,
    module_contains: Optional[str] = None,
    limit: int = 50,
    cursor: Optional[str] = None,
) -> Dict[str, object]:
    """Export Notion pages to JSON for MCP/other tools.

    - Filters applied on Notion properties; current_iteration verified via TAPD when possible
    - Returns {schema_version, items, next_cursor}
    """
    tapd = TAPDClient(
        cfg.tapd_api_key or "",
        cfg.tapd_api_secret or "",
        cfg.tapd_workspace_id or "",
        api_user=cfg.tapd_api_user,
        api_password=cfg.tapd_api_password,
        token=cfg.tapd_token,
        api_base=cfg.tapd_api_base,
        stories_path=cfg.tapd_stories_path,
        modules_path=cfg.tapd_modules_path,
        iterations_path=getattr(cfg, 'tapd_iterations_path', '/iterations'),
    )
    notion = NotionWrapper(cfg.notion_token or "", cfg.notion_requirement_db_id or "")
    client = notion.client
    items: list = []
    next_cursor: Optional[str] = None
    if not client:
        return {"schema_version": "v1", "items": [], "next_cursor": None}

    # Helpers
    import re as _re

    def _plain_from_meta(meta: Dict[str, Any]) -> str:
        t = meta.get('type')
        if t == 'rich_text':
            return _pt(meta.get('rich_text', []))
        if t == 'title':
            return _pt(meta.get('title', []))
        if t == 'number':
            v = meta.get('number')
            return str(v) if v is not None else ''
        if t == 'url':
            return meta.get('url') or ''
        return ''

    def _extract_tapd_id_from_props(props: Dict[str, Any]) -> Optional[str]:
        # 1) Preferred id property
        if notion._id_prop and notion._id_prop in props:
            s = _plain_from_meta(props[notion._id_prop])
            m = _re.search(r"TAPD_ID\s*:\s*([0-9A-Za-z_-]+)", s)
            if m:
                return m.group(1)
            if s and s.strip().isdigit():
                return s.strip()
            # also try first 10+ digit chunk
            m = _re.search(r"(\d{6,})", s)
            if m:
                return m.group(1)
        # 2) Any rich_text property containing marker
        for name, meta in props.items():
            if not isinstance(meta, dict):
                continue
            if meta.get('type') != 'rich_text':
                continue
            s = _plain_from_meta(meta)
            m = _re.search(r"TAPD_ID\s*:\s*([0-9A-Za-z_-]+)", s)
            if m:
                return m.group(1)
        return None

    # Pull one page of results from Notion
    try:
        res = client.databases.query(  # type: ignore
            database_id=cfg.notion_requirement_db_id,
            page_size=max(1, min(100, int(limit))),
            **({"start_cursor": cursor} if cursor else {}),
        )
        pages = res.get("results", [])
        next_cursor = res.get("next_cursor")
    except Exception:
        pages = []
        next_cursor = None

    # Current iteration id (optional TAPD check)
    cur_iter_id: Optional[str] = None
    if current_iteration:
        try:
            itr = tapd.get_current_iteration()
            if itr:
                cur_iter_id = str(itr.get('id') or itr.get('iteration_id'))
        except Exception:
            pass

    # Helper to extract plain
    def _pt(arr):
        txt = []
        for x in arr or []:
            if isinstance(x, dict):
                t = x.get('plain_text') or x.get('text', {}).get('content')
                if t:
                    txt.append(t)
        return ''.join(txt)

    # Owner substrings
    owner_subs: list[str] = [s.strip() for s in (owner_contains or '').split(',') if s.strip()]

    for pg in pages:
        pid = pg.get('id')
        props = pg.get('properties', {})
        # Title
        title = None
        if notion._title_prop and notion._title_prop in props:
            meta = props[notion._title_prop]
            if meta.get('type') == 'title':
                title = _pt(meta.get('title', []))
        # TAPD_ID (robust extraction)
        tapd_id = _extract_tapd_id_from_props(props)
        # Owner filter
        if owner_subs:
            matched_owner = False
            # Prefer Notion owner property
            if notion._owner_prop and notion._owner_prop in props:
                meta = props[notion._owner_prop]
                names: list[str] = []
                if meta.get('type') == 'multi_select':
                    names = [o.get('name') for o in meta.get('multi_select', []) if isinstance(o, dict)]
                elif meta.get('type') == 'people':
                    names = [o.get('name') for o in meta.get('people', []) if isinstance(o, dict)]
                hay = ' '.join([n for n in names if n])
                if any(sub in hay for sub in owner_subs):
                    matched_owner = True
            # Fallback to TAPD owner via get_story
            if not matched_owner and tapd_id:
                try:
                    st = tapd.get_story(str(tapd_id))
                    s = unwrap_story_payload(st) or {}
                    hay = ' '.join(str(s.get(k) or '') for k in ('owner','assignee','current_owner','owners'))
                    if any(sub in hay for sub in owner_subs):
                        matched_owner = True
                except Exception:
                    pass
            if owner_subs and not matched_owner:
                continue
        # Module filter
        if module_contains and notion._module_prop and notion._module_prop in props:
            meta = props[notion._module_prop]
            txt = ''
            if meta.get('type') == 'select':
                o = meta.get('select') or {}
                txt = o.get('name') or ''
            elif meta.get('type') == 'multi_select':
                txt = ' '.join([o.get('name') or '' for o in meta.get('multi_select', []) if isinstance(o, dict)])
            if module_contains not in txt:
                continue
        # Current iteration filter via TAPD get_story
        if current_iteration and cur_iter_id and tapd_id:
            try:
                st = tapd.get_story(str(tapd_id))
                s = unwrap_story_payload(st) or {}
                it = str(s.get('iteration_id') or '')
                if it != cur_iter_id:
                    continue
            except Exception:
                continue

        # Gather blocks & images
        blks = notion.get_page_blocks(pid)
        # Extract paragraphs under headings to derive description_text, analysis, feature_points
        section = None
        desc_lines: list[str] = []
        analysis: Dict[str, Any] = {}
        feature_points: list[str] = []
        images: list[Dict[str, Any]] = []

        def _blk_text(b):
            rich = (b.get(b.get('type'), {}) or {}).get('rich_text', [])
            return _pt(rich)

        for b in blks:
            t = b.get('type')
            if t in {'heading_1','heading_2','heading_3'}:
                name = _blk_text(b).strip()
                if name:
                    section = name
                continue
            if t == 'image':
                img = b.get('image', {})
                if isinstance(img, dict):
                    if img.get('type') == 'external':
                        url = (img.get('external') or {}).get('url')
                    else:
                        url = (img.get('file') or {}).get('url')
                    if url:
                        images.append({'url': url})
            if section == '原始描述':
                if t in {'paragraph','bulleted_list_item','numbered_list_item','quote','code'}:
                    txt = _blk_text(b)
                    if txt:
                        desc_lines.append(txt)
            elif section == '内容分析':
                if t in {'paragraph'}:
                    txt = _blk_text(b)
                    if ':' in txt:
                        k, v = txt.split(':', 1)
                        analysis[k.strip()] = [x.strip() for x in v.split(',') if x.strip()]
            elif section in {'功能点','需求点'}:
                if t in {'bulleted_list_item','numbered_list_item','paragraph'}:
                    txt = _blk_text(b)
                    if txt:
                        feature_points.append(txt)

        # Status
        status = None
        if notion._status_prop and notion._status_prop in props:
            meta = props[notion._status_prop]
            if meta.get('type') == 'select' and meta.get('select'):
                status = (meta.get('select') or {}).get('name')

        # Priority
        priority = None
        if notion._priority_prop and notion._priority_prop in props:
            meta = props[notion._priority_prop]
            if meta.get('type') == 'select' and meta.get('select'):
                priority = (meta.get('select') or {}).get('name')

        # Assignees
        assignees: list[str] = []
        if notion._owner_prop and notion._owner_prop in props:
            meta = props[notion._owner_prop]
            if meta.get('type') == 'multi_select':
                assignees = [o.get('name') for o in meta.get('multi_select', []) if isinstance(o, dict) and o.get('name')]
            elif meta.get('type') == 'people':
                assignees = [o.get('name') for o in meta.get('people', []) if isinstance(o, dict) and o.get('name')]

        # Module value
        module_val = None
        if notion._module_prop and notion._module_prop in props:
            meta = props[notion._module_prop]
            if meta.get('type') == 'select':
                module_val = (meta.get('select') or {}).get('name')
            elif meta.get('type') == 'multi_select':
                names = [o.get('name') for o in meta.get('multi_select', []) if isinstance(o, dict) and o.get('name')]
                module_val = ','.join(names)

        # Planned dates / FE hours from Notion props
        def _date_from_prop(pname: Optional[str]) -> Optional[str]:
            if not pname or pname not in props:
                return None
            meta = props[pname]
            if meta.get('type') == 'date' and meta.get('date'):
                d = meta.get('date') or {}
                return d.get('start')
            return None
        planned_start = _date_from_prop(notion._planned_start_prop)
        planned_end = _date_from_prop(notion._planned_end_prop)
        if not (planned_start or planned_end) and notion._planned_range_prop and notion._planned_range_prop in props:
            meta = props[notion._planned_range_prop]
            if meta.get('type') == 'date' and meta.get('date'):
                d = meta.get('date') or {}
                planned_start = d.get('start')
                planned_end = d.get('end')
        fe_hours = None
        if notion._fe_hours_prop and notion._fe_hours_prop in props:
            meta = props[notion._fe_hours_prop]
            if meta.get('type') == 'number':
                fe_hours = meta.get('number')

        items.append({
            'id': str(tapd_id or ''),
            'title': title or '',
            'status': status or '',
            'priority': priority,
            'assignees': assignees,
            'iteration_id': cur_iter_id if current_iteration else None,
            'module': module_val,
            'planned_start': planned_start,
            'planned_end': planned_end,
            'fe_hours': fe_hours,
            'updated_at': pg.get('last_edited_time'),
            'links': {
                'tapd': None,
                'notion_page': notion.page_url(pid),
            },
            'content': {
                'description_text': '\n'.join(desc_lines).strip(),
                'blocks': blks,
                'analysis': analysis,
                'feature_points': feature_points,
                'images': images,
            },
        })

    return {"schema_version": "v1", "items": items, "next_cursor": next_cursor}

def run_sync_by_modules(
    cfg: Config,
    full: bool = False,
    since: Optional[str] = None,
    dry_run: bool = True,
    owner: Optional[str] = None,
    creator: Optional[str] = None,
    wipe_first: bool = False,
    insert_only: bool = False,
    current_iteration: bool = False,
) -> SyncResult:
    start_ts = time.perf_counter()
    last = None if full else (since or store.get_last_sync_at())
    print(f"[sync-mod] start | full={full} | since={last} | wipe_first={wipe_first} | insert_only={insert_only}")

    tapd = TAPDClient(
        cfg.tapd_api_key or "",
        cfg.tapd_api_secret or "",
        cfg.tapd_workspace_id or "",
        api_user=cfg.tapd_api_user,
        api_password=cfg.tapd_api_password,
        token=cfg.tapd_token,
        api_base=cfg.tapd_api_base,
        stories_path=cfg.tapd_stories_path,
        modules_path=cfg.tapd_modules_path,
        iterations_path=cfg.tapd_iterations_path,
        story_tags_path=getattr(cfg, "tapd_story_tags_path", "/story_tags"),
        story_attachments_path=getattr(cfg, "tapd_story_attachments_path", "/story_attachments"),
        story_comments_path=getattr(cfg, "tapd_story_comments_path", "/story_comments"),
    )
    notion = NotionWrapper(cfg.notion_token or "", cfg.notion_requirement_db_id or "")

    if wipe_first and not dry_run:
        print("[sync-mod] wipe-first enabled: clearing Notion database (archiving all pages) and switching to full fetch")
        try:
            cleared = notion.clear_database()
            print(f"[sync-mod] cleared pages={cleared}")
        except Exception as e:
            print(f"[sync-mod] clear failed: {e}")
        last = None

    # base filters
    base_filters: Dict[str, object] = {}
    # NOTE: owner kept local to allow substring matching
    if creator or cfg.tapd_only_creator:
        base_filters["creator"] = creator or cfg.tapd_only_creator
    cur_iter = None
    detected_iter_key: Optional[str] = None
    if current_iteration or getattr(cfg, 'tapd_use_current_iteration', False):
        try:
            cur_iter = tapd.get_current_iteration()
        except Exception:
            cur_iter = None
        if cur_iter:
            it_id = cur_iter.get('id') or cur_iter.get('iteration_id')
            if it_id:
                candidates = getattr(cfg, 'tapd_filter_iteration_id_keys', []) or ['iteration_id']
                for k in candidates:
                    try:
                        probe = tapd._get(tapd.stories_path, params={
                            'workspace_id': cfg.tapd_workspace_id,
                            k: it_id,
                            'page': 1,
                            'limit': 1,
                            'with_v_status': 1,
                        })
                        data = probe.get('data') if isinstance(probe, dict) else None
                        ok = False
                        if isinstance(data, list) and len(data) > 0:
                            ok = True
                        elif isinstance(data, dict):
                            for key in ('stories','list','items'):
                                if isinstance(data.get(key), list) and data.get(key):
                                    ok = True
                                    break
                        if ok:
                            detected_iter_key = k
                            break
                    except Exception:
                        continue
                (detected_iter_key := detected_iter_key or (candidates[0] if candidates else None))
                if detected_iter_key:
                    base_filters[detected_iter_key] = it_id

    # Local filter helpers (same as above)
    owner_subs: list[str] = []
    if owner or cfg.tapd_only_owner:
        owner_subs = [s.strip() for s in str(owner or cfg.tapd_only_owner).split(',') if s.strip()]

    def owner_matches(story: dict) -> bool:
        return story_matches_owner(story, owner_subs)

    def iteration_matches(story: dict) -> bool:
        if not cur_iter:
            return True
        want = str(cur_iter.get('id') or cur_iter.get('iteration_id') or '')
        if not want:
            return True
        for key in ('iteration_id', 'sprint_id', 'iteration'):
            v = story.get(key)
            if v is None:
                continue
            if str(v) == want:
                return True
        return False

    # Preload modules to compute path/labels
    modules = list(tapd.list_modules())
    # Build quick index for parent traversal if needed
    by_id: Dict[str, dict] = {}
    for m in modules:
        mid = str(m.get("id", ""))
        if mid:
            by_id[mid] = m

    def module_path(mod: dict) -> str:
        # 1) direct full path fields if present
        for key in ("path", "full_path", "fullName", "fullname", "name_path", "module_path"):
            val = mod.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        # 2) try reconstruct from parent pointer
        names = []
        seen = set()
        cur = mod
        depth = 0
        while cur and depth < 20:
            nm = cur.get("name") or cur.get("label") or cur.get("title") or cur.get("id")
            if nm:
                names.append(str(nm))
            # guess parent id field names
            pid = cur.get("parent_id") or cur.get("parent") or cur.get("pid") or cur.get("parent_module_id")
            pid = str(pid) if pid is not None else ""
            if not pid or pid in seen:
                break
            seen.add(pid)
            cur = by_id.get(pid)
            depth += 1
        names.reverse()
        sep = cfg.module_path_sep if hasattr(cfg, "module_path_sep") else "/"
        return sep.join([s for s in names if s])

    def module_label_for_notion(mod: dict) -> str:
        if getattr(cfg, "notion_module_value", "name") == "path":
            p = module_path(mod)
            return p or (mod.get("name") or mod.get("id") or "")
        return mod.get("name") or mod.get("id") or ""

    # Preload existing index once when insert-only
    existing_idx: Dict[str, str] = {}
    if insert_only and notion.client:
        try:
            existing_idx = notion.existing_index()
            print(f"[sync-mod] insert-only: existing TAPD_ID count={len(existing_idx)}")
        except Exception as e:
            print(f"[sync-mod] build existing index failed: {e}")

    extras_cache: Dict[str, Dict[str, Any]] = {}

    total = 0
    created_total = 0
    existing_total = 0
    skipped_total = 0
    for mod in modules:
        mod_id = mod.get("id")
        mod_name = mod.get("name") or mod_id
        mod_label = module_label_for_notion(mod)
        print(f"[sync-mod] module={mod_label} (id={mod_id})")
        filters = dict(base_filters)
        # Allow explicit override key for maximum control
        if cfg.tapd_module_filter_key:
            key = cfg.tapd_module_filter_key
            # Heuristic: if key contains 'id', send id; else send name
            if mod_id and ("id" in key.lower()):
                filters[key] = mod_id
            else:
                filters[key] = mod_name
        else:
            # Send multiple candidates for better compatibility
            for k in getattr(cfg, "tapd_filter_module_id_keys", []) or ["module_id"]:
                if mod_id:
                    filters[k] = mod_id
            for k in getattr(cfg, "tapd_filter_module_name_keys", []) or ["module"]:
                if mod_name:
                    filters[k] = mod_name

        # Creation guard for this module batch
        creation_owner = cfg.creation_owner_substr or (owner or cfg.tapd_only_owner)
        creation_owner_subs: list[str] = []
        if creation_owner:
            creation_owner_subs = [s.strip() for s in str(creation_owner).split(',') if s.strip()]
        require_cur_iter_for_create = getattr(cfg, 'creation_require_current_iteration', True)
        if require_cur_iter_for_create and not cur_iter:
            try:
                cur_iter = tapd.get_current_iteration()
            except Exception:
                cur_iter = None

        def owner_matches_creation(story: dict) -> bool:
            return story_matches_owner(story, creation_owner_subs)

        def iteration_matches_creation(story: dict) -> bool:
            if not require_cur_iter_for_create:
                return True
            if not cur_iter:
                return False
            want = str(cur_iter.get('id') or cur_iter.get('iteration_id') or '')
            if not want:
                return False
            for key in ('iteration_id', 'sprint_id', 'iteration'):
                v = story.get(key)
                if v is None:
                    continue
                if str(v) == want:
                    return True
            return False

        count = 0
        for story in tapd.list_stories(updated_since=last, filters=filters or None):
            count += 1
            total += 1
            if not owner_matches(story):
                continue
            if not iteration_matches(story):
                continue
            # Ensure downstream Notion mapping sees the chosen module label
            if mod_label:
                story.setdefault("module", mod_label)
            enrich_story_with_extras(tapd, cfg, story, cache=extras_cache, ctx="sync-mod")
            text = story.get("description") or ""
            res = analyze(text)
            props = map_story_to_notion_properties(story)
            blocks = build_page_blocks_from_story(story, cfg=cfg)
            raw_id = story.get('id')
            tapd_id = str(raw_id) if raw_id is not None else ''
            if not tapd_id or tapd_id in ('', 'None', 'unknown'):
                print(f"[sync-mod] skip story without valid TAPD id in module={mod_name}")
                continue
            if insert_only and tapd_id:
                exists = tapd_id in existing_idx if existing_idx else bool(notion.find_page_by_tapd_id(tapd_id))
                if exists:
                    print(f"[sync-mod] skip existing module={mod_name} TAPD_ID={tapd_id}")
                    existing_total += 1
                    continue
                if not owner_matches_creation(story) or not iteration_matches_creation(story):
                    print(f"[sync-mod] skip create module={mod_name} TAPD_ID={tapd_id} (not owned/current-iter)")
                    skipped_total += 1
                    continue
                if dry_run:
                    print(f"[sync-mod] would create module={mod_name} TAPD_ID={tapd_id} title={props.get('Name')}")
                    created_total += 1
                else:
                    page_id = notion.create_story_page(story, blocks)
                    print(f"[sync-mod] created module={mod_name} page {page_id}")
                    created_total += 1
            else:
                if dry_run:
                    existing_page = notion.find_page_by_tapd_id(tapd_id) or notion.find_page_by_title(story.get('name') or story.get('title') or '')
                    if existing_page:
                        print(f"[sync-mod] would update module={mod_name} TAPD_ID={tapd_id} title={props.get('Name')}")
                        existing_total += 1
                    elif owner_matches_creation(story) and iteration_matches_creation(story):
                        print(f"[sync-mod] would create module={mod_name} TAPD_ID={tapd_id} title={props.get('Name')}")
                        created_total += 1
                    else:
                        print(f"[sync-mod] skip create module={mod_name} TAPD_ID={tapd_id} (not owned/current-iter)")
                        skipped_total += 1
                else:
                    existing_page = notion.find_page_by_tapd_id(tapd_id) or notion.find_page_by_title(story.get('name') or story.get('title') or '')
                    if existing_page:
                        blocks = build_page_blocks_from_story(story, cfg=cfg)
                        page_id = notion.upsert_story_page(story, blocks)
                        print(f"[sync-mod] upserted module={mod_name} page {page_id}")
                        existing_total += 1
                    else:
                        if owner_matches_creation(story) and iteration_matches_creation(story):
                            page_id = notion.create_story_page(story, blocks)
                            print(f"[sync-mod] created module={mod_name} page {page_id}")
                            created_total += 1
                        else:
                            print(f"[sync-mod] skip create module={mod_name} TAPD_ID={tapd_id} (not owned/current-iter)")
                            skipped_total += 1
        print(f"[sync-mod] done module={mod_name} items={count}")

    duration = time.perf_counter() - start_ts
    print(
        f"[sync-mod] done | total_items={total} | created={created_total} | "
        f"existing={existing_total} | skipped={skipped_total} | duration={duration:.2f}s"
    )
    return SyncResult(
        total=total,
        created=created_total,
        existing=existing_total,
        skipped=skipped_total,
        duration=duration,
        dry_run=dry_run,
    )
