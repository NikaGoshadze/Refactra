"""Internal checkpoint for versioned plan proposals based on saved analysis."""

import json
import re
from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, undefer

from refactra.analysis_context import load_analysis_context
from refactra.models import Run, RunAnalysisResult, RunEvent, RunPlan

MAX_PLAN_BYTES = 64 * 1024
SUPPORTED_PLAN_CHECK_IDS = ("python.pytest",)
PlanText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")]


class PlanRevisionConflict(ValueError):
    """The run or its current revision no longer permits this save."""


def _relative_path(path: str) -> str:
    if re.search(r"[\x00-\x1f\x7f\\:]", path) or any(
        part in {"", ".", ".."} for part in path.split("/")
    ):
        raise ValueError("Use a repository-relative path.")
    return path


PlanPath = Annotated[
    str, StringConstraints(min_length=1, max_length=1024), AfterValidator(_relative_path)
]


class _PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class ProposedChange(_PlanModel):
    path: PlanPath
    action: Literal["CREATE", "MODIFY", "DELETE"]
    intent: PlanText


class AcceptanceCriterion(_PlanModel):
    id: Identifier
    description: PlanText


class ValidationCheck(_PlanModel):
    check_id: Identifier
    paths: Annotated[list[PlanPath], Field(max_length=10)]
    required: bool
    criteria_ids: Annotated[list[Identifier], Field(min_length=1, max_length=16)]


class PlanProposal(_PlanModel):
    summary: PlanText
    changes: Annotated[list[ProposedChange], Field(min_length=1, max_length=10)]
    acceptance_criteria: Annotated[list[AcceptanceCriterion], Field(min_length=1, max_length=16)]
    validation_checks: Annotated[list[ValidationCheck], Field(min_length=1, max_length=12)]
    preserve_behavior: Annotated[list[PlanText], Field(min_length=1, max_length=16)]
    assumptions: Annotated[list[PlanText], Field(max_length=16)]
    blocking_questions: Annotated[list[PlanText], Field(max_length=16)]

    @model_validator(mode="after")
    def check_references(self):
        if len({change.path for change in self.changes}) != len(self.changes):
            raise ValueError("Each path must have one proposed action.")
        criteria = {item.id for item in self.acceptance_criteria}
        if len(criteria) != len(self.acceptance_criteria):
            raise ValueError("Acceptance criterion IDs must be unique.")
        if any(not criteria.issuperset(check.criteria_ids) for check in self.validation_checks):
            raise ValueError("Validation checks must reference criteria in the same plan.")
        if not any(check.required for check in self.validation_checks):
            raise ValueError("At least one validation check must be required.")
        return self


class PlanRepository(BaseModel):
    repository_id: int
    base_branch: str
    base_sha: str


class PlanContent(PlanProposal):
    schema_version: Literal["1"]
    run_id: UUID
    repository: PlanRepository


class PlanReadiness(BaseModel):
    blocking_question_count: int
    unsupported_check_ids: list[str]
    has_required_supported_check: bool


def evaluate_plan_readiness(proposal: PlanProposal) -> PlanReadiness:
    """Check proposal metadata against the initial Python validation catalog."""
    return PlanReadiness(
        blocking_question_count=len(proposal.blocking_questions),
        unsupported_check_ids=sorted(
            {
                check.check_id
                for check in proposal.validation_checks
                if check.check_id not in SUPPORTED_PLAN_CHECK_IDS
            }
        ),
        has_required_supported_check=any(
            check.required and check.check_id in SUPPORTED_PLAN_CHECK_IDS
            for check in proposal.validation_checks
        ),
    )


def load_refactoring_plan(
    session: Session, run_id: UUID, *, revision: int | None = None
) -> RunPlan | None:
    query = select(RunPlan).options(undefer(RunPlan.document)).where(RunPlan.run_id == run_id)
    if revision is not None:
        query = query.where(RunPlan.revision == revision)
    return session.scalar(query.order_by(RunPlan.revision.desc()).limit(1))


def save_refactoring_plan(
    session: Session, run_id: UUID, proposal: dict, *, expected_revision: int
) -> RunPlan:
    """Caller authorizes the run and commits. The server supplies repository identity."""
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("An expected plan revision of zero or higher is required.")
    payload = PlanProposal.model_validate(proposal).model_dump(mode="json")
    run = session.get(Run, run_id, with_for_update=True, populate_existing=True)
    if (
        run is None
        or run.workflow_version != "repository-v0.1"
        or run.status != "NEEDS_HUMAN_REVIEW"
        or session.get(RunAnalysisResult, run_id) is None
        or load_analysis_context(session, run_id) is None
    ):
        raise PlanRevisionConflict("A repository run with completed analysis is required.")
    payload.update(
        schema_version="1",
        run_id=str(run_id),
        repository={
            "repository_id": run.github_repository_id,
            "base_branch": run.base_branch,
            "base_sha": run.base_sha,
        },
    )
    document = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    if len(document) > MAX_PLAN_BYTES:
        raise ValueError("Plan exceeds the 64 KiB storage limit.")
    digest = sha256(document).hexdigest()
    latest = load_refactoring_plan(session, run_id)
    if latest is not None and latest.sha256 == digest:
        return latest
    revision = latest.revision if latest is not None else 0
    if expected_revision != revision:
        raise PlanRevisionConflict("Plan revision changed. Reload it before saving a revision.")
    plan = RunPlan(run_id=run_id, revision=revision + 1, sha256=digest, document=document)
    session.add(plan)
    session.flush()
    session.add(
        RunEvent(
            run_id=run_id,
            event_type="PLAN_SAVED",
            payload={"plan_id": str(plan.id), "revision": plan.revision, "sha256": digest},
        )
    )
    return plan
