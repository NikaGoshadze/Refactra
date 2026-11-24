"""Internal checkpoint for versioned plan proposals based on saved analysis."""

import json
import re
from collections.abc import Sequence
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, undefer

from refactra.analysis_context import load_analysis_context
from refactra.models import Run, RunAnalysisResult, RunCommand, RunEvent, RunPlan, RunPlanApproval
from refactra.repository_selection import BLOCKED_PARTS, is_excluded_source_path
from refactra.request_coverage import requested_paths

MAX_PLAN_BYTES = 64 * 1024
PATCH_CHECK = "repository.patch"
TYPESCRIPT_CHECK = "typescript.tsc"
ASSET_CHECK = "repository.asset"
FLASK_CHECK = "python.flask"
GO_CHECK = "go.test"
SUPPORTED_PLAN_CHECK_IDS = (
    "python.pytest",
    FLASK_CHECK,
    GO_CHECK,
    PATCH_CHECK,
    TYPESCRIPT_CHECK,
    ASSET_CHECK,
)
APPLY_PLAN_APPROVAL = "APPLY_PLAN_APPROVAL"
GENERATE_PLAN = "GENERATE_PLAN"
VALIDATE_CANDIDATE = "VALIDATE_CANDIDATE"
PLAN_FEEDBACK_REQUESTED = "PLAN_FEEDBACK_REQUESTED"
PROTECTED_PYTHON_FILES = {"conftest.py", "setup.py", "sitecustomize.py", "usercustomize.py"}
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


_NO_QUESTION = re.compile(
    r"(?:none(?: (?:needed|required))?|n/a|not applicable|"
    r"no (?:(?:blocking |open |outstanding )?questions|(?:additional |further )?clarification)"
    r"(?: (?:needed|required))?)(?: at this (?:stage|time))?"
)
_ENOUGH_INFORMATION = re.compile(
    r"(?:the )?(?:(?:provided|available|current|pinned|repository) )*"
    r"(?:inventory|context|information|details|request|requirements|scope|task) (?:is|are) "
    r"(?:sufficient|clear|complete|well[- ]defined)"
    r"(?: to (?:proceed|plan(?: the refactor)?|"
    r"propose (?:the|a) (?:refactor|plan|refactoring plan)))?"
    r"(?: without (?:additional |further )?(?:scope )?clarification)?"
)


def _question_placeholder(text: str) -> bool:
    normalized = " ".join(text.casefold().split()).rstrip(".!")
    if _NO_QUESTION.fullmatch(normalized) or _ENOUGH_INFORMATION.fullmatch(normalized):
        return True
    head, _, explanation = normalized.partition(".")
    return bool(_NO_QUESTION.fullmatch(head) and _ENOUGH_INFORMATION.fullmatch(explanation.strip()))


class PlanProposal(_PlanModel):
    summary: PlanText
    changes: Annotated[list[ProposedChange], Field(max_length=10)]
    acceptance_criteria: Annotated[list[AcceptanceCriterion], Field(max_length=16)]
    validation_checks: Annotated[list[ValidationCheck], Field(max_length=12)]
    preserve_behavior: Annotated[list[PlanText], Field(max_length=16)]
    assumptions: Annotated[list[PlanText], Field(max_length=16)]
    blocking_questions: Annotated[list[PlanText], Field(max_length=16)]

    @field_validator("blocking_questions")
    @classmethod
    def remove_question_placeholders(cls, questions: list[str]) -> list[str]:
        return [question for question in questions if not _question_placeholder(question)]

    @model_validator(mode="after")
    def check_references(self):
        if not self.changes:
            if self.validation_checks:
                raise ValueError("A plan without changes cannot require validation.")
            return self
        if not self.acceptance_criteria or not self.preserve_behavior:
            raise ValueError("Describe the acceptance criteria and behavior to preserve.")
        if len({change.path for change in self.changes}) != len(self.changes):
            raise ValueError("Each path must have one proposed action.")
        criteria = {item.id for item in self.acceptance_criteria}
        if len(criteria) != len(self.acceptance_criteria):
            raise ValueError("Acceptance criterion IDs must be unique.")
        if any(not criteria.issuperset(check.criteria_ids) for check in self.validation_checks):
            raise ValueError("Validation checks must reference criteria in the same plan.")
        return self


class PlanRepository(BaseModel):
    repository_id: int
    base_branch: str
    base_sha: str


