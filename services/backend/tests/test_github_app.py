import json
from datetime import UTC, datetime, timedelta
from time import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from refactra.github_app import GitHubAppClient, GitHubAppError


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def app(private_key, handler):
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return GitHubAppClient("fixture-client", pem, httpx.MockTransport(handler))


def token_response():
    return {
        "token": "ghs_fixture_variable_length_token",
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "permissions": {"contents": "read", "metadata": "read"},
        "repository_selection": "selected",
        "repositories": [{"id": 42}],
    }


def test_signed_request_is_downscoped_and_secrets_are_hidden(private_key):
    payload = token_response()
    before = int(time())

    def handler(request):
        assert request.method == "POST"
        assert str(request.url) == "https://api.github.com/app/installations/7/access_tokens"
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["X-GitHub-Api-Version"] == "2026-03-10"
        scheme, signed = request.headers["Authorization"].split()
        assert scheme == "Bearer"
        claims = jwt.decode(signed, private_key.public_key(), algorithms=["RS256"])
        assert claims["iss"] == "fixture-client"
        assert before - 60 <= claims["iat"] <= int(time()) - 60
        assert before + 540 <= claims["exp"] <= int(time()) + 540
        assert json.loads(request.content) == {
            "repository_ids": [42],
            "permissions": {"contents": "read", "metadata": "read"},
        }
        return httpx.Response(201, json=payload)

    client = app(private_key, handler)
    token = client.mint_read_token(7, 42)
    assert token.value == payload["token"]
    assert token.repository_id == 42
    assert token.expires_at == datetime.fromisoformat(payload["expires_at"])
    assert payload["token"] not in repr(token)
    assert "PRIVATE KEY" not in repr(client)


@pytest.mark.parametrize(
    "problem",
    [
        "write",
        "extra_permission",
        "wrong_repo",
        "extra_repo",
        "expired",
        "long",
        "naive",
        "empty",
        "missing",
        "malformed",
    ],
)
def test_invalid_token_response_is_rejected(private_key, problem):
    payload = token_response()
    if problem == "write":
        payload["permissions"]["contents"] = "write"
    elif problem == "extra_permission":
        payload["permissions"]["issues"] = "read"
    elif problem == "wrong_repo":
        payload["repositories"] = [{"id": 99}]
    elif problem == "extra_repo":
        payload["repositories"].append({"id": 99})
    elif problem in {"expired", "long", "naive"}:
        delta = timedelta(seconds=-1) if problem == "expired" else timedelta(hours=2)
        expiry = datetime.now(UTC) + delta
        payload["expires_at"] = (
            expiry.replace(tzinfo=None) if problem == "naive" else expiry
        ).isoformat()
    elif problem == "empty":
        payload["token"] = ""
    elif problem == "missing":
        del payload["repositories"]

    def handler(request):
        return (
            httpx.Response(201, content="not JSON")
            if problem == "malformed"
            else httpx.Response(201, json=payload)
        )

    with pytest.raises(GitHubAppError, match="could not be completed") as error:
        app(private_key, handler).mint_read_token(7, 42)
    assert "ghs_fixture" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("status", [301, 401, 403, 404, 422, 500])
def test_upstream_errors_are_sanitized_and_redirects_are_not_followed(private_key, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://elsewhere.example"},
            text="sensitive upstream response",
        )

    with pytest.raises(GitHubAppError) as error:
        app(private_key, handler).mint_read_token(7, 42)
    assert len(requests) == 1
    assert "sensitive" not in str(error.value)


def test_transport_failure_is_sanitized(private_key):
    def handler(request):
        raise httpx.ReadTimeout("sensitive transport details", request=request)

    with pytest.raises(GitHubAppError, match="could not be completed"):
        app(private_key, handler).mint_read_token(7, 42)


@pytest.mark.parametrize("installation,repository", [(0, 42), (7, -1), (True, 42), (7, "42")])
def test_invalid_ids_fail_before_a_request(private_key, installation, repository):
    def handler(request):
        pytest.fail("Invalid IDs must not reach GitHub")

    with pytest.raises(ValueError, match="Positive"):
        app(private_key, handler).mint_read_token(installation, repository)


@pytest.mark.parametrize("problem", ["invalid", "public", "ec", "missing_client"])
def test_invalid_credentials_fail_before_a_request(private_key, problem):
    def handler(request):
        pytest.fail("Invalid credentials must not reach GitHub")

    client_id, pem = "fixture-client", b"invalid-private-key"
    if problem == "public":
        pem = private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    elif problem == "ec":
        pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    elif problem == "missing_client":
        client_id = ""
    client = GitHubAppClient(client_id, pem, httpx.MockTransport(handler))
    with pytest.raises(GitHubAppError) as error:
        client.mint_read_token(7, 42)
    assert "invalid-private-key" not in str(error.value)
