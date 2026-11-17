"""Current GitHub permission check; callers must pass the local eligibility policy first.

Returns a read-only App token after verifying the user's write permission. No API
route uses this yet; the user's permission does not broaden the App token's scope.
"""

import re

import httpx
from fastapi import HTTPException

from refactra.auth_sessions import GitHubIdentity
from refactra.github_app import GitHubAppClient, GitHubAppError, InstallationToken


def _denied() -> HTTPException:
    return HTTPException(
        403,
        {
            "code": "repository_access_denied",
            "message": "Current repository write access is required.",
        },
    )


def require_github_write_access(
    app: GitHubAppClient, installation_id: int, repository_id: int, identity: GitHubIdentity
) -> InstallationToken:
    """Recheck GitHub on each call, using the durable user ID rather than session login."""
    if type(identity.user_id) is not int or identity.user_id <= 0:
        raise _denied()
    token = app.mint_read_token(installation_id, repository_id)
    try:
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
            transport=app.transport,
        ) as client:

            def get(path):
                response = client.get(path)
                if response.status_code == 404:
                    raise _denied()
                response.raise_for_status()
                return response.json()

            repository = get(f"/repositories/{repository_id}")
            name = repository["full_name"]
            if (
                type(repository["id"]) is not int
                or repository["id"] != repository_id
                or not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name)
            ):
                raise ValueError("Unexpected repository identity")
            user = get(f"/user/{identity.user_id}")
            login = user["login"]
            if (
                type(user["id"]) is not int
                or user["id"] != identity.user_id
                or not isinstance(login, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", login)
            ):
                raise ValueError("Unexpected user identity")
            result = get(f"/repos/{name}/collaborators/{login}/permission")
            if type(result["user"]["id"]) is not int or result["user"]["id"] != identity.user_id:
                raise ValueError("Unexpected permission subject")
            if result["permission"] not in {"write", "admin"}:
                raise _denied()
        return token
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise GitHubAppError("GitHub repository permissions could not be verified.") from None
