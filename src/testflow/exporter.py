from __future__ import annotations

import json
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List
from zipfile import ZIP_DEFLATED, ZipFile

from .models import GeneratedAttachment, TestCase, TesterSuite


def export_suite_to_xmind(suite: TesterSuite, base_dir: Path, run_time: datetime) -> GeneratedAttachment:
    base_dir.mkdir(parents=True, exist_ok=True)
    timestamp = run_time.strftime("%Y%m%d_%H%M%S")
    safe_name = _sanitize_filename(suite.tester.name or "tester")
    file_path = base_dir / f"{safe_name}_{timestamp}.xmind"
    workbook = _build_workbook_payload(suite, run_time)
    metadata = _build_metadata(run_time)
    manifest = _build_manifest()
    styles = {"styles": []}
    with ZipFile(file_path, "w", compression=ZIP_DEFLATED) as zf:
        zf.writestr("content.json", json.dumps(workbook, ensure_ascii=False, indent=2))
        zf.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False))
        zf.writestr("styles.json", json.dumps(styles, ensure_ascii=False))
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
    return GeneratedAttachment(
        tester=suite.tester,
        file_path=file_path,
        case_count=len(suite.cases),
    )


def _build_workbook_payload(suite: TesterSuite, run_time: datetime) -> List[Dict[str, object]]:
    workbook_id = _uuid()
    root_topic_id = _uuid()
    root_title = f"{suite.tester.name} - {run_time.strftime('%Y-%m-%d')}"
    story_groups: Dict[str, List[TestCase]] = defaultdict(list)
    story_titles: Dict[str, str] = {}
    for case in suite.cases:
        story_groups[case.story_id].append(case)
        story_titles[case.story_id] = case.story_title
    children = []
    for story_id, cases in story_groups.items():
        story_topic_id = _uuid()
        case_children = []
        for case in cases:
            topic_id = _uuid()
            note = _render_case_note(case)
            labels = _build_labels(case)
            topic = {
                "id": topic_id,
                "title": _shorten(case.title, 120),
                "notes": {"plain": {"content": note}},
            }
            if labels:
                topic["labels"] = labels
            case_children.append(topic)
        story_topic = {
            "id": story_topic_id,
            "title": _shorten(f"{story_titles.get(story_id, '需求')} ({story_id})", 140),
            "children": {"attached": case_children},
        }
        children.append(story_topic)
    workbook = [
        {
            "id": workbook_id,
            "class": "org.xmind.ui.mindmap.MindMap",
            "title": suite.tester.name,
            "rootTopic": {
                "id": root_topic_id,
                "title": root_title,
                "structureClass": "org.xmind.ui.logic.right",
                "children": {"attached": children},
            },
        }
    ]
    return workbook


def _build_metadata(run_time: datetime) -> Dict[str, object]:
    ts = int(time.mktime(run_time.timetuple()) * 1000)
    return {
        "creatorName": "TestFlow",
        "creatorVersion": "0.1",
        "modifierName": "TestFlow",
        "modifierVersion": "0.1",
        "creatingTime": ts,
        "modifyingTime": ts,
    }


def _build_manifest() -> Dict[str, object]:
    return {
        "file-entries": {
            "content.json": {
                "media-type": "application/vnd.xmind.content",
                "version": "2.0",
            },
            "metadata.json": {
                "media-type": "application/vnd.xmind.metadata",
                "version": "2.0",
            },
            "styles.json": {
                "media-type": "application/vnd.xmind.styles",
                "version": "2.0",
            },
        }
    }


def _render_case_note(case: TestCase) -> str:
    lines: List[str] = []
    if case.summary:
        lines.append(f"概述：{case.summary}")
    extra_map = dict(case.extra)
    test_points = extra_map.pop("测试点列表", None)
    if test_points:
        points = _ensure_iterable(test_points)
        if points:
            lines.append("测试点：")
            for idx, point in enumerate(points, 1):
                lines.append(f"{idx}. {point}")
    if case.preconditions:
        lines.append("前置条件：")
        for cond in case.preconditions:
            lines.append(f"- {cond}")
    if case.steps:
        lines.append("测试步骤：")
        for idx, step in enumerate(case.steps, 1):
            lines.append(f"{idx}. {step}")
    if case.expected_results:
        lines.append("期望结果：")
        for idx, expect in enumerate(case.expected_results, 1):
            lines.append(f"{idx}. {expect}")
    if case.priority:
        lines.append(f"优先级：{case.priority}")
    if case.risk:
        lines.append(f"风险：{case.risk}")
    if extra_map:
        for key, value in extra_map.items():
            lines.append(f"{key}：{value}")
    return "\n".join(lines) if lines else "待补充"


def _ensure_iterable(value) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if "\n" in text:
            return [line.strip() for line in text.splitlines() if line.strip()]
        if "；" in text or ";" in text:
            return [seg.strip() for seg in text.replace("；", ";").split(";") if seg.strip()]
        return [text]
    return []


def _build_labels(case: TestCase) -> List[str]:
    labels: List[str] = []
    if case.priority:
        labels.append(case.priority)
    if case.risk:
        labels.append(case.risk)
    return labels


def _uuid() -> str:
    return uuid.uuid4().hex


def _sanitize_filename(raw: str) -> str:
    text = raw or "tester"
    text = re.sub(r"[^0-9A-Za-z\-_.\u4e00-\u9fa5]", "_", text)
    return text[:80]


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
