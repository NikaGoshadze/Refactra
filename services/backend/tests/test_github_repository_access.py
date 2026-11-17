from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import HTTPException

from refactra.auth_sessions import GitHubIdentity
from refactra.github_app import GitHubAppClient, GitHubAppError, InstallationToken
from refactra.github_repository_access import require_github_write_access


@pytest.fixture
def token(monkeypatch):
    token = InstallationToken("fixture-secret-token", 42, datetime.now(UTC) + timedelta(hours=1))

    def mint(self, installation_id, repository_id):
        assert (installation_id, repository_id) == (7, 42)
        return token

    monkeypatch.setattr(GitHubAppClient, "mint_read_token", mint)
    return token


def check(handler):
    app = GitHubAppClient("fixture-client", b"fixture-key", httpx.MockTransport(handler))
    return require_github_write_access(app, 7, 42, GitHubIdentity(12345, "old-login"))


def payloads(permission="write", role="write"):
    return {
        "/repositories/42": {"id": 42, "full_name": "fixture/example"},
        "/user/12345": {"id": 12345, "login": "current-login"},
        "/repos/fixture/example/collaborators/current-login/permission": {
            "permission": permission,
            "role_name": role,
            "user": {"id": 12345},
        },
    }


@pytest.mark.parametrize(
    "permission,role",
    [("write", "write"), ("write", "maintain"), ("admin", "admin"), ("write", "custom-role")],
)
def test_current_base_permission_is_checked_using_durable_identity(token, permission, role):
    responses = payloads(permission, role)
    requests = []

    def handler(request):
        requests.append(request.url.path)
        assert request.method == "GET" and request.url.host == "api.github.com"
        assert request.headers["Authorization"] == f"Bearer {token.value}"
        assert request.headers["X-GitHub-Api-Version"] == "2026-03-10"
        return httpx.Response(200, json=responses[request.url.path])

    assert check(handler) is token
    assert requests == list(responses)


@pytest.mark.parametrize("permission", ["read", "none", "unexpected"])
def test_non_writers_are_denied_even_if_the_role_name_says_admin(token, permission):
    responses = payloads(permission, "admin")
    with pytest.raises(HTTPException) as error:
        check(lambda request: httpx.Response(200, json=responses[request.url.path]))
    assert error.value.status_code == 403
    assert error.value.detail["code"] == "repository_access_denied"
    assert token.value not in str(error.value)


@pytest.mark.parametrize("path", list(payloads()))
def test_missing_repository_user_or_permission_is_denied(token, path):
    responses = payloads()

    def handler(request):
        return (
            httpx.Response(404)
            if request.url.path == path
            else httpx.Response(200, json=responses[request.url.path])
        )

    with pytest.raises(HTTPException) as error:
        check(handler)
    assert error.value.status_code == 403


@pytest.mark.parametrize(
    "problem", ["wrong_repo", "wrong_user", "wrong_subject", "missing", "malformed"]
)
def test_unverifiable_identity_or_response_is_sanitized(token, problem):
    responses = payloads()
    if problem == "wrong_repo":
        responses["/repositories/42"]["id"] = 99
    elif problem == "wrong_user":
        responses["/user/12345"]["id"] = 99999
    elif problem == "wrong_subject":
        responses[list(responses)[-1]]["user"]["id"] = 99999
    elif problem == "missing":
        del responses[list(responses)[-1]]["permission"]

    def handler(request):
        return (
            httpx.Response(200, text="sensitive invalid JSON")
            if problem == "malformed"
            else httpx.Response(200, json=responses[request.url.path])
        )

    with pytest.raises(GitHubAppError) as error:
        check(handler)
    assert "sensitive" not in str(error.value)
    assert token.value not in str(error.value)


@pytest.mark.parametrize("problem", [302, 401, 403, 500, "timeout"])
def test_upstream_failures_do_not_grant_access_or_follow_redirects(token, problem):
    requests = []

    def handler(request):
        requests.append(request)
        if problem == "timeout":
            raise httpx.ReadTimeout("sensitive transport details", request=request)
        return httpx.Response(
            problem,
            headers={"Location": "https://elsewhere.example"},
            text="sensitive upstream body",
        )

    with pytest.raises(GitHubAppError) as error:
        check(handler)
    assert len(requests) == 1
    assert "sensitive" not in str(error.value)
    assert token.value not in str(error.value)


def test_permissions_are_rechecked_after_access_changes(token):
    responses = payloads()

    def handler(request):
        return httpx.Response(200, json=responses[request.url.path])

    app = GitHubAppClient("fixture-client", b"fixture-key", httpx.MockTransport(handler))
    identity = GitHubIdentity(12345, "old-login")
    assert require_github_write_access(app, 7, 42, identity) is token
    responses[list(responses)[-1]]["permission"] = "read"
    with pytest.raises(HTTPException) as error:
        require_github_write_access(app, 7, 42, identity)
    assert error.value.status_code == 403
