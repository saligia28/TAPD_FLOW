from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .models import TesterContact


DEFAULT_TESTER_CONTACT = TesterContact(
    name="默认测试人员",
    email="saligia28@126.com",
    aliases=["默认", "saligia", "江林QA"],
)


@dataclass
class TesterRegistry:
    contacts: List[TesterContact]
    index: Dict[str, TesterContact]

    __test__ = False
    def lookup(self, token: str) -> Optional[TesterContact]:
        key = _normalize(token)
        if not key:
            return None
        return self.index.get(key)

    def resolve_tokens(self, tokens: Iterable[str]) -> List[TesterContact]:
        resolved: List[TesterContact] = []
        for token in tokens:
            contact = self.lookup(token)
            if contact and contact not in resolved:
                resolved.append(contact)
        return resolved

    def default(self) -> TesterContact:
        return self.contacts[0] if self.contacts else DEFAULT_TESTER_CONTACT


def load_testers(path: str) -> TesterRegistry:
    file_path = Path(path)
    contacts = _load_contacts_from_file(file_path)
    if not contacts:
        # keep deterministic default for first run
        contacts = [DEFAULT_TESTER_CONTACT]
        print(f"[testflow] tester config missing or empty, fallback to {contacts[0].email}")
    index = _build_index(contacts)
    return TesterRegistry(contacts=contacts, index=index)


def _load_contacts_from_file(file_path: Path) -> List[TesterContact]:
    if not file_path.exists():
        return []
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[testflow] failed to read tester config {file_path}: {exc}")
        return []
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as exc:
        print(f"[testflow] invalid JSON in {file_path}: {exc}")
        return []
    items: List[TesterContact] = []
    entries = raw.get("testers") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        email = str(entry.get("email") or "").strip()
        if not name or not email:
            continue
        aliases = _normalize_list(entry.get("aliases"))
        modules = _normalize_list(entry.get("modules"))
        items.append(TesterContact(name=name, email=email, aliases=aliases, modules=modules))
    return items


def _normalize_list(value: Optional[Iterable[str]]) -> List[str]:
    if not value:
        return []
    out: List[str] = []
    for item in value:
        if not item:
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _build_index(contacts: Iterable[TesterContact]) -> Dict[str, TesterContact]:
    index: Dict[str, TesterContact] = {}
    for contact in contacts:
        for token in contact.all_names():
            key = _normalize(token)
            if key and key not in index:
                index[key] = contact
    return index


def _normalize(token: str) -> str:
    return "".join(ch for ch in token.lower().strip() if not ch.isspace()) if token else ""


TESTER_FIELD_HINTS: Tuple[str, ...] = (
    "tester",
    "test_owner",
    "test_owner_name",
    "qa",
    "qa_owner",
    "qa_assignee",
    "测试",
    "测试人员",
    "测试负责人",
    "qa负责人",
)


def extract_story_testers(story: Dict[str, object]) -> List[str]:
    tokens: List[str] = []
    queue: List[Tuple[str, object]] = []
    for key, value in story.items():
        queue.append((str(key), value))
    while queue:
        key, value = queue.pop()
        if isinstance(value, dict):
            for sub_key, sub_val in value.items():
                queue.append((f"{key}.{sub_key}", sub_val))
            continue
        if isinstance(value, (list, tuple, set)):
            for idx, item in enumerate(value):
                queue.append((f"{key}[{idx}]", item))
            continue
        if isinstance(value, (str, int, float)):
            if _looks_like_tester_field(key):
                text = str(value).strip()
                if text:
                    for part in _split_candidates(text):
                        if part not in tokens:
                            tokens.append(part)
    return tokens


def _looks_like_tester_field(key: str) -> bool:
    low = key.lower()
    for hint in TESTER_FIELD_HINTS:
        if hint in low:
            return True
    return False


def _split_candidates(raw: str) -> List[str]:
    seps = [",", "，", ";", "；", "|", "/", " "]
    # replace separators with comma and split once
    text = raw
    for sep in seps:
        text = text.replace(sep, ",")
    items = [item.strip() for item in text.split(",") if item.strip()]
    # remove duplicates while preserving order
    seen = set()
    result: List[str] = []
    for item in items:
        norm = _normalize(item)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        result.append(item)
    return result
