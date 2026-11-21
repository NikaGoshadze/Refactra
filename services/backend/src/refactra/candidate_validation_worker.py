"""Validate a persisted candidate and prepare the exact commit for review."""

from pathlib import Path
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select

from refactra.candidate_commit import prepare_candidate_commit
from refactra.candidate_patch import CandidatePatch, CandidatePatchError, apply_candidate_patch
from refactra.candidate_results import load_validated_candidate, save_validated_candidate
from refactra.github_app import GitHubAppError
from refactra.models import Run, RunEvent, RunGeneratedCandidate
from refactra.refactoring_plan import load_approved_plan
from refactra.repository_analysis_worker import _claimed_command
from refactra.repository_candidate import VALIDATE_CANDIDATE, _attempt_id, validation_command_key
from refactra.repository_validation import validate_candidate
from refactra.repository_workspace import prepare_approved_workspace


def process_candidate_validation(
    session_factory, command_id: UUID, worker_id: str, *, source: Path, artifacts: Path
) -> bool:
    with session_factory.begin() as session:
        command = _claimed_command(session, command_id, worker_id)
        now = session.scalar(select(func.now()))
        if command is None or command.kind != VALIDATE_CANDIDATE or command.lease_until <= now:
            return False
        run_id, attempt, key = command.run_id, command.attempts, command.dedupe_key

    code = "candidate_validation_failed"
    receipt = None
    try:
        with session_factory() as session:
            plan = load_approved_plan(session, session.get(Run, run_id))
            generated = session.get(RunGeneratedCandidate, run_id)
            if (
                generated is None
                or generated.reservation_id != _attempt_id(run_id, plan.sha256)
                or key != validation_command_key(run_id, plan.revision)
            ):
                raise CandidatePatchError("A matching generated candidate is required.")
            candidate = CandidatePatch.model_validate_json(generated.document)
            result = load_validated_candidate(session, run_id, candidate)
        if result is None:
            with prepare_approved_workspace(session_factory, run_id) as workspace:
                diff = apply_candidate_patch(workspace, candidate)
                checks = validate_candidate(workspace)
            if any(check.required and check.status != "PASSED" for check in checks):
                code = "candidate_checks_failed"
            with session_factory.begin() as session:
                command = _claimed_command(session, command_id, worker_id)
                if command is None or command.attempts != attempt:
                    return False
                result = save_validated_candidate(session, run_id, candidate, diff, checks)

        code = "candidate_commit_preparation_failed"
        receipt = prepare_candidate_commit(
            session_factory,
            run_id,
            result.revision,
            source,
            artifacts / f"{run_id}-r{result.revision}",
            save_target=True,
        )
    except (GitHubAppError, HTTPException, KeyError, OSError, ValueError):
        # Record a stage code without storing untrusted test output or settings.
        pass

    with session_factory.begin() as session:
        command = _claimed_command(session, command_id, worker_id)
        if command is None or command.attempts != attempt:
            return False
        command.status = "DONE" if receipt else "FAILED"
        command.completed_at = session.scalar(select(func.now()))
        command.lease_until = None
        session.add(
            RunEvent(
                run_id=run_id,
                event_type="CANDIDATE_REVIEW_READY" if receipt else "CANDIDATE_VALIDATION_STOPPED",
                payload={
                    "command_id": str(command_id),
                    **(
                        {"result_id": receipt["result_id"], "commit_sha": receipt["commit_sha"]}
                        if receipt
                        else {"code": code}
                    ),
                },
            )
        )
        return True
