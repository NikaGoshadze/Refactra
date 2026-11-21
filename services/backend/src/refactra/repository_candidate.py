"""Generate a local candidate patch for a verified repository plan."""

import argparse
import os
from pathlib import Path
from uuid import UUID, uuid5

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from refactra.analysis_request import MAX_REQUEST_OUTPUT, BudgetExceeded
from refactra.candidate_patch import CandidatePatch, CandidatePatchError, apply_candidate_patch
from refactra.candidate_request import CANDIDATE_INSTRUCTIONS, prepare_candidate_context
from refactra.config import Settings
from refactra.db import make_engine, sessions
from refactra.github_app import GitHubAppError
from refactra.models import ModelReservation
from refactra.openai_analysis import (
    AnalysisProviderError,
    OpenAIAnalysisProvider,
    execute_model_request,
)
from refactra.repository_workspace import WorkspacePreparationError, prepare_approved_workspace


def generate_candidate(session_factory, run_id: UUID, provider) -> CandidatePatch:
    with prepare_approved_workspace(session_factory, run_id) as workspace:
        attempt_id = uuid5(run_id, f"repository:candidate:v1:{workspace.sha256}")
        with session_factory() as session:
            if session.get(ModelReservation, attempt_id) is not None:
                raise CandidatePatchError("A candidate attempt is already recorded for this plan.")
            context = prepare_candidate_context(session, run_id, workspace)
        result = execute_model_request(
            session_factory,
            attempt_id,
            provider,
            context,
            instructions=CANDIDATE_INSTRUCTIONS,
            max_output_tokens=MAX_REQUEST_OUTPUT,
        )
        try:
            patch = CandidatePatch.model_validate_json(result.text)
        except ValidationError:
            raise CandidatePatchError(
                "The generated candidate did not match the patch format."
            ) from None
        apply_candidate_patch(workspace, patch)
        return patch


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
