from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from refactra.repository_revision import validate_branch


class RunTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_text: str = Field(min_length=1, max_length=4000)

    @field_validator("task_text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("Enter a nonblank task.")
        return value


class MockRunCreate(RunTask):
    pass


class RepositoryRunCreate(RunTask):
    repository_id: int = Field(strict=True, gt=0, le=2**63 - 1)
    base_branch: str = Field(min_length=1, max_length=255)

    @field_validator("base_branch")
    @classmethod
    def literal_branch(cls, value: str) -> str:
        validate_branch(value)
        return value


class RepositoryRunReceipt(BaseModel):
    run_id: UUID
    repository_id: int
    base_branch: str
    base_sha: str
    created_at: datetime


class StopReason(BaseModel):
    code: str
    message: str


class RepositoryRunRead(RepositoryRunReceipt):
    status: Literal["CREATED", "ANALYZING", "NEEDS_HUMAN_REVIEW", "FAILED"]
    updated_at: datetime
    stop_reason: StopReason | None


class MockRunRead(BaseModel):
    run_id: UUID
    mode: Literal["mock"] = "mock"
    status: Literal["CREATED", "ANALYZING", "NEEDS_HUMAN_REVIEW", "FAILED"]
    task_text: str
    version: int
    workflow_version: str
    created_at: datetime
    updated_at: datetime
    stop_reason: StopReason | None
    status_url: str
    # Null means not measured/executed; mock results never claim a successful check or PR.
    validation: None = None
    pull_request: None = None


class EventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    event_type: str
    payload: dict
    occurred_at: datetime


class EventPage(BaseModel):
    items: list[EventRead]
    next_after_id: int
    has_more: bool
