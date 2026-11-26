from datetime import UTC, datetime, timedelta

import httpx
import pytest

from refactra.github_app import (
    AppNotInstalled,
    GitHubAppClient,
    GitHubAppError,
    InstallationToken,
)
from refactra.github_repository_check import check_repository, main


@pytest.fixture
def token(monkeypatch):
    token = InstallationToken("fixture-secret-token", 42, datetime.now(UTC) + timedelta(hours=1))

    def mint(self, installation_id, repository_id):
        assert (installation_id, repository_id) == (7, 42)
        return token

    def installation(self, repository_id):
        assert repository_id == 42
        return 7

    monkeypatch.setattr(GitHubAppClient, "mint_read_token", mint)
    monkeypatch.setattr(GitHubAppClient, "repository_installation", installation)
    return token


def test_check_reads_the_selected_repository_without_exposing_credentials(token):
    def handler(request):
        assert request.method == "GET"
        assert str(request.url) == "https://api.github.com/repositories/42"
        assert request.headers["Authorization"] == f"Bearer {token.value}"
        assert request.headers["X-GitHub-Api-Version"] == "2026-03-10"
        return httpx.Response(200, json={"id": 42, "full_name": "fixture/example"})

    result = check_repository("client", b"private-key", 42, transport=httpx.MockTransport(handler))
    assert result == {
        "repository": "fixture/example",
        "repository_id": 42,
        "installation_id": 7,
        "access": "read-only",
        "expires_at": token.expires_at.isoformat(),
    }
    assert token.value not in str(result)
    assert "private-key" not in str(result)


@pytest.mark.parametrize(
    "problem", ["redirect", "forbidden", "wrong_id", "missing", "malformed", "timeout"]
)
def test_failed_reads_are_sanitized_without_following_redirects(token, problem):
    requests = []

    def handler(request):
        requests.append(request)
        if problem == "timeout":
            raise httpx.ReadTimeout("sensitive transport details", request=request)
        if problem == "redirect":
            return httpx.Response(302, headers={"Location": "https://elsewhere.example"})
        if problem == "forbidden":
            return httpx.Response(403, text="sensitive upstream response")
        if problem == "malformed":
            return httpx.Response(200, text="sensitive invalid JSON")
        payload = {"id": 99 if problem == "wrong_id" else 42, "full_name": "fixture/example"}
        if problem == "missing":
            del payload["full_name"]
        return httpx.Response(200, json=payload)

    with pytest.raises(GitHubAppError) as error:
        check_repository("client", b"key", 42, transport=httpx.MockTransport(handler))
    assert len(requests) == 1
    assert "sensitive" not in str(error.value)
    assert token.value not in str(error.value)


def test_missing_configuration_exits_cleanly(monkeypatch, capsys):
    monkeypatch.delenv("REFACTRA_GITHUB_CLIENT_ID", raising=False)
    assert main(["42"]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "Repository check failed" in output.err


def test_cli_requires_exactly_one_repository_id(capsys):
    assert main([]) == 2
    assert main(["42", "43"]) == 2
    assert "Usage:" in capsys.readouterr().err


def test_cli_reads_local_key_and_prints_only_the_summary(monkeypatch, tmp_path, capsys):
    key = tmp_path / "key.pem"
    key.write_bytes(b"fixture-private-key")
    monkeypatch.setenv("REFACTRA_GITHUB_CLIENT_ID", "client")
    monkeypatch.setenv("REFACTRA_GITHUB_PRIVATE_KEY_PATH", str(key))

    def check(client_id, private_key, repository_id):
        assert (client_id, private_key, repository_id) == ("client", b"fixture-private-key", 43)
        return {"repository_id": 43, "access": "read-only"}

    monkeypatch.setattr("refactra.github_repository_check.check_repository", check)
    assert main(["43"]) == 0
    output = capsys.readouterr()
    assert '"access": "read-only"' in output.out
    assert "fixture-private-key" not in output.out
    assert not output.err


def test_cli_explains_when_the_app_is_not_installed(monkeypatch, tmp_path, capsys):
    key = tmp_path / "key.pem"
    key.write_bytes(b"fixture-private-key")
    monkeypatch.setenv("REFACTRA_GITHUB_CLIENT_ID", "client")
    monkeypatch.setenv("REFACTRA_GITHUB_PRIVATE_KEY_PATH", str(key))

    def check(*_):
        raise AppNotInstalled("not installed")

    monkeypatch.setattr("refactra.github_repository_check.check_repository", check)
    assert main(["42"]) == 1
    assert "not installed on that repository" in capsys.readouterr().err
