"""Operator-only diagnostic; this does not authorize browser users or live runs."""

import json
import os
import re
import sys
from pathlib import Path

import httpx

from refactra.github_app import AppNotInstalled, GitHubAppClient, GitHubAppError


def check_repository(
    client_id: str,
    private_key: bytes,
    repository_id: int,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    app = GitHubAppClient(client_id, private_key, transport)
    installation_id = app.repository_installation(repository_id)
    token = app.mint_read_token(installation_id, repository_id)
    try:
        with httpx.Client(
            timeout=10.0, follow_redirects=False, trust_env=False, transport=transport
        ) as client:
            response = client.get(
                f"https://api.github.com/repositories/{repository_id}",
                headers={
                    "Authorization": f"Bearer {token.value}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2026-03-10",
                },
            )
        response.raise_for_status()
        repository = response.json()
        name = repository["full_name"]
        if (
            type(repository["id"]) is not int
            or repository["id"] != repository_id
            or not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name)
        ):
            raise ValueError("Unexpected repository identity")
        return {
            "repository": name,
            "repository_id": repository_id,
            "installation_id": installation_id,
            "access": "read-only",
            "expires_at": token.expires_at.isoformat(),
        }
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise GitHubAppError("GitHub repository read could not be verified.") from None


def main(argv: list[str] | None = None) -> int:
    """Check one repository: its ID from the first argument or REFACTRA_GITHUB_REPOSITORY_ID."""
    argv = sys.argv[1:] if argv is None else argv
    try:
        repository = argv[0] if argv else os.environ["REFACTRA_GITHUB_REPOSITORY_ID"]
        result = check_repository(
            os.environ["REFACTRA_GITHUB_CLIENT_ID"],
            Path(os.environ["REFACTRA_GITHUB_PRIVATE_KEY_PATH"]).read_bytes(),
            int(repository),
        )
    except AppNotInstalled:
        print("The App is not installed on that repository.", file=sys.stderr)
        return 1
    except (OSError, KeyError, ValueError, GitHubAppError):
        print(
            "Repository check failed. Check Client ID, PEM file, the repository ID "
            "and the App's Contents read permission. No credentials are shown.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
