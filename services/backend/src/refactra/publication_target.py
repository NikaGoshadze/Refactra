"""Persist the verified Git commit and PR destination for human review."""

import json
import re
from hashlib import sha256
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from refactra.models import Run, RunCandidateResult, RunCommand, RunEvent, RunPublicationTarget

PUBLICATION_APPROVED = "PUBLICATION_APPROVED"
PUBLICATION_CHECK_WAIVED = "PUBLICATION_CHECK_WAIVED"
MAX_WAIVER_REASON = 500
PUBLICATION_BRANCH_PUSHED = "PUBLICATION_BRANCH_PUSHED"
PUBLICATION_PR_OPENED = "PUBLICATION_PR_OPENED"
PUBLISH_CANDIDATE = "PUBLISH_CANDIDATE"


def publication_command_key(run_id: UUID, target_sha256: str) -> str:
    return f"repository:publication:{run_id}:{target_sha256}"


class PublicationTargetError(ValueError):
    """The prepared commit cannot be bound to this saved result."""


class PublicationApproval(BaseModel):
    result_id: UUID
    result_revision: int
    target_sha256: str
    approved_by: int


class PublicationWaiver(BaseModel):
    """The owner's decision to publish without one required check that could not run."""

    result_id: UUID
    result_sha256: str
    check_index: int
    check_id: str
    reason: str
    waived_by: int


class PublicationBranchRead(BaseModel):
    status: Literal["branch_pushed"] = "branch_pushed"


class PublicationDraftRead(BaseModel):
    status: Literal["draft_opened"] = "draft_opened"
    number: int
    url: str


def load_publication_progress(
    session: Session, run_id: UUID, *, target_sha256: str, commit_sha: str, head_branch: str
) -> PublicationBranchRead | PublicationDraftRead | None:
    event = session.execute(
        select(RunEvent.event_type, RunEvent.payload)
        .where(
            RunEvent.run_id == run_id,
            RunEvent.event_type.in_([PUBLICATION_BRANCH_PUSHED, PUBLICATION_PR_OPENED]),
            RunEvent.payload["target_sha256"].astext == target_sha256,
            RunEvent.payload["commit_sha"].astext == commit_sha,
            RunEvent.payload["head_branch"].astext == head_branch,
        )
        .order_by(RunEvent.id.desc())
        .limit(1)
    ).one_or_none()
    if event is None:
        return None
    kind, receipt = event
    if kind == PUBLICATION_BRANCH_PUSHED:
        return PublicationBranchRead()
    number, repository = receipt.get("pull_request_number"), receipt.get("repository")
    if (
        type(number) is int
        and number > 0
        and isinstance(repository, str)
        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        and not any(part in {".", ".."} for part in repository.split("/"))
    ):
        url = f"https://github.com/{repository}/pull/{number}"
        if receipt.get("pull_request_url") == url:
            return PublicationDraftRead(number=number, url=url)
    return None


def load_publication_approval(session: Session, run_id: UUID) -> PublicationApproval | None:
    payload = session.scalar(
        select(RunEvent.payload)
        .where(RunEvent.run_id == run_id, RunEvent.event_type == PUBLICATION_APPROVED)
        .order_by(RunEvent.id)
        .limit(1)
    )
    return PublicationApproval.model_validate(payload) if payload is not None else None


def load_publication_waivers(
    session: Session, run_id: UUID, result: RunCandidateResult
) -> list[PublicationWaiver]:
    """Waivers bound to this exact result revision and digest; others grant nothing."""
    payloads = session.scalars(
        select(RunEvent.payload)
        .where(
            RunEvent.run_id == run_id,
            RunEvent.event_type == PUBLICATION_CHECK_WAIVED,
            RunEvent.payload["result_id"].astext == str(result.id),
            RunEvent.payload["result_sha256"].astext == result.sha256,
        )
        .order_by(RunEvent.id)
    )
    return [PublicationWaiver.model_validate(payload) for payload in payloads]


def _latest_result(session: Session, run_id: UUID) -> RunCandidateResult | None:
    return session.scalar(
        select(RunCandidateResult)
        .where(RunCandidateResult.run_id == run_id)
        .order_by(RunCandidateResult.revision.desc())
        .limit(1)
    )


def _lock_reviewable_run(session: Session, run_id: UUID, actor_id: int) -> Run:
    run = session.get(Run, run_id, with_for_update=True)
    if run is None or run.requested_by != actor_id or run.status != "NEEDS_HUMAN_REVIEW":
        raise PublicationTargetError("This run cannot accept a publication approval.")
    return run