class PlanContent(PlanProposal):
    schema_version: Literal["1"]
    run_id: UUID
    repository: PlanRepository
    requested_files: dict[PlanPath, Literal["CREATE", "MODIFY", "DELETE"]] = Field(
        default_factory=dict
    )
    inventory_errors: list[str] = Field(default_factory=list)


class PlanReadiness(BaseModel):
    outcome: Literal["ready", "needs_clarification", "unsupported"]
    blocking_question_count: int
    unsupported_check_ids: list[str]
    has_required_supported_check: bool
    scope_errors: list[str]


def is_env_path(value: str) -> bool:
    name = PurePosixPath(value).name.casefold()
    return bool(re.fullmatch(r"\.env(?:[._-][a-z0-9]+)*", name))


def plan_edit_kind(value: str, action: str) -> Literal["text", "env_delete", "env_template"] | None:
    path = PurePosixPath(value)
    if any(part.startswith(".") or part.casefold() in BLOCKED_PARTS for part in path.parts[:-1]):
        return None
    if is_env_path(value):
        if action == "DELETE":
            return "env_delete"
        if action in {"CREATE", "MODIFY"} and re.search(
            r"[._-](example|sample|template|dist)$", path.name.casefold()
        ):
            return "env_template"
        return None
    if path.name.casefold() in PROTECTED_PYTHON_FILES or is_excluded_source_path(path):
        return None
    return "text"


def is_typescript_config(path: str) -> bool:
    return bool(re.fullmatch(r"tsconfig(?:\.[\w-]+)*\.json", PurePosixPath(path).name))


def approved_file_operation(
    changes: Sequence[ProposedChange], checks: Sequence[ValidationCheck]
) -> Literal["env_delete", "asset_replace"] | None:
    if len(changes) != 1 or len(checks) != 1:
        return None
    edit, check = changes[0], checks[0]
    if not check.required or check.paths != [edit.path]:
        return None
    kind = plan_edit_kind(edit.path, edit.action)
    if check.check_id == PATCH_CHECK and kind == "env_delete":
        return "env_delete"
    path = PurePosixPath(edit.path)
    if (
        edit.action == "MODIFY"
        and check.check_id == ASSET_CHECK
        and kind == "text"
        and path.suffix.lower()
        in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2"}
    ):
        return "asset_replace"
    return None


def validation_command_key(run_id: UUID, revision: int) -> str:
    return f"repository:candidate-validation:{run_id}:{revision}"


def evaluate_plan_readiness(proposal: PlanProposal) -> PlanReadiness:
    """Check proposal metadata against the supported edit and validation scopes."""
    scope_errors = [
        f"{edit.action} is unsupported for {edit.path}."
        for edit in proposal.changes
        if plan_edit_kind(edit.path, edit.action) is None
    ]
    if isinstance(proposal, PlanContent) and proposal.changes:
        planned = {edit.path: edit.action for edit in proposal.changes}
        scope_errors.extend(proposal.inventory_errors)
        scope_errors.extend(
            f"The request calls for {action} {path}, but the plan does not include that action."
            for path, action in proposal.requested_files.items()
            if planned.get(path) != action
        )
    unsupported_checks = sorted(
        {
            check.check_id
            for check in proposal.validation_checks
            if check.check_id not in SUPPORTED_PLAN_CHECK_IDS
        }
    )
    outcome = "ready"
    if proposal.blocking_questions:
        outcome = "needs_clarification"
    elif not proposal.changes or scope_errors:
        outcome = "unsupported"
    return PlanReadiness(
        outcome=outcome,
        scope_errors=list(dict.fromkeys(scope_errors)),
        blocking_question_count=len(proposal.blocking_questions),
        unsupported_check_ids=unsupported_checks,
        has_required_supported_check=bool(proposal.changes),
    )


def load_refactoring_plan(
    session: Session, run_id: UUID, *, revision: int | None = None
) -> RunPlan | None:
    query = select(RunPlan).options(undefer(RunPlan.document)).where(RunPlan.run_id == run_id)
    if revision is not None:
        query = query.where(RunPlan.revision == revision)
    return session.scalar(query.order_by(RunPlan.revision.desc()).limit(1))


class PlanFeedback(_PlanModel):
    expected_revision: int = Field(ge=1, le=2_147_483_647)
    notes: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
    answers: Annotated[
        list[Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]],
        Field(max_length=16),
    ]


def _feedback_prefix(run_id: UUID, revision: int) -> str:
    return f"repository:plan-feedback:{run_id}:{revision}:"


