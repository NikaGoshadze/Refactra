from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from refactra.github_app import GitHubAppError, InstallationToken
from refactra.repository_revision import resolve_default_revision

SHA = "a" * 40


@pytest.fixture
def token():
    return InstallationToken("fixture-secret-token", 42, datetime.now(UTC) + timedelta(hours=1))


def payloads(branch="main", sha=SHA):
    return (
        {"id": 42, "full_name": "fixture/example", "default_branch": branch},
        {"ref": f"refs/heads/{branch}", "object": {"type": "commit", "sha": sha}},
    )


@pytest.mark.parametrize("branch", ["main", "release/v1"])
def test_resolution_selects_the_exact_branch_and_encodes_its_name(token, branch):
    repository, reference = payloads(branch)
    paths = []

    def handler(request):
        paths.append(request.url.raw_path.decode())
        assert request.method == "GET" and request.url.host == "api.github.com"
        assert request.headers["Authorization"] == f"Bearer {token.value}"
        assert request.headers["X-GitHub-Api-Version"] == "2026-03-10"
        return httpx.Response(200, json=repository if len(paths) == 1 else reference)

    revision = resolve_default_revision(token, transport=httpx.MockTransport(handler))
    assert revision.repository_id == 42 and revision.full_name == "fixture/example"
    assert revision.default_branch == branch and revision.commit_sha == SHA
    assert paths == [
        "/repositories/42",
        f"/repos/fixture/example/git/ref/heads%2F{branch.replace('/', '%2F')}",
    ]
    assert token.value not in repr(revision)


def test_an_existing_revision_stays_fixed_after_the_branch_moves(token):
    repository, reference = payloads()

    def handler(request):
        return httpx.Response(
            200, json=repository if request.url.path == "/repositories/42" else reference
        )

    transport = httpx.MockTransport(handler)
    original = resolve_default_revision(token, transport=transport)
    reference["object"]["sha"] = "b" * 40
    assert resolve_default_revision(token, transport=transport).commit_sha == "b" * 40
    assert original.commit_sha == SHA
    with pytest.raises(FrozenInstanceError):
        original.commit_sha = "b" * 40


@pytest.mark.parametrize(
    "problem",
    [
        "wrong_repo",
        "bad_name",
        "empty_branch",
        "tag",
        "wrong_ref",
        "short_sha",
        "bad_sha",
        "missing",
        "malformed",
    ],
)
def test_mismatched_or_malformed_results_fail_closed(token, problem):
    repository, reference = payloads()
    if problem == "wrong_repo":
        repository["id"] = 99
    elif problem == "bad_name":
        repository["full_name"] = "https://elsewhere.example/repo"
    elif problem == "empty_branch":
        repository["default_branch"] = ""
    elif problem == "tag":
        reference["object"]["type"] = "tag"
    elif problem == "wrong_ref":
        reference["ref"] = "refs/heads/other"
    elif problem == "short_sha":
        reference["object"]["sha"] = "a" * 7
    elif problem == "bad_sha":
        reference["object"]["sha"] = "z" * 40
    elif problem == "missing":
        del reference["object"]

    def handler(request):
        if problem == "malformed":
            return httpx.Response(200, text="sensitive invalid JSON")
        return httpx.Response(
            200, json=repository if request.url.path == "/repositories/42" else reference
        )

    with pytest.raises(GitHubAppError) as error:
        resolve_default_revision(token, transport=httpx.MockTransport(handler))
    assert token.value not in str(error.value) and "sensitive" not in str(error.value)


@pytest.mark.parametrize("status", [302, 403, 404, 409, 500, "timeout"])
def test_failures_do_not_follow_redirects_or_fall_back_to_another_ref(token, status):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if len(paths) == 1:
            return httpx.Response(200, json=payloads()[0])
        if status == "timeout":
            raise httpx.ReadTimeout("sensitive details", request=request)
        return httpx.Response(
            status, headers={"Location": "https://elsewhere.example"}, text="sensitive body"
        )

    with pytest.raises(GitHubAppError) as error:
        resolve_default_revision(token, transport=httpx.MockTransport(handler))
    assert len(paths) == 2
    assert token.value not in str(error.value) and "sensitive" not in str(error.value)


def test_expired_token_is_rejected_before_any_request():
    token = InstallationToken("fixture-secret-token", 42, datetime.now(UTC) - timedelta(seconds=1))

    def handler(request):
        pytest.fail("Expired token must not reach GitHub")

    with pytest.raises(GitHubAppError):
        resolve_default_revision(token, transport=httpx.MockTransport(handler))
