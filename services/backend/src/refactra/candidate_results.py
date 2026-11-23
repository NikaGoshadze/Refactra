"""Persist the exact candidate, diff and successful required validation evidence."""

import json
from dataclasses import asdict
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from refactra.candidate_patch import MAX_DIFF_BYTES, CandidatePatch
from refactra.diff_review import review_candidate_diff
from refactra.models import Run, RunCandidateResult, RunEvent
from refactra.refactoring_plan import PlanContent, load_approved_plan
from refactra.repository_validation import ValidationResult, blocks_candidate, candidate_checks

MAX_RESULT_BYTES = 2 * 1024 * 1024


class CandidateResultError(ValueError):
    """An operator-safe reason the validated artifact could not be saved."""


def _canonical(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _candidate_payload(candidate: CandidatePatch) -> dict:
    patch = candidate.model_dump(mode="json")
    patch["edits"].sort(key=lambda edit: edit["path"])
    return patch


def load_validated_candidate(
    session: Session, run_id: UUID, candidate: CandidatePatch
) -> RunCandidateResult | None:
    digest = sha256(_canonical(_candidate_payload(candidate))).hexdigest()
    return session.scalar(
        select(RunCandidateResult).where(
            RunCandidateResult.run_id == run_id, RunCandidateResult.candidate_sha256 == digest
        )
    )


def save_validated_candidate(
    session: Session,
    run_id: UUID,
    candidate: CandidatePatch,
    diff: bytes,
    checks: list[ValidationResult],
) -> RunCandidateResult:
    """Trusted caller applied and validated the candidate in the approved workspace."""
    run = session.get(Run, run_id, with_for_update=True, populate_existing=True)
    if run is None:
        raise CandidateResultError("Repository run could not be found.")
    plan = load_approved_plan(session, run)
    content = PlanContent.model_validate_json(plan.document)
    if (candidate.base_sha, candidate.plan_sha256) != (run.base_sha, plan.sha256):
        raise CandidateResultError("The candidate does not match the approved commit and plan.")
    expected = {item.path: item.action for item in content.changes}
    if (
        len(candidate.edits) != len(expected)
        or {item.path: item.action for item in candidate.edits} != expected
    ):
        raise CandidateResultError("The candidate does not match the approved file scope.")
    if [(check.check_id, check.paths, check.required) for check in checks] != [
        (check.check_id, check.paths, check.required)
        for check in candidate_checks(content.changes, content.validation_checks)
    ] or blocks_candidate(checks):
        raise CandidateResultError(
            "The result needs a passing built-in check and valid evidence for required checks."
        )
    if not diff or len(diff) > MAX_DIFF_BYTES:
        raise CandidateResultError("The validation result needs a bounded review diff.")

    patch = _candidate_payload(candidate)
    candidate_hash = sha256(_canonical(patch)).hexdigest()
    diff_hash = sha256(diff).hexdigest()
    existing = session.scalar(
        select(RunCandidateResult).where(
            RunCandidateResult.run_id == run_id,
            RunCandidateResult.candidate_sha256 == candidate_hash,
        )
    )
    if existing is not None:
        if json.loads(existing.document)["diff_sha256"] != diff_hash:
            raise CandidateResultError("This candidate was already saved with a different diff.")
        return existing

    previous = session.scalar(
        select(RunCandidateResult.revision)
        .where(RunCandidateResult.run_id == run_id)
        .order_by(RunCandidateResult.revision.desc())
        .limit(1)
    )
    revision = (previous or 0) + 1
    review_diff = diff.decode("utf-8")
    document = _canonical(
        {
            "schema_version": "1",
            "run_id": str(run_id),
            "revision": revision,
            "repository": content.repository.model_dump(),
            "approved_plan": {"id": str(plan.id), "revision": plan.revision, "sha256": plan.sha256},
            "candidate": patch,
            "candidate_sha256": candidate_hash,
            "diff": review_diff,
            "diff_sha256": diff_hash,
            "review": review_candidate_diff(content, review_diff, checks).model_dump(),
            "validation": [asdict(check) for check in checks],
        }
    )
    if len(document) > MAX_RESULT_BYTES:
        raise CandidateResultError("The candidate result exceeds the 2 MiB storage limit.")
    result = RunCandidateResult(
        run_id=run_id,
        revision=revision,
        candidate_sha256=candidate_hash,
        sha256=sha256(document).hexdigest(),
        document=document,
    )
    session.add(result)
    session.flush()
    session.add(
        RunEvent(
            run_id=run_id,
            event_type="CANDIDATE_RESULT_SAVED",
            payload={"result_id": str(result.id), "revision": revision, "sha256": result.sha256},
        )
    )
    return result
