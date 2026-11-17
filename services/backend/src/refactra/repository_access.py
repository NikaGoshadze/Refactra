"""Local eligibility gate; GitHub repository permissions must be checked separately.

No API route uses this policy yet. Passing it does not authorize a GitHub operation.
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
    allowed_user_ids: frozenset[int] = frozenset()
    repository_id: int | None = None

    @classmethod
    def from_env(cls) -> "RepositoryAccessPolicy":
        users = os.getenv("REFACTRA_ALLOWED_GITHUB_USER_IDS", "").strip()
        repository = os.getenv("REFACTRA_GITHUB_REPOSITORY_ID", "").strip()
        return cls(
            allowed_user_ids=(
                frozenset(_positive_id(value) for value in users.split(","))
                if users
                else frozenset()
            ),
            repository_id=_positive_id(repository) if repository else None,
        )

    def require_eligible_session(
        self, session: Session, token: str | None, repository_id: int
    ) -> GitHubIdentity:
        """Resolve an active session, then apply the operator's user/repository limits."""
        identity = resolve_session(session, token)
        if identity is None:
            raise HTTPException(
                401,
                {"code": "authentication_required", "message": "Sign in to access repositories."},
            )
        if (
            identity.user_id not in self.allowed_user_ids
            or type(repository_id) is not int
            or repository_id != self.repository_id
        ):
            raise HTTPException(
                403,
                {
                    "code": "repository_access_denied",
                    "message": "Repository access is not allowed.",
                },
            )
        return identity