def unwaived_required_checks(result: RunCandidateResult, waivers: list[PublicationWaiver]):
    """Indexes of required checks that neither passed nor have a matching waiver."""
    waived = {(waiver.check_index, waiver.check_id) for waiver in waivers}
    return [
        index
        for index, check in enumerate(json.loads(result.document)["validation"])
        if check["required"]
        and check["status"] != "PASSED"
        and (index, check["check_id"]) not in waived
    ]


def waive_publication_check(
    session: Session,
    run_id: UUID,
    *,
    result_id: UUID,
    result_sha256: str,
    check_index: int,
    reason: str,
    actor_id: int,
) -> PublicationWaiver:
    """Record the owner's waiver for one required check that could not run."""
    _lock_reviewable_run(session, run_id, actor_id)
    if load_publication_approval(session, run_id) is not None:
        raise PublicationTargetError("Publication is already approved.")
    result = _latest_result(session, run_id)
    if result is None or (result.id, result.sha256) != (result_id, result_sha256):
        raise PublicationTargetError("The saved result changed. Refresh it before waiving a check.")
    checks = json.loads(result.document)["validation"]
    if not 0 <= check_index < len(checks):
        raise PublicationTargetError("The check is not part of this result.")
    check = checks[check_index]
    if not check["required"] or check["status"] != "UNAVAILABLE":
        raise PublicationTargetError("Only required checks that could not run can be waived.")
    reason = reason.strip()
    if not reason or len(reason) > MAX_WAIVER_REASON:
        raise PublicationTargetError("Give a reason of at most 500 characters.")
    waiver = PublicationWaiver(
        result_id=result.id,
        result_sha256=result.sha256,
        check_index=check_index,
        check_id=check["check_id"],
        reason=reason,
        waived_by=actor_id,
    )
    for existing in load_publication_waivers(session, run_id, result):
        if existing.check_index == check_index:
            if existing == waiver:
                return existing
            raise PublicationTargetError("This check is already waived for this result.")
    session.add(
        RunEvent(
            run_id=run_id,
            event_type=PUBLICATION_CHECK_WAIVED,
            payload=waiver.model_dump(mode="json"),
        )
    )
    return waiver


def approve_publication_target(
    session: Session, run_id: UUID, *, result_id: UUID, target_sha256: str, actor_id: int
) -> PublicationApproval:
    _lock_reviewable_run(session, run_id, actor_id)
    existing = load_publication_approval(session, run_id)
    if existing is not None:
        if (existing.result_id, existing.target_sha256) == (result_id, target_sha256):
            return existing
        raise PublicationTargetError("Publication is already approved for another target.")
    result = _latest_result(session, run_id)
    target = (
        session.get(RunPublicationTarget, result_id) if result and result.id == result_id else None
    )
    if target is None or target.sha256 != target_sha256:
        raise PublicationTargetError("The publication target changed. Refresh the saved result.")
    if unwaived_required_checks(result, load_publication_waivers(session, run_id, result)):
        raise PublicationTargetError(
            "Waive each required check that could not run before approving publication."
        )
    approval = PublicationApproval(
        result_id=result_id,
        result_revision=result.revision,
        target_sha256=target_sha256,
        approved_by=actor_id,
    )
    session.add(
        RunEvent(
            run_id=run_id, event_type=PUBLICATION_APPROVED, payload=approval.model_dump(mode="json")
        )
    )
    session.add(
        RunCommand(
            run_id=run_id,
            kind=PUBLISH_CANDIDATE,
            dedupe_key=publication_command_key(run_id, target_sha256),
        )
    )
    return approval


def save_publication_target(session: Session, receipt: dict) -> str:
    """The operator supplies a receipt recomputed from the pinned Git objects."""
    result_id = UUID(receipt["result_id"])
    result = session.get(RunCandidateResult, result_id, with_for_update=True)
    if result is None or (str(result.run_id), result.revision, result.sha256) != (
        receipt["run_id"],
        receipt["result_revision"],
        receipt["result_sha256"],
    ):
        raise PublicationTargetError("The prepared commit does not match the saved result.")

    document = json.dumps(
        {"schema_version": "1", **receipt}, sort_keys=True, separators=(",", ":")
    ).encode()
    digest = sha256(document).hexdigest()
    existing = session.get(RunPublicationTarget, result_id)
    if existing is not None:
        if existing.sha256 != digest:
            raise PublicationTargetError("This result already has a different publication target.")
        return existing.sha256

    session.add(RunPublicationTarget(result_id=result_id, sha256=digest, document=document))
    session.add(
        RunEvent(
            run_id=result.run_id,
            event_type="PUBLICATION_TARGET_SAVED",
            payload={
                "result_id": str(result_id),
                "target_sha256": digest,
                "commit_sha": receipt["commit_sha"],
            },
        )
    )
    return digest
