"""One repository authorization policy for browser sessions and queued run owners."""

import os
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import HTTPException

from refactra.auth_sessions import GitHubIdentity
from refactra.github_app import GitHubAppClient, InstallationToken
from refactra.github_repository_access import require_github_write_access
from refactra.models import Run
from refactra.repository_access import RepositoryAccessPolicy


class RunAccessDenied(ValueError):
    """The local policy no longer allows this run owner, repository or installation."""


@dataclass(frozen=True)
class RepositoryGrant:
    """Current write access, with the App client and a repository-scoped read token."""

    identity: GitHubIdentity
    installation_id: int
    app: GitHubAppClient
    token: InstallationToken


class RepositoryAuthorizer:
    """Check local eligibility, then current GitHub write permission, for one repository.

    Settings come from the environment on every call; the GitHub transport can be injected,
    which tests use to simulate GitHub. Errors are left for callers to map:
    HTTPException for local session rejection or current GitHub denial, RunAccessDenied for a
    queued run the local policy no longer allows, GitHubAppError when GitHub cannot verify
    access, and KeyError, OSError or ValueError for unusable settings.
    """

    def __init__(self, *, transport: httpx.BaseTransport | None = None):
        self.transport = transport

    @staticmethod
    def _installation_id() -> int:
        installation_id = int(os.environ["REFACTRA_GITHUB_INSTALLATION_ID"])
        if installation_id <= 0:
            raise ValueError("Invalid App configuration")
        return installation_id

    def _app(self) -> GitHubAppClient:
        client_id = os.environ["REFACTRA_GITHUB_CLIENT_ID"].strip()
        if not client_id:
            raise ValueError("Invalid App configuration")
        key = Path(os.environ["REFACTRA_GITHUB_PRIVATE_KEY_PATH"]).read_bytes()
        return GitHubAppClient(client_id, key, self.transport)

    def for_session(
        self,
        session_factory,
        session_token: str | None,
        repository_id: int,
        *,
        expected_installation: int | None = None,
    ) -> RepositoryGrant:
        """Authorize a signed-in browser user; the local policy runs before credentials load."""
        policy = RepositoryAccessPolicy.from_env()
        with session_factory() as session:
            identity = policy.require_eligible_session(session, session_token, repository_id)
        installation_id = self._installation_id()
        if expected_installation is not None and installation_id != expected_installation:
            raise HTTPException(403, "Repository installation changed.")
        app = self._app()
        token = require_github_write_access(app, installation_id, repository_id, identity)
        return RepositoryGrant(identity, installation_id, app, token)

    def for_run(self, run: Run) -> RepositoryGrant:
        """Recheck a queued run's owner, repository and installation, then GitHub permission."""
        policy = RepositoryAccessPolicy.from_env()
        installation_id = self._installation_id()
        if (
            run.github_repository_id != policy.repository_id
            or run.requested_by not in policy.allowed_user_ids
            or installation_id != run.github_installation_id
        ):
            raise RunAccessDenied("Repository access is not allowed.")
        app = self._app()
        identity = GitHubIdentity(run.requested_by, "operator")
        token = require_github_write_access(
            app, installation_id, run.github_repository_id, identity
        )
        return RepositoryGrant(identity, installation_id, app, token)


def authorize_run_repository(
    run: Run, authorizer: RepositoryAuthorizer | None = None
) -> tuple[GitHubAppClient, InstallationToken]:
    """Worker entry point: recheck the pinned run owner and return the App and read token."""
    grant = (authorizer or RepositoryAuthorizer()).for_run(run)
    return grant.app, grant.token
