"""Internal session storage. Issue only after the OAuth callback verifies identity.

The GitHub callback calls this service. Protected actions must separately verify
the session, CSRF protection and current repository permissions.
"""

import re
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from secrets import token_urlsafe

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from refactra.models import AuthSession

SESSION_TTL = timedelta(hours=8)
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


@dataclass(frozen=True)
class GitHubIdentity:
    user_id: int
    login: str

    def __post_init__(self):
        if self.user_id <= 0 or not self.login.strip() or len(self.login) > 255:
            raise ValueError("A positive GitHub user ID and nonblank login are required.")


def _token_hash(token: str) -> str:
    return sha256(token.encode("ascii")).hexdigest()


def issue_session(session: Session, identity: GitHubIdentity) -> str:
    """Caller owns the transaction and must commit before returning the token to a browser."""
    token = token_urlsafe(32)
    now = session.scalar(select(func.now()))
    session.add(
        AuthSession(
            token_hash=_token_hash(token),
            github_user_id=identity.user_id,
            github_login=identity.login,
            expires_at=now + SESSION_TTL,
        )
    )
    session.flush()
    return token


def resolve_session(session: Session, token: str | None) -> GitHubIdentity | None:
    if token is None or TOKEN_PATTERN.fullmatch(token) is None:
        return None
    stored = session.scalar(
        select(AuthSession).where(
            AuthSession.token_hash == _token_hash(token),
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > func.now(),
        )
    )
    if stored is None:
        return None
    return GitHubIdentity(user_id=stored.github_user_id, login=stored.github_login)


def revoke_session(session: Session, token: str | None) -> None:
    """Idempotent revocation; the caller commits it in the same transaction as logout."""
    if token is not None and TOKEN_PATTERN.fullmatch(token) is not None:
        session.execute(
            update(AuthSession)
            .where(
                AuthSession.token_hash == _token_hash(token),
                AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=func.now())
        )
