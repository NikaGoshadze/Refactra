import time
from base64 import urlsafe_b64encode
from hashlib import sha256
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

import refactra.github_auth as github_auth
from refactra.api import create_app
from refactra.auth_sessions import resolve_session
from refactra.config import Settings
from refactra.github_auth import (
    CALLBACK_PATH,
    GitHubOAuthConfig,
    refresh_github_token,
    validate_oauth_attempt,
)
from refactra.github_user_tokens import KEY_ENV, TokenCipher, TokenRejected, current_user_token
from refactra.models import AuthSession


@pytest.fixture
def oauth(monkeypatch):
    config = GitHubOAuthConfig(
        "fixture-client", f"https://refactra.example{CALLBACK_PATH}", "x" * 32
    )
    monkeypatch.setenv("REFACTRA_GITHUB_CLIENT_ID", config.client_id)
    monkeypatch.setenv("REFACTRA_GITHUB_CALLBACK_URL", config.callback_url)
    monkeypatch.setenv("REFACTRA_OAUTH_STATE_SECRET", config.state_secret)
    monkeypatch.setenv("REFACTRA_GITHUB_CLIENT_SECRET", "")
    with TestClient(
        create_app(Settings("postgresql+psycopg://unused:unused@127.0.0.1:1/unused"))
    ) as client:
        yield config, client


def start(client, config):
    response = client.get("/api/v1/auth/github/login", follow_redirects=False)
    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return response, query, response.cookies[config.cookie_name]


def test_login_redirect_binds_random_state_to_pkce_and_a_protected_cookie(oauth):
    config, client = oauth
    response, query, cookie = start(client, config)
    target = urlsplit(response.headers["location"])
    assert (target.scheme, target.netloc, target.path) == (
        "https",
        "github.com",
        "/login/oauth/authorize",
    )
    assert query["client_id"] == [config.client_id]
    assert query["redirect_uri"] == [config.callback_url]
    assert query["code_challenge_method"] == ["S256"]
    verifier = validate_oauth_attempt(config, cookie, query["state"][0])
    challenge = urlsafe_b64encode(sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    assert query["code_challenge"] == [challenge]
    assert verifier not in response.headers["location"]
    assert "scope" not in query
    assert response.headers["cache-control"] == "no-store"
    header = response.headers["set-cookie"]
    assert all(
        flag in header for flag in ("HttpOnly", "Secure", "SameSite=lax", "Max-Age=600", "Path=/")
    )
    assert "Domain=" not in header
    _, second_query, second_cookie = start(client, config)
    assert second_query["state"] != query["state"]
    assert second_cookie != cookie


@pytest.mark.parametrize(
    "problem", ["missing_cookie", "missing_state", "mismatch", "tamper", "expired"]
)
def test_invalid_login_state_is_rejected(oauth, monkeypatch, problem):
    config, client = oauth
    _, query, cookie = start(client, config)
    state = query["state"][0]
    if problem == "missing_cookie":
        cookie = None
    elif problem == "missing_state":
        state = None
    elif problem == "mismatch":
        state = "A" * 43
    elif problem == "tamper":
        cookie += "corrupt"
    else:
        later = int(time.time()) + 601
        monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda _: later)
    with pytest.raises(HTTPException) as error:
        validate_oauth_attempt(config, cookie, state)
    assert error.value.status_code == 400
    assert error.value.detail["code"] == "invalid_oauth_state"


