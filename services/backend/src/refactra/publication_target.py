"""Persist the verified Git commit and PR destination for human review."""

import json
from hashlib import sha256
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from refactra.models import Run, RunCandidateResult, RunEvent, RunPublicationTarget

PUBLICATION_APPROVED = "PUBLICATION_APPROVED"


class PublicationTargetError(ValueError):
    """The prepared commit cannot be bound to this saved result."""


class PublicationApproval(BaseModel):
    result_id: UUID
    result_revision: int
    target_sha256: str
    approved_by: int


def load_publication_approval(session: Session, run_id: UUID) -> PublicationApproval | None:
    payload = session.scalar(
        select(RunEvent.payload)
        .where(RunEvent.run_id == run_id, RunEvent.event_type == PUBLICATION_APPROVED)
        .order_by(RunEvent.id)
        .limit(1)
    )
    return PublicationApproval.model_validate(payload) if payload is not None else None


def approve_publication_target(
    session: Session, run_id: UUID, *, result_id: UUID, target_sha256: str, actor_id: int
) -> PublicationApproval:
    run = session.get(Run, run_id, with_for_update=True)
    if run is None or run.requested_by != actor_id or run.status != "NEEDS_HUMAN_REVIEW":
        raise PublicationTargetError("This run cannot accept a publication approval.")
    existing = load_publication_approval(session, run_id)
    if existing is not None:
        if (existing.result_id, existing.target_sha256) == (result_id, target_sha256):
            return existing
        raise PublicationTargetError("Publication is already approved for another target.")
    result = session.scalar(
        select(RunCandidateResult)
        .where(RunCandidateResult.run_id == run_id)
        .order_by(RunCandidateResult.revision.desc())
        .limit(1)
    )
    target = (
        session.get(RunPublicationTarget, result_id) if result and result.id == result_id else None
    )
    if target is None or target.sha256 != target_sha256:
        raise PublicationTargetError("The publication target changed. Refresh the saved result.")
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
