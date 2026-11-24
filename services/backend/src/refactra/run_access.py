"""Recheck a queued run owner's repository access before worker side effects."""

import os
from pathlib import Path

from refactra.auth_sessions import GitHubIdentity
from refactra.github_app import GitHubAppClient, InstallationToken
from refactra.github_repository_access import require_github_write_access
from refactra.models import Run
from refactra.repository_access import RepositoryAccessPolicy


class RunAccessDenied(ValueError):
    """The local policy no longer allows this run owner, repository or installation."""


def authorize_run_repository(run: Run) -> tuple[GitHubAppClient, InstallationToken]:
    """Recheck the pinned run owner's permission and return the App and read token.

    Local denial raises RunAccessDenied and current GitHub denial raises a 403
    HTTPException. GitHubAppError means GitHub could not verify access right now;
    KeyError, OSError and other ValueErrors mean the configuration is unusable.
    """
    policy = RepositoryAccessPolicy.from_env()
    installation_id = int(os.environ["REFACTRA_GITHUB_INSTALLATION_ID"])
    if (
        run.github_repository_id != policy.repository_id
        or run.requested_by not in policy.allowed_user_ids
        or installation_id != run.github_installation_id
        or installation_id <= 0
    ):
        raise RunAccessDenied("Repository access is not allowed.")
    app = GitHubAppClient(
        os.environ["REFACTRA_GITHUB_CLIENT_ID"],
        Path(os.environ["REFACTRA_GITHUB_PRIVATE_KEY_PATH"]).read_bytes(),
    )
    token = require_github_write_access(
        app, installation_id, run.github_repository_id, GitHubIdentity(run.requested_by, "operator")
    )
    return app, token
