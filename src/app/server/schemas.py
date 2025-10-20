"""Pydantic response/request models for the server APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .jobs import JobStatus

__all__ = [
    "JobCreateRequest",
    "JobResponse",
    "JobSummaryResponse",
    "StoryCollectionResponse",
    "StoryOwnerAggregateResponse",
    "StoryQuickOwnerAggregateResponse",
    "StorySummaryResponse",
]


class JobCreateRequest(BaseModel):
    action_id: str = Field(..., alias="actionId")
    extra_args: List[str] = Field(default_factory=list, alias="extraArgs")
    args: Optional[List[str]] = None
    story_ids: List[str] = Field(default_factory=list, alias="storyIds")


class JobResponse(BaseModel):
    id: str
    actionId: str
    title: str
    status: JobStatus
    command: List[str]
    displayCommand: str
    createdAt: datetime
    startedAt: datetime | None
    finishedAt: datetime | None
    exitCode: int | None
    cancelRequested: bool
    logs: List[Dict[str, object]]
    nextCursor: int


class JobSummaryResponse(BaseModel):
    id: str
    actionId: str
    title: str
    status: JobStatus
    createdAt: datetime
    finishedAt: datetime | None
    exitCode: int | None


class StorySummaryResponse(BaseModel):
    id: str
    title: str
    status: Optional[str]
    owners: List[str]
    iteration: Optional[str]
    updatedAt: Optional[str]
    frontend: Optional[str] = None
    url: Optional[str] = None


class StoryOwnerAggregateResponse(BaseModel):
    name: str
    count: int


class StoryQuickOwnerAggregateResponse(BaseModel):
    name: str
    owners: List[str]
    count: int


class StoryCollectionResponse(BaseModel):
    stories: List[StorySummaryResponse]
    total: int
    owners: List[StoryOwnerAggregateResponse]
    quickOwners: List[StoryQuickOwnerAggregateResponse]
    truncated: bool = False
