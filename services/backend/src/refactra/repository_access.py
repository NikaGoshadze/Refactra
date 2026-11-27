"""Local eligibility gate; GitHub repository permissions must be checked separately.

Repository routes apply this gate first; passing it alone does not authorize a GitHub operation.
Owners listed in REFACTRA_ALLOWED_GITHUB_USER_IDS always pass. Everyone else needs an
`invited` row in user_access; signing in without one puts the user on the waitlist.
"""

import os
import re
from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from refactra.auth_sessions import GitHubIdentity, resolve_session
from refactra.models import UserAccess

Access = Literal["allowed", "waitlisted", "denied"]
WAITLISTED = {
    "code": "waitlisted",
    "message": "You're on the Refactra waitlist. You'll get access once the owner invites you.",
}
DENIED = {"code": "repository_access_denied", "message": "Repository access is not allowed."}

# Background workers re-check queued runs without a request session. They register their
# session factory at startup so invites in the database count there too; without one, only
# the operator list in REFACTRA_ALLOWED_GITHUB_USER_IDS does.
_invite_sessions = None


def use_invite_store(session_factory) -> None:
    global _invite_sessions
    _invite_sessions = session_factory


def _positive_id(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", value.strip()):
        raise ValueError("Repository access settings require positive numeric IDs.")
    return int(value)


@dataclass(frozen=True)
class RepositoryAccessPolicy:
    """Who may use Refactra. Which repositories is GitHub's decision, through the App
    installation and the user's current write permission."""

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

    def access(self, session: Session, user_id: int) -> Access:
        if user_id in self.allowed_user_ids:
            return "allowed"
        status = session.scalar(
            select(UserAccess.status).where(UserAccess.github_user_id == user_id)
        )
        return {"invited": "allowed", "waitlisted": "waitlisted"}.get(status, "denied")

    def allows(self, user_id: int) -> bool:
        """For workers: the operator list, plus invites when a store is registered."""
        if user_id in self.allowed_user_ids:
            return True
        if _invite_sessions is None:
            return False
        with _invite_sessions() as session:
            return self.access(session, user_id) == "allowed"

    def require_allowed_user(self, session: Session, token: str | None) -> GitHubIdentity:
        """Resolve an active session for an owner or an invited user."""
        identity = resolve_session(session, token)
        if identity is None:
            raise HTTPException(
                401,
                {"code": "authentication_required", "message": "Sign in to access repositories."},
            )
        access = self.access(session, identity.user_id)
        if access != "allowed":
            raise HTTPException(403, WAITLISTED if access == "waitlisted" else DENIED)
        return identity

    def join_waitlist(self, session: Session, identity: GitHubIdentity) -> Access:
        """Record a signed-in user who is not allowed yet; the caller commits."""
        access = self.access(session, identity.user_id)
        if access == "allowed" or access == "waitlisted":
            if access == "waitlisted":
                session.execute(
                    UserAccess.__table__.update()
                    .where(UserAccess.github_user_id == identity.user_id)
                    .values(github_login=identity.login)
                )
            return access
        if access == "denied" and session.get(UserAccess, identity.user_id) is not None:
            return "denied"  # Removed users are not put back on the waitlist.
        session.execute(
            insert(UserAccess)
            .values(
                github_user_id=identity.user_id, github_login=identity.login, status="waitlisted"
            )
            .on_conflict_do_nothing()
        )
        return "waitlisted"
