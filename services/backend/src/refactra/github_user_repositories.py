"""List the repositories a signed-in user can reach through Refactra's GitHub App.

GitHub answers these with the user's own token, so the result is the intersection of what the
user can access and what the App is installed on. It is a picker list only: every run still
checks the repository and the user's write permission again before doing anything.
"""

import os
import re
from dataclasses import dataclass

import httpx

API = "https://api.github.com"
PAGE_SIZE = 100
# Caps keep one request bounded; the listing reports when they cut it short.
MAX_INSTALLATIONS = 20
MAX_REPOSITORY_PAGES = 10
SLUG_ENV = "REFACTRA_GITHUB_APP_SLUG"
_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?")
_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")


class UserTokenRejected(Exception):
    """GitHub no longer accepts the user's token, for example after the user revoked it."""


@dataclass(frozen=True)
class Installation:
    id: int
    account: str
    account_type: str
    repository_selection: str

    @property
    def settings_url(self) -> str:
        """Where the user changes which repositories the App can reach."""
        if self.account_type == "Organization":
            return (
                f"https://github.com/organizations/{self.account}/settings/installations/{self.id}"
            )
        return f"https://github.com/settings/installations/{self.id}"


@dataclass(frozen=True)
class UserRepository:
    id: int
    full_name: str
    private: bool
    default_branch: str
    installation_id: int
    # Refactra publishes a branch and pull request, which needs write access.
    writable: bool


@dataclass(frozen=True)
class RepositoryListing:
    installations: tuple[Installation, ...]
    repositories: tuple[UserRepository, ...]
    truncated: bool


def install_url() -> str | None:
    slug = os.getenv(SLUG_ENV, "").strip()
    if not slug:
        return None
    if _SLUG.fullmatch(slug) is None:
        raise ValueError(f"{SLUG_ENV} must be the App's URL name, such as refactra-app.")
    return f"https://github.com/apps/{slug}/installations/new"


def _positive_int(value) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("Expected a positive integer")
    return value


def _installation(data) -> Installation:
    account = data["account"]
    login, kind = account["login"], account["type"]
    selection = data["repository_selection"]
    if (
        not isinstance(login, str)
        or _LOGIN.fullmatch(login) is None
        or kind not in {"User", "Organization"}
        or selection not in {"all", "selected"}
    ):
        raise ValueError("Unexpected installation")
    return Installation(_positive_int(data["id"]), login, kind, selection)


def _repository(data, installation_id: int) -> UserRepository:
    name, branch, private = data["full_name"], data["default_branch"], data["private"]
    permissions = data.get("permissions") or {}
    if (
        not isinstance(name, str)
        or _NAME.fullmatch(name) is None
        or not isinstance(branch, str)
        or not branch
        or type(private) is not bool
        or not isinstance(permissions, dict)
    ):
        raise ValueError("Unexpected repository")
    writable = permissions.get("push") is True or permissions.get("admin") is True
    return UserRepository(
        _positive_int(data["id"]), name, private, branch, installation_id, writable
    )


def list_user_repositories(
    token: str, *, transport: httpx.BaseTransport | None = None
) -> RepositoryListing:
    """Raises UserTokenRejected on 401, and httpx.HTTPError, ValueError, KeyError or TypeError
    when GitHub cannot be reached or answers unexpectedly."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    truncated = False
    with httpx.Client(
        base_url=API,
        headers=headers,
        timeout=10.0,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    ) as client:

        def page(path: str, number: int) -> dict:
            response = client.get(path, params={"per_page": PAGE_SIZE, "page": number})
            if response.status_code == 401:
                raise UserTokenRejected()
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or type(data.get("total_count")) is not int:
                raise ValueError("Unexpected page")
            return data

        first = page("/user/installations", 1)
        installations = [_installation(item) for item in first["installations"]]
        if first["total_count"] > len(installations) or len(installations) > MAX_INSTALLATIONS:
            truncated = True
        installations = installations[:MAX_INSTALLATIONS]

        repositories: dict[int, UserRepository] = {}
        for installation in installations:
            path = f"/user/installations/{installation.id}/repositories"
            seen = 0
            for number in range(1, MAX_REPOSITORY_PAGES + 1):
                data = page(path, number)
                items = data["repositories"]
                for item in items:
                    repository = _repository(item, installation.id)
                    repositories.setdefault(repository.id, repository)
                seen += len(items)
                if seen >= data["total_count"] or len(items) < PAGE_SIZE:
                    break
            else:
                truncated = True
    ordered = sorted(repositories.values(), key=lambda item: item.full_name.lower())
    return RepositoryListing(tuple(installations), tuple(ordered), truncated)
