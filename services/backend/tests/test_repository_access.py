from datetime import timedelta
from hashlib import sha256

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from refactra.auth_sessions import GitHubIdentity, issue_session, revoke_session
from refactra.models import AuthSession
from refactra.repository_access import RepositoryAccessPolicy


@pytest.fixture
def signed_in(session_factory):
    identity = GitHubIdentity(12345, "renamed-fixture-user")
    with session_factory.begin() as session:
        token = issue_session(session, identity)
    return identity, token


def test_missing_configuration_denies_by_default(monkeypatch):
    monkeypatch.delenv("REFACTRA_ALLOWED_GITHUB_USER_IDS", raising=False)
    assert RepositoryAccessPolicy.from_env() == RepositoryAccessPolicy()


def test_configuration_uses_numeric_ids_and_removes_duplicates(monkeypatch):
    monkeypatch.setenv("REFACTRA_ALLOWED_GITHUB_USER_IDS", " 12345, 67890,12345 ")
    assert RepositoryAccessPolicy.from_env() == RepositoryAccessPolicy(frozenset({12345, 67890}))


@pytest.mark.parametrize("value", ["*", "fixture-user", "12345,,67890", "0", "-42"])
def test_invalid_configuration_is_rejected_without_echoing_values(monkeypatch, value):
    monkeypatch.setenv("REFACTRA_ALLOWED_GITHUB_USER_IDS", value)
    with pytest.raises(ValueError, match="positive numeric IDs") as error:
        RepositoryAccessPolicy.from_env()
    assert value not in str(error.value)


def test_active_invited_user_is_allowed(session_factory, signed_in):
    identity, token = signed_in
    policy = RepositoryAccessPolicy(frozenset({identity.user_id}))
    with session_factory() as session:
        assert policy.require_allowed_user(session, token) == identity


def test_same_login_with_a_different_user_id_is_not_invited(session_factory, signed_in):
    identity, _ = signed_in
    policy = RepositoryAccessPolicy(frozenset({identity.user_id}))
    with session_factory.begin() as session:
        token = issue_session(session, GitHubIdentity(99999, identity.login))
    with session_factory() as session, pytest.raises(HTTPException) as error:
        policy.require_allowed_user(session, token)
    assert error.value.status_code == 403
    assert error.value.detail["code"] == "repository_access_denied"
    assert token not in str(error.value)


def test_an_empty_invite_list_denies_everyone(session_factory, signed_in):
    _, token = signed_in
    policy = RepositoryAccessPolicy(frozenset())
    with session_factory() as session, pytest.raises(HTTPException) as error:
        policy.require_allowed_user(session, token)
    assert error.value.status_code == 403
    assert error.value.detail["code"] == "repository_access_denied"


@pytest.mark.parametrize("state", ["missing", "unknown", "expired", "revoked"])
def test_missing_or_inactive_sessions_cannot_pass_the_policy(session_factory, signed_in, state):
    identity, token = signed_in
    policy = RepositoryAccessPolicy(frozenset({identity.user_id}))
    if state == "missing":
        token = None
    elif state == "unknown":
        token = "A" * 43
    else:
        with session_factory.begin() as session:
            if state == "revoked":
                revoke_session(session, token)
            else:
                stored = session.get(AuthSession, sha256(token.encode()).hexdigest())
                now = session.scalar(select(func.now()))
                stored.created_at = now - timedelta(hours=9)
                stored.expires_at = now - timedelta(seconds=1)
    with session_factory() as session, pytest.raises(HTTPException) as error:
        policy.require_allowed_user(session, token)
    assert error.value.status_code == 401
    assert error.value.detail["code"] == "authentication_required"
