"""Local eligibility gate; GitHub repository permissions must be checked separately.

Repository routes apply this gate first; passing it alone does not authorize a GitHub operation.
"""

import os
import re
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.orm import Session

from refactra.auth_sessions import GitHubIdentity, resolve_session


def _positive_id(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", value.strip()):
        raise ValueError("Repository access settings require positive numeric IDs.")
    return int(value)


@dataclass(frozen=True)
class RepositoryAccessPolicy:
    """Who may use Refactra: the invite list. Which repositories is GitHub's decision, through
    the App installation and the user's current write permission."""

    allowed_user_ids: frozenset[int] = frozenset()

    @classmethod
    def from_env(cls) -> "RepositoryAccessPolicy":
        users = os.getenv("REFACTRA_ALLOWED_GITHUB_USER_IDS", "").strip()
        return cls(
            allowed_user_ids=(
                frozenset(_positive_id(value) for value in users.split(","))
                if users
                else frozenset()
            ),
        )

    def require_allowed_user(self, session: Session, token: str | None) -> GitHubIdentity:
        """Resolve an active session for a user on the operator's invite list."""
        identity = resolve_session(session, token)
        if identity is None:
            raise HTTPException(
                401,
                {"code": "authentication_required", "message": "Sign in to access repositories."},
            )
        if identity.user_id not in self.allowed_user_ids:
            raise HTTPException(
                403,
                {
                    "code": "repository_access_denied",
                    "message": "Repository access is not allowed.",
                },
            )
        return identity
