from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import refactra.repository_routes as routes
from refactra.api import create_app
from refactra.auth_sessions import GitHubIdentity, issue_session, revoke_session
from refactra.config import Settings
from refactra.github_app import GitHubAppClient, InstallationToken
from refactra.models import Run, RunAnalysisContext, RunCommand, RunEvent
from refactra.openai_analysis import OpenAIAnalysisProvider
from refactra.refactoring_plan import save_refactoring_plan
from refactra.repository_analysis_worker import WORKFLOW, process_repository_analysis
from refactra.worker import claim_command

from .test_openai_analysis import response as model_response
from .test_refactoring_plan import analyze_fixture_run
from .test_refactoring_plan import proposal as proposal
from .test_repository_snapshot import zip_bytes

ARCHIVE = zip_bytes(
    [("example/src/cart.py", b"def cart(): return 1"), ("example/.env", b"PASSWORD=private")]
)
PIN = "a" * 40
ARCHIVE_PATH = f"/repositories/42/zipball/{PIN}"


@pytest.fixture
def browser(database, session_factory, monkeypatch, tmp_path):
    key = tmp_path / "app.pem"
    key.write_bytes(b"fixture-private-key")
    for name, value in {
        "REFACTRA_GITHUB_CALLBACK_URL": "https://refactra.example/api/v1/auth/github/callback",
        "REFACTRA_ALLOWED_GITHUB_USER_IDS": "12345",
        "REFACTRA_GITHUB_REPOSITORY_ID": "42",
        "REFACTRA_GITHUB_INSTALLATION_ID": "7",
        "REFACTRA_GITHUB_CLIENT_ID": "fixture-client",
        "REFACTRA_GITHUB_PRIVATE_KEY_PATH": str(key),
    }.items():
        monkeypatch.setenv(name, value)
    calls, mode = [], {}

    def handler(request):
        calls.append(request.url.path)
        assert request.method == "GET"
        if request.url.host == "codeload.github.com":
            assert "authorization" not in request.headers
            if mode.get("source_failure"):
                return httpx.Response(503)
            return httpx.Response(200, stream=httpx.ByteStream(ARCHIVE))
        assert request.headers["Authorization"] == "Bearer fixture-secret-token"
        if mode.get("upstream_failure"):
            return httpx.Response(503, text="sensitive upstream body")
        if request.url.path == "/repositories/42":
            return httpx.Response(
                200,
                json={
                    "id": 99
                    if mode.get("wrong_final_id") and calls.count("/repositories/42") == 2
                    else 42,
                    "full_name": "fixture/example",
                    "default_branch": "main",
                    "token": "fixture-secret-token",
                    "private_key": "fixture-private-key",
                },
            )
        if request.url.path == "/user/12345":
            return httpx.Response(200, json={"id": 12345, "login": "current-login"})
        if request.url.path == "/repos/fixture/example/git/ref/heads/feature/cart":
            return httpx.Response(
                200,
                json={
                    "ref": "refs/heads/feature/cart",
                    "object": {"type": "commit", "sha": mode.get("branch_tip", PIN)},
                },
            )
        if request.url.path == ARCHIVE_PATH:
            with session_factory() as session:
                assert session.scalar(select(Run.base_sha)) == PIN
                assert session.scalar(select(func.count()).select_from(RunCommand)) == 0
            return httpx.Response(
                302, headers={"Location": f"https://codeload.github.com/fixture/example/zip/{PIN}"}
            )
        assert request.url.path == "/repos/fixture/example/collaborators/current-login/permission"
        return httpx.Response(
            200,
            json={
                "user": {"id": 12345},
                "permission": "read" if mode.get("non_writer") else "write",
            },
        )

    def app(client_id, private_key):
        assert (client_id, private_key) == ("fixture-client", b"fixture-private-key")
        return GitHubAppClient(client_id, private_key, httpx.MockTransport(handler))

    def mint(self, installation_id, repository_id):
        assert (installation_id, repository_id) == (7, 42)
        return InstallationToken("fixture-secret-token", 42, datetime.now(UTC) + timedelta(hours=1))

    monkeypatch.setattr(routes, "GitHubAppClient", app)
    monkeypatch.setattr(GitHubAppClient, "mint_read_token", mint)
    with session_factory.begin() as session:
        token = issue_session(session, GitHubIdentity(12345, "old-login"))
    with TestClient(
        create_app(Settings("unused", enable_repository_analysis=True), database),
        base_url="https://refactra.example",
    ) as client:
        client.cookies.set("__Host-refactra_session", token, domain="refactra.example", path="/")
        yield client, calls, mode, token


def test_authorized_endpoint_returns_only_safe_metadata(browser):
    client, calls, _, _ = browser
    response = client.get("/api/v1/repositories/42")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "repository_id": 42,
        "full_name": "fixture/example",
        "default_branch": "main",
        "access": "read-only",
    }
    assert calls == [
        "/repositories/42",
        "/user/12345",
        "/repos/fixture/example/collaborators/current-login/permission",
        "/repositories/42",
    ]
    assert "fixture-secret" not in response.text and "fixture-private" not in response.text


