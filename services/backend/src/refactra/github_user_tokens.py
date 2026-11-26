"""Encrypted storage and refresh of the signed-in user's GitHub token.

Refactra uses this token only to list the App installations and repositories the user can
reach. It is encrypted with AES-GCM under REFACTRA_TOKEN_ENCRYPTION_KEY and bound to its
session row and field, so a copied ciphertext cannot be used under another session. It is
never returned to the browser and is wiped on logout. Without a configured key, sign-in still
works but no token is kept.
"""

import os
from base64 import urlsafe_b64decode
from binascii import Error as Base64Error
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from secrets import token_bytes

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from refactra.models import AuthSession

KEY_ENV = "REFACTRA_TOKEN_ENCRYPTION_KEY"
FORMAT = b"\x01"
NONCE_BYTES = 12
# Refresh a little early so a token cannot expire during the request that uses it.
REFRESH_MARGIN = timedelta(minutes=5)
MAX_TOKEN_LENGTH = 1024


class TokenRejected(Exception):
    """GitHub refused the refresh token; the user must sign in again."""


@dataclass(frozen=True, repr=False)
class GitHubUserToken:
    """A user access token as GitHub returned it. Lifetimes are in seconds; None never expires."""

    access_token: str
    expires_in: int | None = None
    refresh_token: str | None = None
    refresh_expires_in: int | None = None

    @classmethod
    def from_response(cls, data: dict) -> "GitHubUserToken":
        access, refresh = data.get("access_token"), data.get("refresh_token")
        if not _valid_token(access) or data.get("token_type") != "bearer":
            raise ValueError("Invalid token response")
        if refresh is not None and not _valid_token(refresh):
            raise ValueError("Invalid refresh token")
        lifetimes = [data.get("expires_in"), data.get("refresh_token_expires_in")]
        if any(value is not None and (type(value) is not int or value <= 0) for value in lifetimes):
            raise ValueError("Invalid token lifetime")
        return cls(access, lifetimes[0], refresh, lifetimes[1] if refresh else None)


def _valid_token(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_TOKEN_LENGTH and value.isprintable()


class TokenCipher:
    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("The token encryption key must be 32 bytes.")
        self._aead = AESGCM(key)

    @classmethod
    def from_env(cls) -> "TokenCipher | None":
        value = os.getenv(KEY_ENV, "").strip()
        if not value:
            return None
        try:
            key = urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except (Base64Error, ValueError):
            raise ValueError(f"{KEY_ENV} must be 32 random bytes in URL-safe base64.") from None
        if len(key) != 32:
            raise ValueError(f"{KEY_ENV} must be 32 random bytes in URL-safe base64.")
        return cls(key)

    def seal(self, plaintext: str, context: str) -> bytes:
        nonce = token_bytes(NONCE_BYTES)
        data = self._aead.encrypt(nonce, plaintext.encode("utf-8"), _context(context))
        return FORMAT + nonce + data

    def open(self, blob: bytes, context: str) -> str | None:
        """Return the plaintext, or None if the blob is damaged or belongs elsewhere."""
        if not blob.startswith(FORMAT) or len(blob) <= 1 + NONCE_BYTES:
            return None
        nonce, data = blob[1 : 1 + NONCE_BYTES], blob[1 + NONCE_BYTES :]
        try:
            return self._aead.decrypt(nonce, data, _context(context)).decode("utf-8")
        except (InvalidTag, UnicodeDecodeError):
            return None


def _context(context: str) -> bytes:
    return f"refactra-github-user-token:v1:{context}".encode()


def store_user_token(
    session: Session, row: AuthSession, token: GitHubUserToken, cipher: TokenCipher
) -> None:
    """Encrypt the token into its session row; the caller commits."""
    now = session.scalar(select(func.now()))
    row.github_token_ciphertext = cipher.seal(token.access_token, f"{row.token_hash}:access")
    row.github_token_expires_at = (
        now + timedelta(seconds=token.expires_in) if token.expires_in else None
    )
    row.github_refresh_ciphertext = (
        cipher.seal(token.refresh_token, f"{row.token_hash}:refresh")
        if token.refresh_token
        else None
    )
    row.github_refresh_expires_at = (
        now + timedelta(seconds=token.refresh_expires_in)
        if token.refresh_token and token.refresh_expires_in
        else None
    )


def clear_user_token(row: AuthSession) -> None:
    row.github_token_ciphertext = None
    row.github_token_expires_at = None
    row.github_refresh_ciphertext = None
    row.github_refresh_expires_at = None


def current_user_token(
    session: Session,
    token_hash: str,
    cipher: TokenCipher,
    refresh: Callable[[str], GitHubUserToken],
) -> str | None:
    """Return a usable GitHub token for an active session, refreshing it when it is about to
    expire. None means the user must sign in again. The caller owns the transaction; the row
    stays locked during a refresh because GitHub accepts each refresh token only once.
    """
    row = session.scalar(
        select(AuthSession)
        .where(
            AuthSession.token_hash == token_hash,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > func.now(),
        )
        .with_for_update()
    )
    if row is None or row.github_token_ciphertext is None:
        return None
    now = session.scalar(select(func.now()))
    access = cipher.open(row.github_token_ciphertext, f"{token_hash}:access")
    if access is None:
        clear_user_token(row)
        return None
    expires = row.github_token_expires_at
    if expires is None or expires > now + REFRESH_MARGIN:
        return access
    refresh_token = (
        cipher.open(row.github_refresh_ciphertext, f"{token_hash}:refresh")
        if row.github_refresh_ciphertext is not None
        and (row.github_refresh_expires_at is None or row.github_refresh_expires_at > now)
        else None
    )
    if refresh_token is None:
        clear_user_token(row)
        return None
    try:
        renewed = refresh(refresh_token)
    except TokenRejected:
        clear_user_token(row)
        return None
    store_user_token(session, row, renewed, cipher)
    return renewed.access_token
