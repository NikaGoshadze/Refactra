from base64 import urlsafe_b64encode

import pytest
from sqlalchemy import select, text

from refactra.auth_sessions import (
    GitHubIdentity,
    issue_session,
    revoke_session,
    session_token_hash,
)
from refactra.github_user_tokens import (
    KEY_ENV,
    GitHubUserToken,
    TokenCipher,
    TokenRejected,
    current_user_token,
)
from refactra.models import AuthSession

KEY = urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
IDENTITY = GitHubIdentity(user_id=12345, login="token-user")
EXPIRING = GitHubUserToken("ghu_access", 28800, "ghr_refresh", 15897600)


@pytest.fixture
def cipher(monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)
    return TokenCipher.from_env()


def test_sealed_tokens_open_only_in_their_own_context(cipher):
    blob = cipher.seal("ghu_secret", "session-a:access")
    assert b"ghu_secret" not in blob
    assert cipher.open(blob, "session-a:access") == "ghu_secret"
    assert cipher.open(blob, "session-b:access") is None
    assert cipher.open(blob, "session-a:refresh") is None
    tampered = blob[:-1] + bytes([blob[-1] ^ 1])
    assert cipher.open(tampered, "session-a:access") is None
    assert cipher.seal("ghu_secret", "session-a:access") != blob  # Fresh nonce each time.


@pytest.mark.parametrize("value", ["not base64!", urlsafe_b64encode(b"short").decode()])
def test_an_invalid_key_is_rejected_and_a_missing_key_disables_storage(monkeypatch, value):
    monkeypatch.setenv(KEY_ENV, value)
    with pytest.raises(ValueError, match=KEY_ENV):
        TokenCipher.from_env()
    monkeypatch.setenv(KEY_ENV, " ")
    assert TokenCipher.from_env() is None


def test_token_responses_are_validated():
    token = GitHubUserToken.from_response(
        {
            "access_token": "ghu_a",
            "token_type": "bearer",
            "expires_in": 28800,
            "refresh_token": "ghr_r",
            "refresh_token_expires_in": 15897600,
        }
    )
    assert (token.expires_in, token.refresh_token) == (28800, "ghr_r")
    assert "ghu_a" not in repr(token)
    assert GitHubUserToken.from_response({"access_token": "gho_a", "token_type": "bearer"}) == (
        GitHubUserToken("gho_a")
    )
    for bad in (
        {"access_token": "", "token_type": "bearer"},
        {"access_token": "ghu_a", "token_type": "mac"},
        {"access_token": "ghu_a", "token_type": "bearer", "expires_in": "soon"},
        {"access_token": "ghu_a", "token_type": "bearer", "refresh_token": 7},
    ):
        with pytest.raises(ValueError):
            GitHubUserToken.from_response(bad)


def issue(session_factory, cipher, user_token=EXPIRING):
    with session_factory.begin() as session:
        token = issue_session(session, IDENTITY, user_token, cipher)
    return token, session_token_hash(token)


def expire_access(session_factory, token_hash, refresh_valid=True):
    with session_factory.begin() as session:
        session.execute(
            text(
                "UPDATE auth_sessions SET github_token_expires_at = now() + interval '1 minute',"
                " github_refresh_expires_at = now() + :refresh * interval '1 day'"
                " WHERE token_hash = :hash"
            ),
            {"hash": token_hash, "refresh": 1 if refresh_valid else -1},
        )


def read_token(session_factory, token_hash, cipher, refresh):
    with session_factory.begin() as session:
        return current_user_token(session, token_hash, cipher, refresh)


def no_refresh(_):
    pytest.fail("A fresh token must not be refreshed.")


def test_sign_in_stores_only_ciphertext_and_returns_a_fresh_token(session_factory, cipher):
    _, token_hash = issue(session_factory, cipher)
    with session_factory() as session:
        raw = (
            session.execute(
                text("SELECT * FROM auth_sessions WHERE token_hash = :hash"), {"hash": token_hash}
            )
            .mappings()
            .one()
        )
    assert b"ghu_access" not in raw["github_token_ciphertext"]
    assert b"ghr_refresh" not in raw["github_refresh_ciphertext"]
    assert raw["github_token_expires_at"] < raw["github_refresh_expires_at"]
    assert read_token(session_factory, token_hash, cipher, no_refresh) == "ghu_access"


def test_a_token_near_expiry_is_refreshed_once_and_saved(session_factory, cipher):
    _, token_hash = issue(session_factory, cipher)
    expire_access(session_factory, token_hash)
    calls = []

    def refresh(refresh_token):
        calls.append(refresh_token)
        return GitHubUserToken("ghu_renewed", 28800, "ghr_renewed", 15897600)

    assert read_token(session_factory, token_hash, cipher, refresh) == "ghu_renewed"
    assert calls == ["ghr_refresh"]
    # The renewed token is stored, so the next read needs no refresh.
    assert read_token(session_factory, token_hash, cipher, no_refresh) == "ghu_renewed"


@pytest.mark.parametrize("problem", ["rejected", "refresh_expired"])
def test_an_unrefreshable_token_is_wiped(session_factory, cipher, problem):
    _, token_hash = issue(session_factory, cipher)
    expire_access(session_factory, token_hash, refresh_valid=problem != "refresh_expired")

    def refresh(_):
        raise TokenRejected()

    assert read_token(session_factory, token_hash, cipher, refresh) is None
    with session_factory() as session:
        row = session.get(AuthSession, token_hash)
        assert row.github_token_ciphertext is None and row.github_refresh_ciphertext is None


def test_a_network_failure_during_refresh_keeps_the_stored_token(session_factory, cipher):
    _, token_hash = issue(session_factory, cipher)
    expire_access(session_factory, token_hash)

    def refresh(_):
        raise OSError("GitHub unreachable")

    with pytest.raises(OSError):
        read_token(session_factory, token_hash, cipher, refresh)
    with session_factory() as session:
        assert session.get(AuthSession, token_hash).github_token_ciphertext is not None


def test_a_non_expiring_token_is_used_as_is(session_factory, cipher):
    _, token_hash = issue(session_factory, cipher, GitHubUserToken("gho_classic"))
    assert read_token(session_factory, token_hash, cipher, no_refresh) == "gho_classic"


def test_logout_wipes_the_token_and_a_different_key_cannot_read_it(session_factory, cipher):
    token, token_hash = issue(session_factory, cipher)
    other = TokenCipher(bytes(32))
    assert read_token(session_factory, token_hash, other, no_refresh) is None
    token, token_hash = issue(session_factory, cipher)
    with session_factory.begin() as session:
        revoke_session(session, token)
    with session_factory() as session:
        row = session.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash))
        assert row.revoked_at is not None and row.github_token_ciphertext is None
    assert read_token(session_factory, token_hash, cipher, no_refresh) is None


def test_without_a_cipher_no_token_is_kept(session_factory):
    _, token_hash = issue(session_factory, None)
    with session_factory() as session:
        assert session.get(AuthSession, token_hash).github_token_ciphertext is None