def plan_feedback_status(session: Session, plan: RunPlan) -> str | None:
    return session.scalar(
        select(RunCommand.status)
        .where(RunCommand.dedupe_key.startswith(_feedback_prefix(plan.run_id, plan.revision)))
        .order_by(RunCommand.created_at.desc(), RunCommand.id.desc())
        .limit(1)
    )


def load_plan_feedback(session: Session, run_id: UUID, command_id: UUID) -> dict | None:
    return session.scalar(
        select(RunEvent.payload).where(
            RunEvent.run_id == run_id,
            RunEvent.event_type == PLAN_FEEDBACK_REQUESTED,
            RunEvent.payload["command_id"].astext == str(command_id),
        )
    )


def queue_plan_feedback(session: Session, run_id: UUID, feedback: PlanFeedback) -> RunPlan:
    run = session.get(Run, run_id, with_for_update=True)
    plan = load_refactoring_plan(session, run_id)
    if (
        run is None
        or run.status != "NEEDS_HUMAN_REVIEW"
        or plan is None
        or plan.revision != feedback.expected_revision
        or session.get(RunPlanApproval, run_id)
    ):
        raise PlanRevisionConflict("Reload the latest unapproved plan before sending feedback.")
    payload = feedback.model_dump()
    digest = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:32]
    key = _feedback_prefix(run_id, plan.revision) + digest
    if session.scalar(select(RunCommand.id).where(RunCommand.dedupe_key == key)) is not None:
        return plan
    if plan_feedback_status(session, plan) in {"PENDING", "CLAIMED"}:
        raise PlanRevisionConflict("A plan update is already in progress.")
    questions = PlanContent.model_validate_json(plan.document).blocking_questions
    if len(feedback.answers) != len(questions) or not (feedback.notes or any(feedback.answers)):
        raise ValueError("Add feedback or answer a question from this plan.")
    command_id = uuid4()
    session.add(RunCommand(id=command_id, run_id=run_id, kind=GENERATE_PLAN, dedupe_key=key))
    session.add(
        RunEvent(
            run_id=run_id,
            event_type=PLAN_FEEDBACK_REQUESTED,
            payload={**payload, "command_id": str(command_id)},
        )
    )
    session.flush()
    return plan


def save_refactoring_plan(
    session: Session,
    run_id: UUID,
    proposal: dict,
    *,
    expected_revision: int,
    feedback_notes: str = "",
) -> RunPlan:
    """Caller authorizes the run and commits. The server supplies repository identity."""
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("An expected plan revision of zero or higher is required.")
    parsed = PlanProposal.model_validate(proposal)
    payload = parsed.model_dump(mode="json")
    run = session.get(Run, run_id, with_for_update=True, populate_existing=True)
    context = load_analysis_context(session, run_id) if run is not None else None
    if (
        run is None
        or run.workflow_version != "repository-v0.1"
        or run.status != "NEEDS_HUMAN_REVIEW"
        or session.get(RunAnalysisResult, run_id) is None
        or context is None
    ):
        raise PlanRevisionConflict("A repository run with completed analysis is required.")
    saved_context = json.loads(context.document)
    tracked = set(saved_context["inventory"]["file_paths"])
    withheld = {
        item["path"]
        for item in saved_context["content"]["skipped"]
        if item["reason"] == "possible_secret"
    }
    special_edits = [
        edit
        for edit in parsed.changes
        if plan_edit_kind(edit.path, edit.action) in {"env_delete", "env_template"}
    ]
    if approved_file_operation(parsed.changes, parsed.validation_checks) == "asset_replace":
        special_edits = parsed.changes
    if special_edits:
        for edit in special_edits:
            exists = edit.path in tracked
            if exists != (edit.action == "CREATE"):
                continue
            reason = "already exists in" if exists else "is absent from"
            payload.update(
                summary=f"{edit.path} {reason} the pinned commit.",
                changes=[],
                validation_checks=[],
                blocking_questions=[],
            )
            break
    latest = load_refactoring_plan(session, run_id)
    previous = json.loads(latest.document) if latest is not None else {}
    requirements = previous.get("requested_files", requested_paths(run.task_text, tracked))
    had_requirements = bool(requirements) or "requested_files" in previous
    if feedback_notes:
        requirements = requested_paths(feedback_notes, tracked, requirements)
    if requirements or had_requirements:
        payload["requested_files"] = requirements
        errors = [
            f"{edit['path']} "
            f"{'already exists in' if edit['path'] in tracked else 'is absent from'}"
            " the pinned commit."
            for edit in payload["changes"]
            if edit["path"] in requirements
            and (edit["action"] == "CREATE") == (edit["path"] in tracked)
        ]
        if errors:
            payload["inventory_errors"] = errors
    # Candidate generation would send these files' full text, which analysis already withheld.
    disclosure_errors = [
        f"{edit['path']} may contain a credential and cannot be sent to the model for editing."
        for edit in payload["changes"]
        if edit["path"] in withheld
        and edit["action"] != "CREATE"
        and plan_edit_kind(edit["path"], edit["action"]) == "text"
    ]
    if disclosure_errors:
        payload["inventory_errors"] = [*payload.get("inventory_errors", []), *disclosure_errors]
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
    if latest is not None and latest.sha256 == digest:
        return latest
    if session.get(RunPlanApproval, run_id) is not None:
        raise PlanRevisionConflict("This run already has an approved plan.")
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


