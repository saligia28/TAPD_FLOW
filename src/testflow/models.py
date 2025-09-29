from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional


@dataclass
class TesterContact:
    """Contact info loaded from config/testers.json."""

    __test__ = False
    name: str
    email: str
    aliases: List[str] = field(default_factory=list)
    modules: List[str] = field(default_factory=list)

    def all_names(self) -> List[str]:
        return [self.name, *self.aliases]


@dataclass
class TestCase:
    """Generated test case payload."""

    __test__ = False
    story_id: str
    story_title: str
    tester: str
    title: str
    summary: Optional[str] = None
    preconditions: List[str] = field(default_factory=list)
    steps: List[str] = field(default_factory=list)
    expected_results: List[str] = field(default_factory=list)
    priority: Optional[str] = None
    risk: Optional[str] = None
    extra: Dict[str, object] = field(default_factory=dict)


@dataclass
class TesterSuite:
    tester: TesterContact
    cases: List[TestCase] = field(default_factory=list)

    __test__ = False
    def add_case(self, case: TestCase) -> None:
        self.cases.append(case)


@dataclass
class TestFlowOptions:
    owner: Optional[str] = None
    creator: Optional[str] = None
    current_iteration: bool = False
    limit: Optional[int] = None
    execute: bool = False
    send_mail: bool = False
    ack_pull: Optional[str] = None
    ack_mail: Optional[str] = None


@dataclass
class GeneratedAttachment:
    tester: TesterContact
    file_path: Path
    case_count: int


@dataclass
class MailJob:
    contact: TesterContact
    subject: str
    body: str
    attachments: List[Path]


@dataclass
class MailResult:
    contact: TesterContact
    sent: bool
    dry_run: bool
    message: str = ""


@dataclass
class TestFlowResult:
    total_stories: int
    total_cases: int
    suites: List[TesterSuite]
    attachments: List[GeneratedAttachment]
    mails: Dict[str, str] = field(default_factory=dict)
    started_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: datetime = field(default_factory=datetime.utcnow)

    def summary(self) -> str:
        testers = len(self.suites)
        return (
            f"stories={self.total_stories} cases={self.total_cases} "
            f"testers={testers} attachments={len(self.attachments)}"
        )
