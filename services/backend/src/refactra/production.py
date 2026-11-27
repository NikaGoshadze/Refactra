"""Startup checks for REFACTRA_ENVIRONMENT=production.

Development (the default) keeps the local conveniences. In production the API and the worker
refuse to start until every setting below is safe, and they report the setting names that need
attention, never their values.
"""

import os
from pathlib import Path
from urllib.parse import urlsplit

from refactra.github_user_repositories import SLUG_ENV, install_url
from refactra.github_user_tokens import KEY_ENV, TokenCipher
from refactra.repository_access import RepositoryAccessPolicy

ENVIRONMENT_ENV = "REFACTRA_ENVIRONMENT"
ENVIRONMENTS = {"development", "production"}
LOCAL_DATABASE_PASSWORD = "refactra_local"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ProductionConfigError(RuntimeError):
    """Production settings are unsafe; the message lists setting names only."""


def environment() -> str:
    value = os.getenv(ENVIRONMENT_ENV, "").strip().lower() or "development"
    if value not in ENVIRONMENTS:
        raise ProductionConfigError(f"{ENVIRONMENT_ENV} must be development or production.")
    return value


def _setting(name: str) -> str:
    return os.getenv(name, "").strip()


def production_problems(role: str) -> list[str]:
    """What blocks a production start for the "api" or the "worker"."""
    problems = []
    if _setting("REFACTRA_ENABLE_MOCK").lower() == "true":
        problems.append(
            "REFACTRA_ENABLE_MOCK must be false: the mock queue accepts anonymous runs."
        )
    database = _setting("REFACTRA_DATABASE_URL")
    if not database or LOCAL_DATABASE_PASSWORD in database:
        problems.append("REFACTRA_DATABASE_URL must point to a database with its own password.")
    try:
        if not RepositoryAccessPolicy.from_env().allowed_user_ids:
            problems.append("REFACTRA_ALLOWED_GITHUB_USER_IDS must name at least one user.")
    except ValueError:
        problems.append("REFACTRA_ALLOWED_GITHUB_USER_IDS must be numeric GitHub user IDs.")
    if not _setting("REFACTRA_GITHUB_CLIENT_ID"):
        problems.append("REFACTRA_GITHUB_CLIENT_ID must be set.")
    key = _setting("REFACTRA_GITHUB_PRIVATE_KEY_PATH")
    if not key or not Path(key).is_file():
        problems.append("REFACTRA_GITHUB_PRIVATE_KEY_PATH must name the App's private key file.")
    if role == "worker":
        if _setting("REFACTRA_VALIDATION_RUNTIME") != "runsc":
            problems.append(
                "REFACTRA_VALIDATION_RUNTIME must be runsc: repository tests are untrusted code "
                "and run under gVisor in production."
            )
        return problems

    callback = urlsplit(_setting("REFACTRA_GITHUB_CALLBACK_URL"))
    if callback.scheme != "https" or not callback.hostname or callback.hostname in LOCAL_HOSTS:
        problems.append(
            "REFACTRA_GITHUB_CALLBACK_URL must be the public https:// callback, so cookies are "
            "Secure and __Host- prefixed."
        )
    if not _setting("REFACTRA_GITHUB_CLIENT_SECRET"):
        problems.append("REFACTRA_GITHUB_CLIENT_SECRET must be set.")
    if len(_setting("REFACTRA_OAUTH_STATE_SECRET")) < 32:
        problems.append("REFACTRA_OAUTH_STATE_SECRET must be at least 32 random characters.")
    if len(_setting("REFACTRA_GITHUB_WEBHOOK_SECRET")) < 32:
        problems.append(
            "REFACTRA_GITHUB_WEBHOOK_SECRET must be at least 32 random characters, matching the "
            "App's webhook secret, so uninstalls stop runs at once."
        )
    try:
        if TokenCipher.from_env() is None:
            problems.append(f"{KEY_ENV} must be set so users can list their repositories.")
    except ValueError:
        problems.append(f"{KEY_ENV} must be 32 random bytes in URL-safe base64.")
    try:
        if install_url() is None:
            problems.append(f"{SLUG_ENV} must be set so new users can install the App.")
    except ValueError:
        problems.append(f"{SLUG_ENV} must be the App's URL name.")
    return problems


def require_production_ready(role: str) -> str:
    """Return the environment name; raise when production settings are unsafe."""
    name = environment()
    if name == "production" and (problems := production_problems(role)):
        raise ProductionConfigError(
            "Production settings are not safe yet:\n- " + "\n- ".join(problems)
        )
    return name
