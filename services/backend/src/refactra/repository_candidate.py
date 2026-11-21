"""Generate a local candidate patch for a verified repository plan."""

import argparse
import os
from pathlib import Path
from uuid import UUID, uuid5

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from refactra.analysis_request import MAX_REQUEST_OUTPUT, BudgetExceeded
from refactra.candidate_patch import CandidatePatch, CandidatePatchError, apply_candidate_patch
from refactra.candidate_request import CANDIDATE_INSTRUCTIONS, prepare_candidate_context
from refactra.config import Settings
from refactra.db import make_engine, sessions
from refactra.github_app import GitHubAppError
from refactra.models import ModelReservation, Run, RunCommand, RunEvent, RunGeneratedCandidate
from refactra.openai_analysis import (
    AnalysisProviderError,
    OpenAIAnalysisProvider,
    execute_model_request,
)
from refactra.refactoring_plan import load_approved_plan
from refactra.repository_analysis_worker import (
    GENERATE_CANDIDATE,
    _claimed_command,
    candidate_command_key,
)
from refactra.repository_workspace import WorkspacePreparationError, prepare_approved_workspace

VALIDATE_CANDIDATE = "VALIDATE_CANDIDATE"


def validation_command_key(run_id: UUID, revision: int) -> str:
    return f"repository:candidate-validation:{run_id}:{revision}"


def _attempt_id(run_id: UUID, plan_sha256: str) -> UUID:
    return uuid5(run_id, f"repository:candidate:v1:{plan_sha256}")


def generate_candidate(
    session_factory, run_id: UUID, provider, *, check_admission=None
) -> CandidatePatch:
    with prepare_approved_workspace(session_factory, run_id) as workspace:
        attempt_id = _attempt_id(run_id, workspace.sha256)
        with session_factory() as session:
            saved = session.get(RunGeneratedCandidate, run_id)
            if saved is not None:
                if saved.reservation_id != attempt_id:
                    raise CandidatePatchError("The saved candidate does not match this plan.")
                return CandidatePatch.model_validate_json(saved.document)
            if session.get(ModelReservation, attempt_id) is not None:
                raise CandidatePatchError("A candidate attempt is already recorded for this plan.")
            context = prepare_candidate_context(session, run_id, workspace)
        patch = None
        rejected = None

        def save_response(session, result):
            nonlocal patch, rejected
            try:
                candidate = CandidatePatch.model_validate_json(result.text)
                apply_candidate_patch(workspace, candidate)
            except ValidationError:
                rejected = "The generated candidate did not match the patch format."
            except CandidatePatchError as error:
                rejected = str(error)
            except OSError:
                rejected = "The candidate could not be checked in the disposable workspace."
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
            max_output_tokens=MAX_REQUEST_OUTPUT,
            check_admission=check_admission,
            save_response=save_response,
        )
        if patch is None:
            raise CandidatePatchError(rejected or "The generated candidate could not be saved.")
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
        if command is None or command.attempts != attempt:
            raise CandidatePatchError("The candidate claim was lost before model admission.")

    error_code = None
    try:
        with session_factory() as session:
            run = session.get(Run, run_id)
            plan = load_approved_plan(session, run)
            if dedupe_key != candidate_command_key(run_id, plan.revision):
                raise CandidatePatchError("The candidate command targets another plan revision.")
            saved = session.get(RunGeneratedCandidate, run_id)
            if saved is not None and saved.reservation_id != _attempt_id(run_id, plan.sha256):
                raise CandidatePatchError("The saved candidate does not match this plan.")
        if saved is None:
            generate_candidate(session_factory, run_id, provider, check_admission=check_admission)
    except BudgetExceeded:
        error_code = "candidate_budget_exceeded"
    except (AnalysisProviderError, GitHubAppError, HTTPException, KeyError, OSError, ValueError):
        error_code = "candidate_generation_failed"

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
    parser.add_argument("--output", type=Path, required=True, help="New local candidate JSON file.")
    args = parser.parse_args()
    if not args.output.parent.is_dir() or args.output.exists():
        parser.error("The output must be a new file in an existing directory.")
    engine = make_engine(Settings.from_env().database_url)
    try:
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
