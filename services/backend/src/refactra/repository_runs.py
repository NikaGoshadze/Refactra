"""Internal storage for a run pinned to an already-authorized repository revision.

Call only after session/tester/repository and current GitHub permission checks,
using the installation and revision verified by that flow. This is not an
authorization boundary and is not exposed through the API yet.
"""

import re

from sqlalchemy.orm import Session

from refactra.auth_sessions import GitHubIdentity
from refactra.models import Run, RunEvent
from refactra.repository_revision import RepositoryRevision, validate_branch


def create_repository_run(
    session: Session,
    *,
    identity: GitHubIdentity,
    installation_id: int,
    revision: RepositoryRevision,
    task_text: str,
) -> Run:
    """Stage the run and event atomically; the caller owns commit/rollback.

    No command is enqueued: real orchestration is not available yet. Later
    source retrieval must use the stored base_sha without resolving the branch again.
    """
    if any(
        type(value) is not int or value <= 0
        for value in (identity.user_id, installation_id, revision.repository_id)
    ):
        raise ValueError("Positive GitHub user, installation and repository IDs are required.")
    validate_branch(revision.base_branch)
    if not isinstance(revision.commit_sha, str) or not re.fullmatch(
        r"[0-9a-f]{40}", revision.commit_sha
    ):
        raise ValueError("A full resolved commit SHA is required.")
    if not isinstance(task_text, str) or not 1 <= len(task_text) <= 4000 or not task_text.strip():
        raise ValueError("A nonblank task of at most 4000 characters is required.")
    run = Run(
        requested_by=identity.user_id,
        github_installation_id=installation_id,
        github_repository_id=revision.repository_id,
        base_branch=revision.base_branch,
        base_sha=revision.commit_sha,
        task_text=task_text.strip(),
        status="CREATED",
        workflow_version="repository-v0.1",
    )
    session.add(run)
    session.flush()
    session.add(
        RunEvent(
            run_id=run.id,
            event_type="RUN_CREATED",
            payload={
                "mode": "repository",
                "base_branch": run.base_branch,
                "base_sha": run.base_sha,
            },
        )
    )
    session.flush()
    return run
