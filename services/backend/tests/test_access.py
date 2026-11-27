import io

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

import refactra.repository_access as repository_access
from refactra.access import AccessError, run
from refactra.api import create_app
from refactra.auth_sessions import GitHubIdentity, issue_session
from refactra.config import Settings
from refactra.models import UserAccess
from refactra.repository_access import RepositoryAccessPolicy

OWNER, GUEST = 1, 2
POLICY = RepositoryAccessPolicy(frozenset({OWNER}))


def status(session_factory, user_id):
    with session_factory() as session:
        return session.scalar(select(UserAccess.status).where(UserAccess.github_user_id == user_id))


def add(session_factory, user_id, value, login="guest"):
    with session_factory.begin() as session:
        session.add(UserAccess(github_user_id=user_id, github_login=login, status=value))


@pytest.mark.parametrize(
    "row,expected",
    [(None, "denied"), ("invited", "allowed"), ("waitlisted", "waitlisted"), ("removed", "denied")],
)
def test_access_combines_owners_and_invite_rows(session_factory, row, expected):
    if row:
        add(session_factory, GUEST, row)
    with session_factory() as session:
        assert POLICY.access(session, OWNER) == "allowed"
        assert POLICY.access(session, GUEST) == expected


def test_a_waitlisted_user_gets_a_friendly_refusal(session_factory):
    add(session_factory, GUEST, "waitlisted")
    with session_factory.begin() as session:
        token = issue_session(session, GitHubIdentity(GUEST, "guest"))
    with session_factory() as session, pytest.raises(HTTPException) as refused:
        POLICY.require_allowed_user(session, token)
    assert refused.value.status_code == 403 and refused.value.detail["code"] == "waitlisted"


def test_signing_in_joins_the_waitlist_once_and_removed_users_stay_out(session_factory):
    with session_factory.begin() as session:
        assert POLICY.join_waitlist(session, GitHubIdentity(OWNER, "owner")) == "allowed"
        assert POLICY.join_waitlist(session, GitHubIdentity(GUEST, "guest")) == "waitlisted"
        assert POLICY.join_waitlist(session, GitHubIdentity(GUEST, "renamed")) == "waitlisted"
    assert status(session_factory, OWNER) is None and status(session_factory, GUEST) == "waitlisted"
    with session_factory() as session:
        assert session.get(UserAccess, GUEST).github_login == "renamed"
    add(session_factory, 3, "removed", "gone")
    with session_factory.begin() as session:
        assert POLICY.join_waitlist(session, GitHubIdentity(3, "gone")) == "denied"
    assert status(session_factory, 3) == "removed"


def test_workers_count_invites_only_with_a_registered_store(session_factory, monkeypatch):
    add(session_factory, GUEST, "invited")
    monkeypatch.setattr(repository_access, "_invite_sessions", None)
    assert POLICY.allows(OWNER) and not POLICY.allows(GUEST)
    repository_access.use_invite_store(session_factory)
    assert POLICY.allows(GUEST) and not POLICY.allows(3)


def github(users):
    def handler(request):
        key = request.url.path.rsplit("/", 1)[-1]
        if key not in users:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json=users[key])

    return httpx.MockTransport(handler)


def command(session_factory, *argv, transport=None):
    out = io.StringIO()
    run(list(argv), session_factory, transport=transport, out=out)
    return out.getvalue()


def test_the_command_lists_invites_and_removes(session_factory):
    assert command(session_factory, "waitlist") == "The waitlist is empty.\n"
    add(session_factory, GUEST, "waitlisted", "Guest-User")
    assert command(session_factory, "waitlist").startswith("Guest-User (2)  waitlisted  since ")
    # A waitlisted login is found case-insensitively without calling GitHub.
    assert command(session_factory, "invite", "guest-user", transport=github({})) == (
        "Invited Guest-User (2).\n"
    )
    assert status(session_factory, GUEST) == "invited"
    # Someone who never signed in is looked up on GitHub by login or ID.
    users = {"octocat": {"id": 583231, "login": "octocat"}, "9": {"id": 9, "login": "nine"}}
    assert command(session_factory, "invite", "octocat", transport=github(users)) == (
        "Invited octocat (583231).\n"
    )
    assert command(session_factory, "invite", "9", transport=github(users)) == "Invited nine (9).\n"
    assert command(session_factory, "remove", "octocat") == "Removed access for octocat (583231).\n"
    assert status(session_factory, 583231) == "removed"
    assert "octocat (583231)  removed" in command(session_factory, "list")


@pytest.mark.parametrize("who", ["missing", "bad login!"])
def test_the_command_reports_unknown_or_malformed_users(session_factory, who):
    with pytest.raises(AccessError):
        command(session_factory, "invite", who, transport=github({}))


def test_session_status_reports_access(session_factory, database, monkeypatch):
    monkeypatch.setenv("REFACTRA_ALLOWED_GITHUB_USER_IDS", str(OWNER))
    add(session_factory, GUEST, "waitlisted")
    with TestClient(create_app(Settings("unused"), database)) as client:
        assert client.get("/api/v1/auth/session").json()["access"] is None
        for user_id, expected in ((OWNER, "allowed"), (GUEST, "waitlisted")):
            with session_factory.begin() as session:
                client.cookies.set(
                    "refactra_session", issue_session(session, GitHubIdentity(user_id, "u"))
                )
            assert client.get("/api/v1/auth/session").json()["access"] == expected
            assert client.get("/api/v1/usage").status_code == (
                200 if expected == "allowed" else 403
            )
