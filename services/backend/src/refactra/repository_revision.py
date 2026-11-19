"""Resolve a selected branch of an authorized repository to a fixed commit.

Callers must apply session/tester/repository and current GitHub permission checks
before supplying a scoped read token. This helper does not persist a run pin.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from refactra.github_app import GitHubAppError, InstallationToken


@dataclass(frozen=True)
class RepositoryRevision:
    repository_id: int
    full_name: str
    default_branch: str
    base_branch: str
    commit_sha: str


def _validate_branch(branch: str) -> None:
    if (
        not isinstance(branch, str)
        or not branch
        or branch.startswith(("-", "/"))
        or branch.endswith(("/", "."))
        or branch in {"@", "HEAD"}
        or any(part in branch for part in ("..", "//", "@{"))
        or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", branch)
        or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
    ):
        raise ValueError("A literal branch name is required.")


def resolve_revision(
    token: InstallationToken,
    *,
    base_branch: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> RepositoryRevision:
    """Resolve a literal branch name, using the repository default only when omitted."""
    if base_branch is not None:
        _validate_branch(base_branch)
    try:
        if (
            type(token.repository_id) is not int
            or token.repository_id <= 0
            or not isinstance(token.value, str)
            or not token.value.strip()
            or token.expires_at.tzinfo is None
            or token.expires_at <= datetime.now(UTC)
        ):
            raise ValueError("Invalid or expired repository token")
        with httpx.Client(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {token.value}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
            timeout=10.0,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            response = client.get(f"/repositories/{token.repository_id}")
            response.raise_for_status()
            repository = response.json()
            name, default_branch = repository["full_name"], repository["default_branch"]
            if (
                type(repository["id"]) is not int
                or repository["id"] != token.repository_id
                or not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name)
                or any(part in {".", ".."} for part in name.split("/"))
            ):
                raise ValueError("Unexpected repository identity")
            _validate_branch(default_branch)
            branch = default_branch if base_branch is None else base_branch
            response = client.get(f"/repos/{name}/git/ref/{quote('heads/' + branch, safe='')}")
            response.raise_for_status()
            reference = response.json()
            sha = reference["object"]["sha"]
            if (
                reference["ref"] != f"refs/heads/{branch}"
                or reference["object"]["type"] != "commit"
                or not isinstance(sha, str)
                or not re.fullmatch(r"[0-9a-f]{40}", sha)
            ):
                raise ValueError("Unexpected branch reference or commit SHA")
        return RepositoryRevision(
            repository_id=token.repository_id,
            full_name=name,
            default_branch=default_branch,
            base_branch=branch,
            commit_sha=sha,
        )
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise GitHubAppError("Repository revision could not be verified.") from None


def resolve_default_revision(
    token: InstallationToken, *, transport: httpx.BaseTransport | None = None
) -> RepositoryRevision:
    """Convenience entry point for callers intentionally selecting the default branch."""
    return resolve_revision(token, transport=transport)
