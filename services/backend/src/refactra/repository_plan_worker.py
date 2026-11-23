"""Generate the initial plan for one explicitly selected, analyzed repository run."""

import argparse
import json
import os
from pathlib import Path
from uuid import UUID, uuid5

import httpx
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from refactra.analysis_request import BudgetExceeded
from refactra.auth_sessions import GitHubIdentity
from refactra.config import Settings
from refactra.db import make_engine, sessions
from refactra.diagnostics import workflow_log
from refactra.github_app import GitHubAppClient, GitHubAppError, InstallationToken
from refactra.github_repository_access import require_github_write_access
from refactra.models import ModelReservation, Run, RunEvent
from refactra.openai_analysis import (
    AnalysisProviderError,
    OpenAIAnalysisProvider,
    execute_read_only_analysis,
)
from refactra.planning_request import missing_delete_proposal
from refactra.refactoring_plan import (
    PlanContent,
    PlanProposal,
    evaluate_plan_readiness,
    load_plan_feedback,
    load_refactoring_plan,
    save_refactoring_plan,
)
from refactra.repository_access import RepositoryAccessPolicy
from refactra.repository_analysis_worker import (
    GENERATE_PLAN,
    WORKFLOW,
    _claimed_command,
    initial_plan_command_key,
)


class PlanGenerationError(ValueError):
    """Source-free stop reason suitable for operator output."""


def authorize_run_repository(run: Run) -> tuple[GitHubAppClient, InstallationToken]:
    """Recheck the pinned run owner's permission and return the App and read token."""
    policy = RepositoryAccessPolicy.from_env()
    installation_id = int(os.environ["REFACTRA_GITHUB_INSTALLATION_ID"])
    if (
        run.github_repository_id != policy.repository_id
        or run.requested_by not in policy.allowed_user_ids
        or installation_id != run.github_installation_id
        or installation_id <= 0
    ):
        raise ValueError("Repository access is not allowed.")
    app = GitHubAppClient(
        os.environ["REFACTRA_GITHUB_CLIENT_ID"],
        Path(os.environ["REFACTRA_GITHUB_PRIVATE_KEY_PATH"]).read_bytes(),
    )
    token = require_github_write_access(
        app, installation_id, run.github_repository_id, GitHubIdentity(run.requested_by, "operator")
    )
    return app, token


def authorize_plan(run: Run) -> tuple[InstallationToken, httpx.BaseTransport | None]:
    app, token = authorize_run_repository(run)
    return token, app.transport


def generate_initial_plan(
    session_factory,
    run_id: UUID,
    provider,
    *,
    check_admission=None,
    feedback_id: UUID | None = None,
):
    """Recheck repository access and resolve the plan before any model admission."""
    legacy_id = uuid5(run_id, "repository:initial-plan")
    attempt_id = feedback_id or uuid5(run_id, "repository:initial-plan:structured-v1")
    with session_factory() as session:
        run = session.get(Run, run_id)
        if run is None or run.workflow_version != WORKFLOW or run.status != "NEEDS_HUMAN_REVIEW":
            raise PlanGenerationError("A repository run with completed analysis is required.")
    authorize_plan(run)
    with session_factory.begin() as session:
        feedback = load_plan_feedback(session, run_id, feedback_id) if feedback_id else None
        if feedback_id and feedback is None:
            raise PlanGenerationError("Saved plan feedback is required.")
        expected_revision = feedback["expected_revision"] if feedback else 0
        existing = load_refactoring_plan(session, run_id)
        if existing is not None and existing.revision > expected_revision:
            return existing
        if feedback_id and (existing is None or existing.revision != expected_revision):
            raise PlanGenerationError("The plan changed before feedback could be applied.")
        if not feedback_id:
            proposal = missing_delete_proposal(session, run_id, run.task_text)
            if proposal is not None:
                if check_admission is not None:
                    check_admission(session)
                return save_refactoring_plan(
                    session, run_id, proposal.model_dump(), expected_revision=expected_revision
                )
        if session.get(ModelReservation, attempt_id) is not None:
            raise PlanGenerationError("A plan attempt is already recorded. It cannot be resent.")
        legacy = session.get(ModelReservation, legacy_id)
        if not feedback_id and legacy is not None and legacy.settled_at is None:
            raise PlanGenerationError("An earlier plan attempt has an unknown outcome.")

    plan = None
    schema_error = None

    def save_response(session, result):
        nonlocal plan, schema_error
        try:
            proposal = PlanProposal.model_validate_json(result.text)
        except ValidationError as error:
            # Only the validator's type is safe to include in operator output.
            schema_error = error.errors(include_input=False, include_context=False)[0]["type"]
        else:
            plan = save_refactoring_plan(
                session, run_id, proposal.model_dump(), expected_revision=expected_revision
            )

    execute_read_only_analysis(
        session_factory,
        run_id,
        attempt_id,
        provider,
        planning=True,
        check_admission=check_admission,
        save_response=save_response,
    )
    if schema_error:
        workflow_log("plan_response_invalid", warning=True, run_id=run_id, reason=schema_error)
        raise PlanGenerationError(
            f"The generated plan did not match the proposal schema ({schema_error})."
        )
    return plan


def process_initial_plan(session_factory, command_id: UUID, worker_id: str, provider) -> bool:
    with session_factory.begin() as session:
        command = _claimed_command(session, command_id, worker_id)
        now = session.scalar(select(func.now()))
        if command is None or command.kind != GENERATE_PLAN or command.lease_until <= now:
            return False
        run_id, attempt = command.run_id, command.attempts
        initial = command.dedupe_key == initial_plan_command_key(run_id)

    def check_admission(session):
        command = _claimed_command(session, command_id, worker_id)
        if command is None or command.attempts != attempt:
            raise PlanGenerationError("Plan claim was lost before model admission.")

    code = None
    try:
        generate_initial_plan(
            session_factory,
            run_id,
            provider,
            check_admission=check_admission,
            feedback_id=None if initial else command_id,
        )
    except BudgetExceeded:
        code = "plan_budget_exceeded"
    except (AnalysisProviderError, GitHubAppError, HTTPException, KeyError, OSError, ValueError):
        code = "plan_generation_failed"

    with session_factory.begin() as session:
        command = _claimed_command(session, command_id, worker_id)
        if command is None or command.attempts != attempt:
            return False
        command.status = "FAILED" if code else "DONE"
        command.completed_at = session.scalar(select(func.now()))
        command.lease_until = None
        if code:
            session.add(
                RunEvent(
                    run_id=run_id,
                    event_type="PLAN_GENERATION_FAILED",
                    payload={"command_id": str(command_id), "code": code},
                )
            )
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", type=UUID)
    args = parser.parse_args()
    engine = make_engine(Settings.from_env().database_url)
    try:
        provider = OpenAIAnalysisProvider(os.environ["REFACTRA_OPENAI_API_KEY"])
        plan = generate_initial_plan(sessions(engine), args.run_id, provider)
        readiness = evaluate_plan_readiness(PlanContent.model_validate_json(plan.document))
        print(
            json.dumps(
                {
                    "plan_id": str(plan.id),
                    "revision": plan.revision,
                    "sha256": plan.sha256,
                    "readiness": readiness.model_dump(),
                }
            )
        )
    except PlanGenerationError as error:
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
            "Plan generation stopped. Check run access and the model receipt."
        ) from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
