"""Generate a local candidate patch for a verified repository plan."""

import argparse
import os
from datetime import timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from refactra.analysis_request import MAX_REQUEST_OUTPUT, MAX_RUN_OUTPUT, BudgetExceeded
from refactra.candidate_patch import CandidatePatch, CandidatePatchError, apply_candidate_patch
from refactra.candidate_request import (
    CANDIDATE_INSTRUCTIONS,
    patch_from_contents,
    prepare_candidate_context,
)
from refactra.config import Settings
from refactra.db import make_engine, sessions
from refactra.diagnostics import workflow_log
from refactra.github_app import GitHubAppError
from refactra.models import ModelReservation, Run, RunCommand, RunEvent, RunGeneratedCandidate
from refactra.openai_analysis import (
    AnalysisProviderError,
    OpenAIAnalysisProvider,
    execute_model_request,
)
from refactra.refactoring_plan import (
    VALIDATE_CANDIDATE,
    approved_file_operation,
    load_approved_plan,
    validation_command_key,
)
from refactra.repository_analysis_worker import (
    GENERATE_CANDIDATE,
    _claimed_command,
    candidate_command_key,
)
from refactra.repository_workspace import WorkspacePreparationError, prepare_approved_workspace


class CandidateReplyRejected(CandidatePatchError):
    """A completed model response failed candidate validation."""


AttemptMode = Literal["initial", "repair", "operator"]
_ATTEMPT_PREFIXES = {
    "initial": "repository:candidate:v1",
    "repair": "repository:candidate:retry:v1",
    "operator": "repository:candidate:operator:v1",
}


def _attempt_id(run_id: UUID, plan_sha256: str, mode: AttemptMode) -> UUID:
    prefix = _ATTEMPT_PREFIXES[mode]
    return uuid5(run_id, f"{prefix}:{plan_sha256}")


def candidate_attempt_ids(run_id: UUID, plan_sha256: str) -> set[UUID]:
    return {_attempt_id(run_id, plan_sha256, mode) for mode in _ATTEMPT_PREFIXES}


def _output_limit(session, run_id: UUID, mode: AttemptMode) -> int:
    if mode == "initial":
        return MAX_REQUEST_OUTPUT
    used = session.scalar(
        select(
            func.coalesce(
                func.sum(
                    func.coalesce(
                        ModelReservation.actual_output_tokens, ModelReservation.output_tokens
                    )
                ),
                0,
            )
        ).where(ModelReservation.run_id == run_id)
    )
    if used >= MAX_RUN_OUTPUT:
        raise BudgetExceeded("run_output_tokens")
    return min(MAX_REQUEST_OUTPUT, MAX_RUN_OUTPUT - used)


def queue_candidate_retry(session, run_id: UUID) -> UUID:
    """Queue one explicit retry for an approved plan with a failed first command."""
    run = session.get(Run, run_id, with_for_update=True, populate_existing=True)
    if run is None:
        raise CandidatePatchError("The run could not be found.")
    plan = load_approved_plan(session, run)
    key = candidate_command_key(run_id, plan.revision)
    retry_key = f"{key}:operator-retry"
    existing = session.scalar(select(RunCommand).where(RunCommand.dedupe_key == retry_key))
    if existing is not None:
        return existing.id
    first = session.scalar(select(RunCommand).where(RunCommand.dedupe_key == key))
    attempts = session.scalars(
        select(ModelReservation).where(
            ModelReservation.id.in_(candidate_attempt_ids(run_id, plan.sha256))
        )
    )
    if (
        first is None
        or first.status != "FAILED"
        or session.get(RunGeneratedCandidate, run_id) is not None
        or any(receipt.settled_at is None for receipt in attempts)
    ):
        raise CandidatePatchError("This approved plan has no eligible candidate retry.")
    command = RunCommand(id=uuid4(), run_id=run_id, kind=GENERATE_CANDIDATE, dedupe_key=retry_key)
    session.add(command)
    return command.id


def generate_candidate(
    session_factory,
    run_id: UUID,
    provider,
    *,
    check_admission=None,
    mode: AttemptMode = "initial",
    repair_detail: str | None = None,
) -> CandidatePatch:
    with prepare_approved_workspace(session_factory, run_id) as workspace:
        if approved_file_operation(workspace.changes, workspace.validation_checks):
            raise CandidatePatchError("Special file operations use the validation worker.")
        attempt_id = _attempt_id(run_id, workspace.sha256, mode)
        with session_factory() as session:
            saved = session.get(RunGeneratedCandidate, run_id)
            if saved is not None:
                if saved.reservation_id not in candidate_attempt_ids(run_id, workspace.sha256):
                    raise CandidatePatchError("The saved candidate does not match this plan.")
                return CandidatePatch.model_validate_json(saved.document)
            if session.get(ModelReservation, attempt_id) is not None:
                raise CandidatePatchError("A candidate attempt is already recorded for this plan.")
            context = prepare_candidate_context(
                session, run_id, workspace, retry=mode != "initial", repair_detail=repair_detail
            )
            output_limit = _output_limit(session, run_id, mode)
        patch = None
        rejected = None
        retryable = False

        def save_response(session, result):
            nonlocal patch, rejected, retryable
            try:
                candidate = patch_from_contents(workspace, result.text)
                apply_candidate_patch(workspace, candidate)
            except ValidationError as error:
                retryable = True
                rejected = "The generated candidate did not match the patch format."
                workflow_log(
                    "candidate_patch_rejected",
                    warning=True,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    reason="invalid_patch_schema",
                    error_count=error.error_count(),
                )
            except CandidatePatchError as error:
                retryable = True
                rejected = str(error)
                workflow_log(
                    "candidate_patch_rejected",
                    warning=True,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    reason="patch_preflight_failed",
                    detail=rejected,
                )
            except OSError:
                rejected = "The candidate could not be checked in the disposable workspace."
                workflow_log(
                    "candidate_patch_rejected",
                    warning=True,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    reason="workspace_io",
                )
            else:
                patch = candidate
                session.add(
                    RunGeneratedCandidate(
                        run_id=run_id,
                        reservation_id=attempt_id,
                        document=patch.model_dump_json().encode(),
                    )
                )
                session.add(
                    RunCommand(
                        run_id=run_id,
                        kind=VALIDATE_CANDIDATE,
                        dedupe_key=validation_command_key(run_id, workspace.revision),
                    )
                )

        execute_model_request(
            session_factory,
            attempt_id,
            provider,
            context,
            instructions=CANDIDATE_INSTRUCTIONS,
            max_output_tokens=output_limit,
            check_admission=check_admission,
            save_response=save_response,
        )
        if patch is None:
            error = CandidateReplyRejected if retryable else CandidatePatchError
            raise error(rejected or "The generated candidate could not be saved.")
        return patch