@pytest.mark.parametrize(
    "problem,status",
    [("anonymous", 401), ("revoked", 401), ("uninvited", 403), ("wrong_repo", 403)],
)
def test_local_rejections_precede_key_loading_and_github(
    browser, session_factory, monkeypatch, problem, status
):
    client, calls, _, token = browser
    monkeypatch.setenv("REFACTRA_GITHUB_PRIVATE_KEY_PATH", "missing-private-key.pem")
    if problem == "anonymous":
        client.cookies.clear()
    elif problem == "revoked":
        with session_factory.begin() as session:
            revoke_session(session, token)
    elif problem == "uninvited":
        monkeypatch.setenv("REFACTRA_ALLOWED_GITHUB_USER_IDS", "99999")
    response = client.get(f"/api/v1/repositories/{43 if problem == 'wrong_repo' else 42}")
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert calls == []


@pytest.mark.parametrize(
    "setting,value",
    [
        ("REFACTRA_GITHUB_PRIVATE_KEY_PATH", "missing.pem"),
        ("REFACTRA_GITHUB_INSTALLATION_ID", "bad"),
        ("REFACTRA_GITHUB_CLIENT_ID", ""),
        ("REFACTRA_ALLOWED_GITHUB_USER_IDS", "*"),
    ],
)
def test_invalid_runtime_configuration_fails_closed(browser, monkeypatch, setting, value):
    client, calls, _, _ = browser
    monkeypatch.setenv(setting, value)
    response = client.get("/api/v1/repositories/42")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "repository_access_unconfigured"
    assert response.headers["cache-control"] == "no-store"
    assert calls == []


@pytest.mark.parametrize(
    "mode,status", [("non_writer", 403), ("upstream_failure", 502), ("wrong_final_id", 502)]
)
def test_github_denials_and_failures_do_not_expose_metadata_or_credentials(browser, mode, status):
    client, _, switches, _ = browser
    switches[mode] = True
    response = client.get("/api/v1/repositories/42")
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert "repository_id" not in response.json()
    assert all(
        secret not in response.text for secret in ("fixture-secret", "fixture-private", "sensitive")
    )


def submit(client, key=None, *, origin="https://refactra.example", **overrides):
    return client.post(
        "/api/v1/runs",
        json={"repository_id": 42, "base_branch": "feature/cart", "task_text": "Review cart"}
        | overrides,
        headers={"Origin": origin, "X-Refactra-CSRF": "1", "Idempotency-Key": str(key or uuid4())},
    )


def test_submission_retries_preserve_pin_and_queue_exactly_once(browser, session_factory):
    client, calls, switches, _ = browser
    key = uuid4()
    switches["source_failure"] = True
    assert submit(client, key).status_code == 502
    with session_factory() as session:
        pending = session.scalar(select(Run))
        assert pending.base_sha == PIN
        assert session.get(RunAnalysisContext, pending.id) is None
        assert session.scalar(select(func.count()).select_from(RunCommand)) == 0
    switches.update(source_failure=False, branch_tip="b" * 40)
    accepted = submit(client, key)
    assert accepted.status_code == 202
    assert accepted.headers["cache-control"] == "no-store"
    receipt = accepted.json()
    assert receipt["base_sha"] == PIN and receipt["run_id"] == str(pending.id)
    assert submit(client, key).json() == receipt
    assert submit(client, key, task_text="Another task").status_code == 409
    assert calls.count(ARCHIVE_PATH) == 2
    assert sum("/git/ref/" in path for path in calls) == 1
    with session_factory() as session:
        run_id = UUID(receipt["run_id"])
        context = session.get(RunAnalysisContext, run_id)
        assert context.payload["base_sha"] == PIN
        assert [f["path"] for f in context.payload["content"]["files"]] == ["src/cart.py"]
        assert session.scalars(select(RunCommand.run_id)).all() == [run_id]


@pytest.mark.parametrize("problem,status", [("csrf", 403), ("sign_in", 401), ("non_writer", 403)])
def test_denied_submission_does_not_pin_or_queue(browser, session_factory, problem, status):
    client, calls, switches, _ = browser
    if problem == "sign_in":
        client.cookies.clear()
    switches["non_writer"] = problem == "non_writer"
    origin = "https://foreign.example" if problem == "csrf" else "https://refactra.example"
    assert submit(client, origin=origin).status_code == status
    if problem != "non_writer":
        assert calls == []
    with session_factory() as session:
        assert session.scalar(select(Run)) is None


