"""Push the approved candidate to its designated GitHub branch."""

import argparse
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from refactra.candidate_commit import CandidateCommitError, _git
from refactra.config import Settings
from refactra.db import make_engine, sessions
from refactra.github_app import GitHubAppError
from refactra.models import Run, RunCandidateResult, RunEvent, RunPublicationTarget
from refactra.publication_target import (
    PUBLICATION_BRANCH_PUSHED,
    PublicationTargetError,
    load_publication_approval,
)
from refactra.repository_plan_worker import authorize_run_repository
from refactra.repository_revision import resolve_revision


def load_approved_target(session, run_id: UUID):
    run = session.get(Run, run_id)
    approval = load_publication_approval(session, run_id)
    if (
        run is None
        or approval is None
        or run.requested_by != approval.approved_by
        or run.status != "NEEDS_HUMAN_REVIEW"
    ):
        raise PublicationTargetError("The run owner must approve publication first.")
    result = session.get(RunCandidateResult, approval.result_id)
    target = session.get(RunPublicationTarget, approval.result_id)
    if (
        result is None
        or target is None
        or result.run_id != run_id
        or result.revision != approval.result_revision
        or target.sha256 != approval.target_sha256
    ):
        raise PublicationTargetError("The approved result and target no longer match.")
    saved = json.loads(target.document)
    head = f"refactra/{run_id}/r{result.revision}"
    expected_destination = {
        "repository_id": run.github_repository_id,
        "base_sha": run.base_sha,
        "base_branch": run.base_branch,
        "head_branch": head,
    }
    commit = saved["commit_sha"]
    if (
        head == run.base_branch
        or saved["destination"] != expected_destination
        or saved["result_id"] != str(result.id)
        or saved["result_sha256"] != result.sha256
        or not re.fullmatch(r"[0-9a-f]{40}", commit)
    ):
        raise PublicationTargetError("The prepared commit does not match the approved destination.")
    return run, target.sha256, commit, head, result


def push_approved_branch(session_factory, run_id: UUID, artifact: Path) -> dict:
    with session_factory() as session:
        run, target_sha, commit, head, _ = load_approved_target(session, run_id)
    app, reader = authorize_run_repository(run)
    source = (artifact / "repository.git").resolve(strict=True)
    with TemporaryDirectory(prefix="refactra-push-") as directory:
        repository = Path(directory) / "push.git"
        _git(repository, "init", "--bare", "--template=", "--object-format=sha1")
        _git(
            repository,
            "fetch",
            "--depth=2",
            "--no-tags",
            "--no-recurse-submodules",
            "--no-write-fetch-head",
            "--",
            str(source),
            commit,
        )
        parents = _git(repository, "show", "-s", "--format=%P", commit).strip().decode()
        if parents != run.base_sha:
            raise PublicationTargetError("The local commit does not have the approved base.")

        pinned = resolve_revision(reader, base_branch=run.base_branch, transport=app.transport)
        if pinned.commit_sha != run.base_sha:
            raise PublicationTargetError("The GitHub base branch moved after the run was pinned.")
        writer = app.mint_push_token(run.github_installation_id, run.github_repository_id)
        remote = f"https://github.com/{pinned.full_name}.git"
        ref = f"refs/heads/{head}"
        _git(
            repository,
            "push",
            "--no-verify",
            f"--force-with-lease={ref}:",
            "--",
            remote,
            f"{commit}:{ref}",
            https_auth=(remote, writer.value),
        )
    confirmed = resolve_revision(reader, base_branch=head, transport=app.transport)
    if confirmed.commit_sha != commit or confirmed.full_name != pinned.full_name:
        raise PublicationTargetError("GitHub did not confirm the approved commit on its branch.")

    receipt = {
        "run_id": str(run_id),
        "target_sha256": target_sha,
        "commit_sha": commit,
        "repository": pinned.full_name,
        "head_branch": head,
    }
    with session_factory.begin() as session:
        session.get(Run, run_id, with_for_update=True)
        recorded = session.scalar(
            select(RunEvent.id)
            .where(RunEvent.run_id == run_id, RunEvent.event_type == PUBLICATION_BRANCH_PUSHED)
            .limit(1)
        )
        if recorded is None:
            session.add(
                RunEvent(run_id=run_id, event_type=PUBLICATION_BRANCH_PUSHED, payload=receipt)
            )
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", type=UUID)
    parser.add_argument(
        "--artifact", type=Path, required=True, help="Prepared candidate directory."
    )
    args = parser.parse_args()
    engine = make_engine(Settings.from_env().database_url)
    try:
        print(json.dumps(push_approved_branch(sessions(engine), args.run_id, args.artifact)))
    except (PublicationTargetError, CandidateCommitError, GitHubAppError) as error:
        raise SystemExit(str(error)) from None
    except (HTTPException, OSError, SQLAlchemyError, ValueError, KeyError, TypeError):
        raise SystemExit(
            "Branch publication stopped. Check repository access and the artifact."
        ) from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
