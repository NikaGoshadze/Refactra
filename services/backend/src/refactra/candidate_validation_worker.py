"""Validate a persisted candidate and prepare the exact commit for review."""

import json
from base64 import b64encode
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select

from refactra.candidate_commit import prepare_candidate_commit
from refactra.candidate_patch import (
    MAX_EDIT_FILE_BYTES,
    CandidatePatch,
    CandidatePatchError,
    apply_candidate_patch,
)
from refactra.candidate_results import load_validated_candidate, save_validated_candidate
from refactra.diagnostics import diagnostic_id, workflow_log
from refactra.github_app import GitHubAppError
from refactra.models import Run, RunCandidateResult, RunEvent, RunGeneratedCandidate
from refactra.refactoring_plan import PlanContent, approved_file_operation, load_approved_plan
from refactra.repository_analysis_worker import _claimed_command
from refactra.repository_candidate import VALIDATE_CANDIDATE, _attempt_id, validation_command_key
from refactra.repository_validation import blocks_candidate, validate_candidate
from refactra.repository_workspace import prepare_approved_workspace


def _file_operation_candidate(session, run_id, plan, content, operation, artifacts):
    existing = session.scalar(
        select(RunCandidateResult)
        .where(RunCandidateResult.run_id == run_id)
        .order_by(RunCandidateResult.revision.desc())
        .limit(1)
    )
    if existing is not None:
        return CandidatePatch.model_validate(json.loads(existing.document)["candidate"])
    edit = content.changes[0]
    replacement = None
    if operation == "asset_replace":
        asset = artifacts / f"{run_id}.asset"
        if asset.is_symlink() or not asset.is_file() or asset.stat().st_nlink != 1:
            raise CandidatePatchError("Stage a regular replacement asset in the worker directory.")
        with asset.open("rb") as stream:
            data = stream.read(MAX_EDIT_FILE_BYTES + 1)
        if not data or len(data) > MAX_EDIT_FILE_BYTES:
            raise CandidatePatchError("Replacement assets must fit within 64 KiB.")
        replacement = b64encode(data).decode("ascii")
    return CandidatePatch(
        base_sha=content.repository.base_sha,
        plan_sha256=plan.sha256,
        edits=[{"path": edit.path, "action": edit.action, "content": replacement}],
    )


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
            if key != validation_command_key(run_id, plan.revision):
                raise CandidatePatchError("A matching generated candidate is required.")
            content = PlanContent.model_validate_json(plan.document)
            operation = approved_file_operation(content.changes, content.validation_checks)
            if operation:
                candidate = _file_operation_candidate(
                    session, run_id, plan, content, operation, artifacts
                )
            else:
                generated = session.get(RunGeneratedCandidate, run_id)
                if generated is None or generated.reservation_id != _attempt_id(
                    run_id, plan.sha256
                ):
                    raise CandidatePatchError("A matching generated candidate is required.")
                candidate = CandidatePatch.model_validate_json(generated.document)
            result = load_validated_candidate(session, run_id, candidate)
        if result is None:
            with prepare_approved_workspace(session_factory, run_id) as workspace:
                diff = apply_candidate_patch(workspace, candidate)
                checks = validate_candidate(workspace)
            workflow_log(
                "candidate_checks_finished",
                run_id=run_id,
                command_id=command_id,
                checks=[
                    {
                        "check_id": diagnostic_id(check.check_id),
                        "status": check.status,
                        "exit_code": check.exit_code,
                        "duration_ms": check.duration_ms,
                    }
                    for check in checks
                ],
            )
            if blocks_candidate(checks):
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
    except (GitHubAppError, HTTPException, KeyError, OSError, ValueError) as error:
        workflow_log(
            "candidate_validation_stopped",
            warning=True,
            run_id=run_id,
            command_id=command_id,
            code=code,
            error_type=type(error).__name__,
        )

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