def test_run_status_returns_the_saved_worker_result(browser, session_factory):
    client, _, _, _ = browser
    accepted = submit(client)
    receipt = accepted.json()
    url = accepted.headers["Location"]
    assert url == f"/api/v1/runs/{receipt['run_id']}"
    queued = client.get(url).json()
    assert {field: queued[field] for field in receipt} == receipt
    assert queued["status"] == "CREATED"
    assert queued["analysis_text"] is queued["stop_reason"] is None

    def model(request):
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 100}
            )
        analyzing = client.get(url).json()
        assert analyzing["status"] == "ANALYZING" and analyzing["analysis_text"] is None
        return httpx.Response(200, json=model_response())

    provider = OpenAIAnalysisProvider("fixture", httpx.MockTransport(model))
    command = claim_command(session_factory, "fixture-worker", workflow=WORKFLOW)
    assert process_repository_analysis(session_factory, command, "fixture-worker", provider)
    result = client.get(url)
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    body = result.json()
    assert body["status"] == "NEEDS_HUMAN_REVIEW"
    assert body["analysis_text"] == "src/cart.py:1 needs review" and body["stop_reason"] is None
    assert set(body) == set(receipt) | {"analysis_text", "status", "stop_reason", "updated_at"}


def test_read_saved_plan_revisions(browser, session_factory, proposal):
    client, _, _, _ = browser
    receipt = submit(client)
    url = f"{receipt.headers['Location']}/plan"
    run_id = UUID(receipt.json()["run_id"])
    missing = client.get(url)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "plan_not_found"
    assert missing.headers["cache-control"] == "no-store"

    analyze_fixture_run(session_factory)
    proposal["blocking_questions"] = ["Should empty carts still return zero?"]
    check = proposal["validation_checks"][0]
    proposal["validation_checks"] = [dict(check, check_id="VC1")]
    revised = dict(proposal, blocking_questions=[], validation_checks=[check])
    with session_factory.begin() as session:
        first = save_refactoring_plan(session, run_id, proposal, expected_revision=0)
        second = save_refactoring_plan(session, run_id, revised, expected_revision=1)

    for path, plan, expected in [(url, second, revised), (f"{url}?revision=1", first, proposal)]:
        result = client.get(path)
        assert result.status_code == 200
        assert result.headers["cache-control"] == "no-store"
        payload = result.json()
        assert payload["artifact"] == {
            "id": str(plan.id),
            "run_id": str(run_id),
            "kind": "PLAN",
            "revision": plan.revision,
            "sha256": plan.sha256,
            "size": len(plan.document),
            "created_at": plan.created_at.isoformat().replace("+00:00", "Z"),
        }
        assert payload["content"] == {
            **expected,
            "schema_version": "1",
            "run_id": str(run_id),
            "repository": {"repository_id": 42, "base_branch": "feature/cart", "base_sha": PIN},
        }
        assert payload["readiness"] == {
            "blocking_question_count": 1 if plan.revision == 1 else 0,
            "unsupported_check_ids": ["VC1"] if plan.revision == 1 else [],
            "has_required_supported_check": plan.revision == 2,
        }
    assert client.get(f"{url}?revision=3").status_code == 404
    assert client.get(f"{url}?revision=0").status_code == 422


@pytest.mark.parametrize("suffix", ["", "/plan"])
@pytest.mark.parametrize(
    "problem,expected",
    [
        ("anonymous", 401),
        ("other_user", 404),
        ("uninvited", 404),
        ("installation_changed", 404),
        ("non_writer", 404),
        ("upstream_failure", 502),
    ],
)
def test_run_reads_recheck_access(browser, session_factory, monkeypatch, suffix, problem, expected):
    client, calls, switches, _ = browser
    url = submit(client).headers["Location"]
    calls.clear()
    if problem == "anonymous":
        client.cookies.clear()
    elif problem == "other_user":
        monkeypatch.setenv("REFACTRA_ALLOWED_GITHUB_USER_IDS", "12345,56789")
        with session_factory.begin() as session:
            token = issue_session(session, GitHubIdentity(56789, "other-user"))
        client.cookies.set("__Host-refactra_session", token, domain="refactra.example", path="/")
    elif problem == "uninvited":
        monkeypatch.setenv("REFACTRA_ALLOWED_GITHUB_USER_IDS", "56789")
    elif problem == "installation_changed":
        monkeypatch.setenv("REFACTRA_GITHUB_INSTALLATION_ID", "8")
    else:
        switches[problem] = True
    result = client.get(url + suffix)
    assert result.status_code == expected
    assert result.headers["cache-control"] == "no-store"
    assert set(result.json()) == {"error"}
    if expected == 404:
        assert result.json()["error"]["code"] == "not_found"
    assert "sensitive upstream body" not in result.text
    assert bool(calls) == (problem in {"non_writer", "upstream_failure"})


@pytest.mark.parametrize(
    "stored_code,public_code",
    [("analysis_outcome_unknown", "analysis_outcome_unknown"), ("secret-code", "analysis_failed")],
)
def test_failed_run_returns_only_safe_failure_reason(
    browser, session_factory, stored_code, public_code
):
    client, _, _, _ = browser
    accepted = submit(client)
    run_id = UUID(accepted.json()["run_id"])
    with session_factory.begin() as session:
        session.get(Run, run_id).status = "FAILED"
        session.add(
            RunEvent(
                run_id=run_id,
                event_type="RUN_STOPPED",
                payload={"code": stored_code, "message": "secret upstream message"},
            )
        )
    result = client.get(accepted.headers["Location"])
    assert result.status_code == 200 and "secret" not in result.text
    assert result.json()["analysis_text"] is None
    assert result.json()["stop_reason"]["code"] == public_code
