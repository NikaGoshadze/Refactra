import time
from base64 import urlsafe_b64encode
from hashlib import sha256
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from refactra.api import create_app
from refactra.config import Settings
from refactra.github_auth import CALLBACK_PATH, GitHubOAuthConfig, validate_oauth_attempt


@pytest.fixture
def oauth(monkeypatch):
    config = GitHubOAuthConfig(
        "fixture-client", f"https://refactra.example{CALLBACK_PATH}", "x" * 32
    )
    monkeypatch.setenv("REFACTRA_GITHUB_CLIENT_ID", config.client_id)
    monkeypatch.setenv("REFACTRA_GITHUB_CALLBACK_URL", config.callback_url)
    monkeypatch.setenv("REFACTRA_OAUTH_STATE_SECRET", config.state_secret)
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