@pytest.mark.parametrize(
    "key,value",
    [
        ("REFACTRA_GITHUB_CLIENT_ID", ""),
        ("REFACTRA_OAUTH_STATE_SECRET", "short"),
        ("REFACTRA_GITHUB_CALLBACK_URL", f"http://refactra.example{CALLBACK_PATH}"),
        ("REFACTRA_GITHUB_CALLBACK_URL", f"https://refactra.example{CALLBACK_PATH}?next=evil"),
    ],
)
def test_missing_or_unsafe_configuration_disables_login(oauth, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with TestClient(
        create_app(Settings("postgresql+psycopg://unused:unused@127.0.0.1:1/unused"))
    ) as client:
        response = client.get("/api/v1/auth/github/login", follow_redirects=False)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "github_auth_unconfigured"


def test_local_http_uses_local_cookie_and_request_cannot_override_callback(oauth, monkeypatch):
    callback = f"http://localhost:3000{CALLBACK_PATH}"
    monkeypatch.setenv("REFACTRA_GITHUB_CALLBACK_URL", callback)
    config = GitHubOAuthConfig.from_env()
    with TestClient(
        create_app(Settings("postgresql+psycopg://unused:unused@127.0.0.1:1/unused"))
    ) as client:
        response = client.get(
            "/api/v1/auth/github/login?redirect_uri=https://evil.example", follow_redirects=False
        )
    assert response.status_code == 302
    assert parse_qs(urlsplit(response.headers["location"]).query)["redirect_uri"] == [callback]
    assert config.cookie_name == "refactra_oauth"
    assert "Secure" not in response.headers["set-cookie"]
    assert config.state_secret not in repr(config)


@pytest.fixture
def callback(oauth, database, monkeypatch):
    config, _ = oauth
    monkeypatch.setenv("REFACTRA_GITHUB_CLIENT_SECRET", "fixture-secret")
    monkeypatch.setenv(KEY_ENV, urlsafe_b64encode(bytes(range(32))).decode())
    config = GitHubOAuthConfig.from_env()
    calls, behavior = [], {}

    def upstream(request):
        calls.append(request)
        if behavior.get("problem") == "timeout":
            raise httpx.ReadTimeout("private upstream details", request=request)
        if str(request.url) == "https://github.com/login/oauth/access_token":
            body = (
                {"error": "bad_verification_code"}
                if behavior.get("problem") == "code"
                else {
                    "access_token": "fixture-github-token",
                    "token_type": "bearer",
                }
            )
        else:
            assert str(request.url) == "https://api.github.com/user"
            body = (
                {}
                if behavior.get("problem") == "identity"
                else {"id": 12345, "login": "verified-user"}
            )
        return httpx.Response(200, json=body)

    monkeypatch.setattr(
        github_auth,
        "Client",
        lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(upstream),
            **kwargs,
        ),
    )
    with TestClient(
        create_app(Settings("unused"), database), base_url="https://refactra.example"
    ) as client:
        yield config, client, calls, behavior


def test_callback_verifies_github_and_commits_a_rotated_session(callback, session_factory):
    config, client, calls, _ = callback
    _, query, cookie = start(client, config)
    verifier = validate_oauth_attempt(config, cookie, query["state"][0])
    response = client.get(
        CALLBACK_PATH,
        params={"code": "fixture-code", "state": query["state"][0]},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "https://refactra.example/"
    token = response.cookies[config.session_cookie_name]
    assert client.cookies.get(config.cookie_name) is None
    headers = " ".join(response.headers.get_list("set-cookie"))
    assert all(flag in headers for flag in ("HttpOnly", "Secure", "SameSite=lax", "Max-Age=28800"))
    assert "fixture-github-token" not in headers
    assert parse_qs(calls[0].content.decode()) == {
        "client_id": [config.client_id],
        "client_secret": ["fixture-secret"],
        "code": ["fixture-code"],
        "code_verifier": [verifier],
        "redirect_uri": [config.callback_url],
    }
    assert calls[1].headers["Authorization"] == "Bearer fixture-github-token"
    with session_factory() as session:
        identity = resolve_session(session, token)
        assert (identity.user_id, identity.login) == (12345, "verified-user")
        assert session.scalar(select(AuthSession.token_hash)) == sha256(token.encode()).hexdigest()
        # The GitHub token is kept only as ciphertext and never reaches the browser.
        stored = session.scalar(select(AuthSession.github_token_ciphertext))
        assert stored and b"fixture-github-token" not in stored
    with session_factory.begin() as session:
        assert (
            current_user_token(
                session, sha256(token.encode()).hexdigest(), TokenCipher.from_env(), pytest.fail
            )
            == "fixture-github-token"
        )
    _, query, _ = start(client, config)
    second = client.get(
        CALLBACK_PATH,
        params={"code": "second-code", "state": query["state"][0]},
        follow_redirects=False,
    )
    assert second.status_code == 302
    with session_factory() as session:
        assert resolve_session(session, token) is None
        assert resolve_session(session, second.cookies[config.session_cookie_name]) is not None


@pytest.mark.parametrize(
    "problem,status",
    [("state", 400), ("denied", 400), ("code", 401), ("timeout", 502), ("identity", 502)],
)
def test_failed_callback_clears_attempt_without_issuing_a_session(
    callback, session_factory, problem, status
):
    config, client, calls, behavior = callback
    _, query, _ = start(client, config)
    behavior["problem"] = problem
    params = {
        "code": "fixture-code",
        "state": "A" * 43 if problem == "state" else query["state"][0],
    }
    if problem == "denied":
        params["error"] = "access_denied"
    response = client.get(CALLBACK_PATH, params=params, follow_redirects=False)
    assert response.status_code == status
    assert config.session_cookie_name not in response.cookies
    assert client.cookies.get(config.cookie_name) is None
    assert response.headers["cache-control"] == "no-store"
    assert "private upstream details" not in response.text
    if problem in {"state", "denied"}:
        assert not calls
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AuthSession)) == 0


