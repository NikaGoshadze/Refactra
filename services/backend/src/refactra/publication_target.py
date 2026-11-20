"""Persist the verified Git commit and PR destination for human review."""

import json
from hashlib import sha256
from uuid import UUID

from sqlalchemy.orm import Session

from refactra.models import RunCandidateResult, RunEvent, RunPublicationTarget


class PublicationTargetError(ValueError):
    """The prepared commit cannot be bound to this saved result."""


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
