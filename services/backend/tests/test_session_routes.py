from datetime import timedelta
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

import refactra.session_routes as session_routes
from refactra.api import create_app
from refactra.auth_sessions import GitHubIdentity, issue_session, resolve_session
from refactra.config import Settings
from refactra.github_auth import CALLBACK_PATH, GitHubOAuthConfig
from refactra.models import AuthSession

ORIGIN = "https://refactra.example"
LOGOUT_HEADERS = {"Origin": ORIGIN, "X-Refactra-CSRF": "1"}


@pytest.fixture
def browser(database, session_factory, monkeypatch):
    monkeypatch.setenv("REFACTRA_GITHUB_CALLBACK_URL", f"{ORIGIN}{CALLBACK_PATH}")
    config = GitHubOAuthConfig.from_env()
    with session_factory.begin() as session:
        token = issue_session(session, GitHubIdentity(user_id=12345, login="session-user"))
    with TestClient(create_app(Settings("unused"), database), base_url=ORIGIN) as client:
        client.cookies.set(config.session_cookie_name, token, domain="refactra.example", path="/")
        yield client, config, token


@pytest.mark.parametrize(
    "state", ["valid", "missing", "unknown", "malformed", "expired", "revoked"]
)
def test_status_exposes_only_an_active_sessions_identity(browser, session_factory, state):
    client, config, token = browser
    if state == "missing":
        client.cookies.clear()
    elif state in {"unknown", "malformed"}:
        client.cookies.set(
            config.session_cookie_name,
            "A" * 43 if state == "unknown" else "bad",
            domain="refactra.example",
            path="/",
        )
    elif state in {"expired", "revoked"}:
        with session_factory.begin() as session:
            stored = session.get(AuthSession, sha256(token.encode()).hexdigest())
            now = session.scalar(select(func.now()))
            if state == "expired":
                stored.created_at, stored.expires_at = (
                    now - timedelta(hours=9),
                    now - timedelta(seconds=1),
                )
            else:
                stored.revoked_at = now
    response = client.get("/api/v1/auth/session")
    assert response.status_code == 200
    assert response.json() == {
        "authenticated": state == "valid",
        "user": {"user_id": 12345, "login": "session-user"} if state == "valid" else None,
    }
    assert response.headers["cache-control"] == "no-store"
    if state not in {"valid", "missing"}:
        assert "Max-Age=0" in response.headers["set-cookie"]
        assert client.cookies.get(config.session_cookie_name) is None


def test_logout_revokes_the_server_session_and_clears_cookies(browser, session_factory):
    client, config, token = browser
    client.cookies.set(config.cookie_name, "pending-attempt", domain="refactra.example", path="/")
    response = client.post("/api/v1/auth/logout", headers=LOGOUT_HEADERS)
    assert response.status_code == 204 and response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert client.cookies.get(config.session_cookie_name) is None
    assert client.cookies.get(config.cookie_name) is None
    assert all(
        "HttpOnly" in item and "Secure" in item and "SameSite=lax" in item
        for item in response.headers.get_list("set-cookie")
    )
    with session_factory() as session:
        assert resolve_session(session, token) is None
    assert client.get("/api/v1/auth/session").json() == {"authenticated": False, "user": None}
    assert client.post("/api/v1/auth/logout", headers=LOGOUT_HEADERS).status_code == 204


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": ORIGIN},
        {"X-Refactra-CSRF": "1"},
        {"Origin": ORIGIN, "X-Refactra-CSRF": "0"},
        {"Origin": "https://refactra.example.evil.test", "X-Refactra-CSRF": "1"},
        {"Origin": "null", "X-Refactra-CSRF": "1"},
    ],
)
def test_logout_rejects_untrusted_requests_without_revoking_or_clearing(
    browser, session_factory, headers
):
    client, _, token = browser
    response = client.post("/api/v1/auth/logout", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_rejected"
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    with session_factory() as session:
        assert resolve_session(session, token) is not None


def test_get_cannot_log_out_and_database_failure_does_not_clear_cookie(
    browser, session_factory, monkeypatch
):
    client, config, token = browser
    assert client.get("/api/v1/auth/logout").status_code == 405

    def fail_revoke(*_):
        raise SQLAlchemyError("private database details")

    monkeypatch.setattr(session_routes, "revoke_session", fail_revoke)
    response = client.post("/api/v1/auth/logout", headers=LOGOUT_HEADERS)
    assert response.status_code == 503
    assert "set-cookie" not in response.headers
    assert "private database details" not in response.text
    assert client.cookies.get(config.session_cookie_name) == token
    with session_factory() as session:
        assert resolve_session(session, token) is not None