def _require_reviewable_plan(run: Run, plan: RunPlan) -> PlanReadiness:
    try:
        content = PlanContent.model_validate_json(plan.document)
    except ValidationError:
        raise PlanRevisionConflict("Saved plan could not be verified.") from None
    if (
        content.run_id != run.id
        or content.repository.repository_id != run.github_repository_id
        or content.repository.base_branch != run.base_branch
        or content.repository.base_sha != run.base_sha
    ):
        raise PlanRevisionConflict("Saved plan does not match the pinned repository revision.")
    readiness = evaluate_plan_readiness(content)
    if readiness.blocking_question_count:
        raise PlanRevisionConflict("Resolve blocking questions before approving.")
    if not content.changes:
        raise PlanRevisionConflict("Approval requires a plan with proposed changes.")
    return readiness


def approval_command_key(run_id: UUID, revision: int) -> str:
    return f"repository:plan-approval:{run_id}:{revision}"


def load_approved_plan(session: Session, run: Run) -> RunPlan:
    """Recheck the persisted decision for a run locked by the caller."""
    approval = session.get(RunPlanApproval, run.id)
    plan = load_refactoring_plan(session, run.id)
    if (
        run.workflow_version != "repository-v0.1"
        or run.status != "NEEDS_HUMAN_REVIEW"
        or approval is None
        or plan is None
        or (approval.revision, approval.sha256, approval.approved_by)
        != (plan.revision, plan.sha256, run.requested_by)
    ):
        raise PlanRevisionConflict("This run does not have a matching plan approval.")
    _require_reviewable_plan(run, plan)
    return plan


def approve_refactoring_plan(
    session: Session, run_id: UUID, *, plan_id: UUID, revision: int, sha256: str, actor_id: int
) -> RunPlanApproval:
    """Record the owner's decision and enqueue its handoff in the same transaction."""
    run = session.get(Run, run_id, with_for_update=True, populate_existing=True)
    if (
        run is None
        or run.workflow_version != "repository-v0.1"
        or run.status != "NEEDS_HUMAN_REVIEW"
        or run.requested_by != actor_id
    ):
        raise PlanRevisionConflict("This run cannot accept a plan approval.")
    plan = load_refactoring_plan(session, run_id)
    if plan is None or (plan.id, plan.revision, plan.sha256) != (plan_id, revision, sha256):
        raise PlanRevisionConflict("Plan revision changed. Reload it before approving.")

    existing = session.get(RunPlanApproval, run_id)
    if existing is not None:
        if (existing.revision, existing.sha256, existing.approved_by) == (
            revision,
            sha256,
            actor_id,
        ):
            return existing
        raise PlanRevisionConflict("This run already has an approved plan.")

    if plan_feedback_status(session, plan) in {"PENDING", "CLAIMED"}:
        raise PlanRevisionConflict("Wait for the updated plan before approving.")
    if _require_reviewable_plan(run, plan).scope_errors:
        raise PlanRevisionConflict("Resolve the file and validation scope issues before approving.")
    approval = RunPlanApproval(
        run_id=run_id, revision=revision, sha256=sha256, approved_by=actor_id
    )
    session.add(approval)
    session.add(
        RunCommand(
            run_id=run_id,
            kind=APPLY_PLAN_APPROVAL,
            dedupe_key=approval_command_key(run_id, revision),
        )
    )
    session.add(
        RunEvent(
            run_id=run_id,
            event_type="PLAN_APPROVED",
            payload={
                "plan_id": str(plan_id),
                "revision": revision,
                "sha256": sha256,
                "approved_by": actor_id,
            },
        )
    )
    session.flush()
    return approval
