"""One repository authorization policy for browser sessions and queued run owners.

Any repository works when the App is installed on it and the user can write to it; the local
policy only limits who may use Refactra (the invite list).
"""

import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from time import monotonic

import httpx
from fastapi import HTTPException

from refactra.auth_sessions import GitHubIdentity
from refactra.github_app import AppNotInstalled, GitHubAppClient, InstallationToken
from refactra.github_repository_access import require_github_write_access
from refactra.models import Run
from refactra.repository_access import RepositoryAccessPolicy

REPOSITORY_DENIED = {
    "code": "repository_access_denied",
    "message": "Repository access is not allowed.",
}
APP_NOT_INSTALLED = {
    "code": "app_not_installed",
    "message": "Refactra is not installed on this repository. Install it on GitHub first.",
}
INSTALLATION_CHANGED = {
    "code": "repository_installation_changed",
    "message": "This repository moved to another Refactra installation.",
}


class RunAccessDenied(ValueError):
    """The local policy no longer allows this run owner or installation."""


@dataclass(frozen=True)
class RepositoryGrant:
    """Current write access, with the App client and a repository-scoped read token."""

    identity: GitHubIdentity
    installation_id: int
    app: GitHubAppClient
    token: InstallationToken


class ReadAccessCache:
    """Reuse a recent positive GitHub permission check for read-only polling.

    Concurrent checks for the same key wait for one GitHub round trip. Entries expire after
    `ttl_seconds` and are never used once the token is within five minutes of expiry.
    Denials are never cached.
    """

    TOKEN_MARGIN = timedelta(minutes=5)

    def __init__(self, ttl_seconds: float = 30.0, clock=monotonic):
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: dict[tuple, tuple[float, RepositoryGrant]] = {}
        self._locks: dict[tuple, threading.Lock] = {}
        self._guard = threading.Lock()

    def get_or_check(self, key: tuple, check) -> RepositoryGrant:
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            now = self._clock()
            cached = self._entries.get(key)
            if (
                cached is not None
                and cached[0] > now
                and cached[1].token.expires_at - datetime.now(UTC) > self.TOKEN_MARGIN
            ):
                return cached[1]
            grant = check()
            with self._guard:
                self._entries = {k: v for k, v in self._entries.items() if v[0] > now}
                self._entries[key] = (now + self.ttl_seconds, grant)
            return grant


class RepositoryAuthorizer:
    """Check local eligibility, then current GitHub write permission, for one repository.

    Settings come from the environment on every call; the GitHub transport can be injected,
    which tests use to simulate GitHub. Errors are left for callers to map:
    HTTPException for local session rejection or current GitHub denial, RunAccessDenied for a
    queued run the local policy no longer allows, GitHubAppError when GitHub cannot verify
    access, and KeyError, OSError or ValueError for unusable settings.
    """

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        read_cache: ReadAccessCache | None = None,
    ):
        self.transport = transport
        self.read_cache = read_cache or ReadAccessCache()

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
        reuse: bool = False,
    ) -> RepositoryGrant:
        """Authorize a signed-in browser user; the local policy runs before credentials load.

        With `reuse`, for read-only requests only, a recent positive GitHub check for the same
        session, repository and installation may be reused. The local session and allowlist
        check still runs on every call, so sign-out and local revocation apply at once.
        """
        policy = RepositoryAccessPolicy.from_env()
        with session_factory() as session:
            identity = policy.require_allowed_user(session, session_token)
        if type(repository_id) is not int or repository_id <= 0:
            raise HTTPException(403, REPOSITORY_DENIED)

        def check() -> RepositoryGrant:
            app = self._app()
            try:
                installation_id = app.repository_installation(repository_id)
            except AppNotInstalled:
                raise HTTPException(403, APP_NOT_INSTALLED) from None
            if expected_installation is not None and installation_id != expected_installation:
                raise HTTPException(403, INSTALLATION_CHANGED)
            token = require_github_write_access(app, installation_id, repository_id, identity)
            return RepositoryGrant(identity, installation_id, app, token)

        if not reuse:
            return check()
        session_key = sha256((session_token or "").encode()).hexdigest()
        key = (identity.user_id, repository_id, expected_installation, session_key)
        return self.read_cache.get_or_check(key, check)

    def for_run(self, run: Run) -> RepositoryGrant:
        """Recheck a queued run's owner and installation, then the owner's GitHub permission."""
        policy = RepositoryAccessPolicy.from_env()
        if not policy.allows(run.requested_by):
            raise RunAccessDenied("Repository access is not allowed.")
        app = self._app()
        try:
            installation_id = app.repository_installation(run.github_repository_id)
        except AppNotInstalled:
            raise RunAccessDenied("The App is no longer installed on this repository.") from None
        if installation_id != run.github_installation_id:
            raise RunAccessDenied("The repository moved to another App installation.")
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
