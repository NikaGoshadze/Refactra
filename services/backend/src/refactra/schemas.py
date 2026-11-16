from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MockRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_text: str = Field(min_length=1, max_length=4000)

    @field_validator("task_text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a nonblank task.")
        return value


class StopReason(BaseModel):
    code: str
    message: str


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