def process_repository_candidate(
    session_factory, command_id: UUID, worker_id: str, provider
) -> bool:
    with session_factory.begin() as session:
        command = _claimed_command(session, command_id, worker_id)
        now = session.scalar(select(func.now()))
        if command is None or command.kind != GENERATE_CANDIDATE or command.lease_until <= now:
            return False
        run_id, attempt, dedupe_key = command.run_id, command.attempts, command.dedupe_key

    def check_admission(session):
        command = _claimed_command(session, command_id, worker_id)
        now = session.scalar(select(func.now()))
        if command is None or command.attempts != attempt or command.lease_until <= now:
            raise CandidatePatchError("The candidate claim was lost before model admission.")
        command.lease_until = now + timedelta(seconds=120)

    error_code = None
    try:
        with session_factory() as session:
            run = session.get(Run, run_id)
            plan = load_approved_plan(session, run)
            key = candidate_command_key(run_id, plan.revision)
            if dedupe_key == key:
                mode = "initial"
            elif dedupe_key == f"{key}:retry":
                mode = "repair"
            elif dedupe_key == f"{key}:operator-retry":
                mode = "operator"
            else:
                raise CandidatePatchError("The candidate command targets another plan revision.")
            saved = session.get(RunGeneratedCandidate, run_id)
            if saved is not None and saved.reservation_id not in candidate_attempt_ids(
                run_id, plan.sha256
            ):
                raise CandidatePatchError("The saved candidate does not match this plan.")
        if saved is None:
            try:
                generate_candidate(
                    session_factory, run_id, provider, check_admission=check_admission, mode=mode
                )
            except CandidateReplyRejected as error:
                if mode != "initial":
                    raise
                workflow_log("candidate_retry_started", run_id=run_id, command_id=command_id)
                generate_candidate(
                    session_factory,
                    run_id,
                    provider,
                    check_admission=check_admission,
                    mode="repair",
                    repair_detail=str(error),
                )
    except BudgetExceeded as error:
        error_code = "candidate_budget_exceeded"
        workflow_log(
            "candidate_budget_rejected",
            warning=True,
            run_id=run_id,
            command_id=command_id,
            reason=error.code,
        )
    except (
        AnalysisProviderError,
        GitHubAppError,
        HTTPException,
        KeyError,
        OSError,
        ValueError,
    ) as error:
        error_code = "candidate_generation_failed"
        workflow_log(
            "candidate_generation_failed",
            warning=True,
            run_id=run_id,
            command_id=command_id,
            error_type=type(error).__name__,
        )

    with session_factory.begin() as session:
        command = _claimed_command(session, command_id, worker_id)
        if command is None or command.attempts != attempt:
            return False
        command.status = "FAILED" if error_code else "DONE"
        command.completed_at = session.scalar(select(func.now()))
        command.lease_until = None
        session.add(
            RunEvent(
                run_id=run_id,
                event_type="CANDIDATE_GENERATION_FAILED" if error_code else "CANDIDATE_GENERATED",
                payload={
                    "command_id": str(command_id),
                    **({"code": error_code} if error_code else {}),
                },
            )
        )
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", type=UUID)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--output", type=Path, help="New local candidate JSON file.")
    operation.add_argument("--retry", action="store_true", help="Queue one approved-plan retry.")
    args = parser.parse_args()
    if args.output and (not args.output.parent.is_dir() or args.output.exists()):
        parser.error("The output must be a new file in an existing directory.")
    engine = make_engine(Settings.from_env().database_url)
    try:
        if args.retry:
            with sessions(engine).begin() as session:
                command = queue_candidate_retry(session, args.run_id)
            print(f"Candidate retry command {command}")
            return
        provider = OpenAIAnalysisProvider(os.environ["REFACTRA_OPENAI_API_KEY"])
        patch = generate_candidate(sessions(engine), args.run_id, provider)
        with args.output.open("x", encoding="utf-8", newline="\n") as output:
            output.write(patch.model_dump_json(indent=2) + "\n")
        print(f"Saved candidate with {len(patch.edits)} approved edits.")
    except (BudgetExceeded, CandidatePatchError, WorkspacePreparationError) as error:
        raise SystemExit(str(error)) from None
    except (
        AnalysisProviderError,
        GitHubAppError,
        HTTPException,
        KeyError,
        OSError,
        SQLAlchemyError,
        ValueError,
    ):
        raise SystemExit(
            "Candidate generation stopped. Check run access and the model receipt."
        ) from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
