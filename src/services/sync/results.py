"""Structured results returned by sync routines."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["SyncResult", "UpdateAllResult"]


@dataclass
class SyncResult:
    total: int
    created: int
    existing: int
    skipped: int
    duration: float
    dry_run: bool


@dataclass
class UpdateAllResult:
    scanned: int
    updated: int
    skipped: int
    duration: float
    dry_run: bool