def test_callback_requires_a_client_secret_before_contacting_github(oauth, monkeypatch):
    config, _ = oauth
    monkeypatch.setattr(github_auth, "Client", lambda **_: pytest.fail("Unexpected GitHub request"))
    with TestClient(
        create_app(Settings("postgresql+psycopg://unused:unused@127.0.0.1:1/unused")),
        base_url="https://refactra.example",
    ) as client:
        _, query, _ = start(client, config)
        response = client.get(
            CALLBACK_PATH,
            params={"code": "fixture-code", "state": query["state"][0]},
            follow_redirects=False,
        )
    assert response.status_code == 503
    assert config.session_cookie_name not in response.cookies


def test_database_failure_does_not_issue_a_session_cookie(callback, session_factory, monkeypatch):
    config, client, _, _ = callback

    def fail_save(*_):
        raise SQLAlchemyError("private database details")

    monkeypatch.setattr(github_auth, "issue_session", fail_save)
    _, query, _ = start(client, config)
    response = client.get(
        CALLBACK_PATH,
        params={"code": "fixture-code", "state": query["state"][0]},
        follow_redirects=False,
    )
    assert response.status_code == 503
    assert config.session_cookie_name not in response.cookies
    assert client.cookies.get(config.cookie_name) is None
    assert "private database details" not in response.text
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AuthSession)) == 0


def test_refresh_exchanges_the_refresh_token_and_reports_rejection(monkeypatch):
    config = GitHubOAuthConfig("Iv1.fixture", "https://refactra.example/cb", "s", "secret")
    calls, answers = (
        [],
        [
            {
                "access_token": "ghu_new",
                "token_type": "bearer",
                "expires_in": 28800,
                "refresh_token": "ghr_new",
                "refresh_token_expires_in": 15897600,
            },
            {"error": "bad_refresh_token"},
        ],
    )

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json=answers.pop(0))

    monkeypatch.setattr(
        github_auth,
        "Client",
        lambda **kwargs: httpx.Client(transport=httpx.MockTransport(upstream), **kwargs),
    )
    renewed = refresh_github_token(config, "ghr_old")
    assert (renewed.access_token, renewed.refresh_token) == ("ghu_new", "ghr_new")
    assert str(calls[0].url) == "https://github.com/login/oauth/access_token"
    assert parse_qs(calls[0].content.decode()) == {
        "client_id": ["Iv1.fixture"],
        "client_secret": ["secret"],
        "grant_type": ["refresh_token"],
        "refresh_token": ["ghr_old"],
    }
    with pytest.raises(TokenRejected):
        refresh_github_token(config, "ghr_new")
