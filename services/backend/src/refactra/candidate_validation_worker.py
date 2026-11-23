"""Validate a persisted candidate and prepare the exact commit for review."""

import json
import re
from base64 import b64encode
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select

from refactra.candidate_commit import prepare_candidate_commit
from refactra.candidate_patch import (
    MAX_EDIT_FILE_BYTES,
    CandidatePatch,
    CandidatePatchError,
    apply_candidate_patch,
)
from refactra.candidate_request import MAX_REPAIR_DETAIL
from refactra.candidate_results import load_validated_candidate, save_validated_candidate
from refactra.diagnostics import diagnostic_id, workflow_log
from refactra.github_app import GitHubAppError
from refactra.models import Run, RunCandidateResult, RunCommand, RunEvent, RunGeneratedCandidate
from refactra.refactoring_plan import PlanContent, approved_file_operation, load_approved_plan
from refactra.repository_analysis_worker import (
    GENERATE_CANDIDATE,
    _claimed_command,
    candidate_command_key,
)
from refactra.repository_candidate import (
    VALIDATE_CANDIDATE,
    VALIDATION_REPAIR_EVENT,
    VALIDATION_REPAIR_SUFFIX,
    candidate_attempt_ids,
    candidate_validation_key,
    validation_command_key,
)
from refactra.repository_validation import ValidationResult, blocks_candidate, validate_candidate
from refactra.repository_workspace import prepare_approved_workspace


def _failed_check_detail(checks: list[ValidationResult]) -> str:
    """Extract short diagnostics without transmitting raw test output or values."""
    details = []
    for check in checks:
        if not check.required or check.status != "FAILED" or check.exit_code is None:
            continue
        check_id = diagnostic_id(check.check_id) or "validation"
        details.append(f"{check_id} exited with code {check.exit_code}.")
        environment = re.search(
            r"(?m)^E\s+ValueError: (?:Error: )?"
            r"([A-Z][A-Z0-9_]{0,79}) environment variable not set\.",
            check.output,
        )
        if environment:
            details.append(
                f"Startup requires {environment[1]}. Set a dummy fixture value before imports."
            )
        test = re.search(r"(?m)^(?:FAILED|ERROR) (?:[\w./-]+\.py::)(\w{1,80})", check.output)
        if test:
            details.append(f"Review {test[1]}.")
    return " ".join(dict.fromkeys(details))[:MAX_REPAIR_DETAIL]


def _queue_validation_repair(session, command, revision, reservation_id, detail):
    run_id = command.run_id
    repair_id = uuid4()
    session.add(
        RunCommand(
            id=repair_id,
            run_id=run_id,
            kind=GENERATE_CANDIDATE,
            dedupe_key=candidate_command_key(run_id, revision) + VALIDATION_REPAIR_SUFFIX,
        )
    )
    session.add(
        RunEvent(
            run_id=run_id,
            event_type=VALIDATION_REPAIR_EVENT,
            payload={
                "command_id": str(repair_id),
                "reservation_id": str(reservation_id),
                "validation_command_id": str(command.id),
                "detail": detail,
            },
        )
    )
    command.status = "DONE"
    command.completed_at = session.scalar(select(func.now()))
    command.lease_until = None


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
    detail = ""
    try:
        with session_factory() as session:
            plan = load_approved_plan(session, session.get(Run, run_id))
            content = PlanContent.model_validate_json(plan.document)
            operation = approved_file_operation(content.changes, content.validation_checks)
            generated = None
            expected_key = validation_command_key(run_id, plan.revision)
            if operation:
                candidate = _file_operation_candidate(
                    session, run_id, plan, content, operation, artifacts
                )
            else:
                generated = session.get(RunGeneratedCandidate, run_id)
                if generated is None or generated.reservation_id not in candidate_attempt_ids(
                    run_id, plan.sha256
                ):
                    raise CandidatePatchError("A matching generated candidate is required.")
                expected_key = candidate_validation_key(
                    run_id, plan.revision, plan.sha256, generated.reservation_id
                )
                candidate = CandidatePatch.model_validate_json(generated.document)
            if key != expected_key:
                raise CandidatePatchError("A matching generated candidate is required.")
            reservation_id = generated.reservation_id if generated else None
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
                        "reason": check.reason if check.status == "UNAVAILABLE" else None,
                    }
                    for check in checks
                ],
            )
            if blocks_candidate(checks):
                code = "candidate_checks_failed"
                detail = _failed_check_detail(checks)
            with session_factory.begin() as session:
                command = _claimed_command(session, command_id, worker_id)
                if command is None or command.attempts != attempt:
                    return False
                if detail and reservation_id and not key.endswith(VALIDATION_REPAIR_SUFFIX):
                    _queue_validation_repair(
                        session, command, plan.revision, reservation_id, detail
                    )
                    return True
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
                        else {"code": code, **({"detail": detail} if detail else {})}
                    ),
                },
            )
        )
        return True
