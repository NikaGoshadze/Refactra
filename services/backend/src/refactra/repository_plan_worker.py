"""Generate the initial plan for one explicitly selected, analyzed repository run."""

import argparse
import json
import os
from pathlib import Path
from uuid import UUID, uuid5

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from refactra.auth_sessions import GitHubIdentity
from refactra.config import Settings
from refactra.db import make_engine, sessions
from refactra.github_app import GitHubAppClient, GitHubAppError
from refactra.github_repository_access import require_github_write_access
from refactra.models import ModelReservation, Run
from refactra.openai_analysis import (
    AnalysisProviderError,
    OpenAIAnalysisProvider,
    execute_read_only_analysis,
)
from refactra.refactoring_plan import PlanProposal, load_refactoring_plan, save_refactoring_plan
from refactra.repository_access import RepositoryAccessPolicy
from refactra.repository_analysis_worker import WORKFLOW


class PlanGenerationError(ValueError):
    """Source-free stop reason suitable for operator output."""


def authorize_plan(run: Run) -> None:
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
    require_github_write_access(
        app, installation_id, run.github_repository_id, GitHubIdentity(run.requested_by, "operator")
    )


def generate_initial_plan(session_factory, run_id: UUID, provider):
    """The operator selects the run and authorizes one budgeted plan attempt."""
    attempt_id = uuid5(run_id, "repository:initial-plan")
    with session_factory() as session:
        run = session.get(Run, run_id)
        if run is None or run.workflow_version != WORKFLOW or run.status != "NEEDS_HUMAN_REVIEW":
            raise PlanGenerationError("A repository run with completed analysis is required.")
    authorize_plan(run)
    with session_factory() as session:
        existing = load_refactoring_plan(session, run_id)
        if existing is not None:
            return existing
        if session.get(ModelReservation, attempt_id) is not None:
            raise PlanGenerationError("A plan attempt is already recorded. It cannot be resent.")

    result = execute_read_only_analysis(
        session_factory, run_id, attempt_id, provider, planning=True
    )
    try:
        proposal = PlanProposal.model_validate_json(result.text)
    except ValueError:
        raise PlanGenerationError("The generated plan did not match the proposal schema.") from None
    with session_factory.begin() as session:
        return save_refactoring_plan(session, run_id, proposal.model_dump(), expected_revision=0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", type=UUID)
    args = parser.parse_args()
    engine = make_engine(Settings.from_env().database_url)
    try:
        provider = OpenAIAnalysisProvider(os.environ["REFACTRA_OPENAI_API_KEY"])
        plan = generate_initial_plan(sessions(engine), args.run_id, provider)
        print(
            json.dumps({"plan_id": str(plan.id), "revision": plan.revision, "sha256": plan.sha256})
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
