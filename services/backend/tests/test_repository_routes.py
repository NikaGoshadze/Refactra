import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import refactra.repository_plan_worker as plan_worker
from refactra.api import create_app
from refactra.auth_sessions import GitHubIdentity, issue_session, revoke_session
from refactra.candidate_patch import CandidatePatch
from refactra.candidate_results import save_validated_candidate
from refactra.config import Settings
from refactra.models import Run, RunAnalysisContext, RunCommand, RunEvent
from refactra.openai_analysis import OpenAIAnalysisProvider
from refactra.publication_target import (
    PUBLICATION_APPROVED,
    PUBLICATION_BRANCH_PUSHED,
    PUBLICATION_PR_OPENED,
    PUBLISH_CANDIDATE,
    publication_command_key,
    save_publication_target,
)
from refactra.refactoring_plan import GENERATE_PLAN, approve_refactoring_plan, save_refactoring_plan
from refactra.repository_analysis_worker import WORKFLOW, process_repository_analysis
from refactra.repository_candidate import VALIDATE_CANDIDATE
from refactra.repository_validation import ValidationResult
from refactra.run_access import RepositoryAuthorizer, authorize_run_repository
from refactra.snapshot_preparation import PREPARE_SNAPSHOT, process_snapshot_preparation
from refactra.worker import claim_command

from .test_candidate_results import passing_patch
from .test_openai_analysis import response as model_response
from .test_refactoring_plan import analyze_fixture_run
from .test_refactoring_plan import proposal as proposal
from .test_repository_snapshot import zip_bytes

ARCHIVE = zip_bytes(
    [("example/src/cart.py", b"def cart(): return 1"), ("example/.env", b"PASSWORD=private")]
)
PIN = "a" * 40
APP_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)
ARCHIVE_PATH = f"/repositories/42/zipball/{PIN}"
NO_QUESTIONS = (
    "None. The provided inventory is sufficient to propose the refactor "
    "without additional scope clarification."
)


@pytest.fixture
def browser(database, session_factory, monkeypatch, tmp_path):
    key = tmp_path / "app.pem"
    key.write_bytes(APP_KEY)
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
        if request.method == "POST" and request.url.path == "/app/installations/7/access_tokens":
            # GitHub's installation token endpoint; kept out of `calls` to count API reads only.
            body = json.loads(request.content)
            assert body["repository_ids"] == [42]
            return httpx.Response(
                201,
                json={
                    "token": "fixture-secret-token",
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=59)).isoformat(),
                    "permissions": body["permissions"],
                    "repositories": [{"id": 42}],
                },
            )
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
                # Analysis is queued only after the worker saves the pinned context.
                started = select(RunCommand.id).where(RunCommand.kind == "START")
                assert session.scalar(started) is None
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

    # Inject a simulated GitHub instead of patching module attributes.
    authorizer = RepositoryAuthorizer(transport=httpx.MockTransport(handler))
    with session_factory.begin() as session:
        token = issue_session(session, GitHubIdentity(12345, "old-login"))
    with TestClient(
        create_app(
            Settings("unused", enable_repository_analysis=True),
            database,
            repository_authorizer=authorizer,
        ),
        base_url="https://refactra.example",
    ) as client:
        client.cookies.set("__Host-refactra_session", token, domain="refactra.example", path="/")
        client.prepare_snapshots = lambda: prepare_snapshots(session_factory, authorizer)
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


def test_configured_repository_is_returned_only_to_eligible_users(browser, monkeypatch):
    client, calls, _, _ = browser
    by_id = client.get("/api/v1/repositories/42").json()
    configured = client.get("/api/v1/repository")
    assert configured.status_code == 200 and configured.json() == by_id
    assert configured.headers["cache-control"] == "no-store"
    client.cookies.clear()
    calls.clear()
    anonymous = client.get("/api/v1/repository")
    assert anonymous.status_code == 401 and "42" not in anonymous.text and calls == []
    monkeypatch.delenv("REFACTRA_GITHUB_REPOSITORY_ID")
    missing = client.get("/api/v1/repository")
    assert missing.status_code == 503
    assert missing.json()["error"]["code"] == "repository_access_unconfigured"


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


def prepare_snapshots(session_factory, authorizer) -> int:
    """Run queued snapshot preparation as the worker would, with real access checks."""
    processed = 0
    while command := claim_command(
        session_factory, "snapshot-worker", workflow=WORKFLOW, kind=PREPARE_SNAPSHOT
    ):
        assert process_snapshot_preparation(
            session_factory,
            command,
            "snapshot-worker",
            authorize=lambda run: authorize_run_repository(run, authorizer),
        )
        processed += 1
    return processed


def submit(client, key=None, *, origin="https://refactra.example", prepare=True, **overrides):
    response = client.post(
        "/api/v1/runs",
        json={"repository_id": 42, "base_branch": "feature/cart", "task_text": "Review cart"}
        | overrides,
        headers={"Origin": origin, "X-Refactra-CSRF": "1", "Idempotency-Key": str(key or uuid4())},
    )
    if prepare and response.status_code == 202:
        client.prepare_snapshots()
    return response


def test_submission_returns_once_pinned_and_the_worker_prepares_source(browser, session_factory):
    client, calls, switches, _ = browser
    key = uuid4()
    switches["source_failure"] = True
    accepted = submit(client, key, prepare=False)
    assert accepted.status_code == 202
    assert accepted.headers["cache-control"] == "no-store"
    receipt = accepted.json()
    assert receipt["base_sha"] == PIN
    assert ARCHIVE_PATH not in calls  # The receipt never waits for the source download.
    run_id = UUID(receipt["run_id"])
    with session_factory() as session:
        assert session.get(RunAnalysisContext, run_id) is None
        assert session.scalars(select(RunCommand.kind)).all() == [PREPARE_SNAPSHOT]

    # A failed download is retried by the worker, not by the browser.
    assert client.prepare_snapshots() == 1
    with session_factory.begin() as session:
        command = session.scalar(select(RunCommand).where(RunCommand.kind == PREPARE_SNAPSHOT))
        assert command.status == "PENDING" and session.get(Run, run_id).status == "CREATED"
        command.available_at = session.scalar(select(func.now()))
    switches.update(source_failure=False, branch_tip="b" * 40)
    assert submit(client, key, prepare=False).json() == receipt
    assert client.prepare_snapshots() == 1
    assert submit(client, key).json() == receipt
    assert submit(client, key, task_text="Another task").status_code == 409
    assert calls.count(ARCHIVE_PATH) == 2
    assert sum("/git/ref/" in path for path in calls) == 1
    with session_factory() as session:
        context = session.get(RunAnalysisContext, run_id)
        assert context.payload["base_sha"] == PIN
        assert [f["path"] for f in context.payload["content"]["files"]] == ["src/cart.py"]
        commands = session.execute(select(RunCommand.kind, RunCommand.status)).all()
        assert sorted(commands) == [(PREPARE_SNAPSHOT, "DONE"), ("START", "PENDING")]


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
    assert queued["stop_reason"] is None and "analysis_text" not in queued

    def model(request):
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 100}
            )
        analyzing = client.get(url).json()
        assert analyzing["status"] == "ANALYZING" and "analysis_text" not in analyzing
        return httpx.Response(200, json=model_response())

    provider = OpenAIAnalysisProvider("fixture", httpx.MockTransport(model))
    command = claim_command(session_factory, "fixture-worker", workflow=WORKFLOW)
    assert process_repository_analysis(
        session_factory, command, "fixture-worker", provider, authorize=lambda _run: None
    )
    result = client.get(url)
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    body = result.json()
    assert body["status"] == "NEEDS_HUMAN_REVIEW"
    assert body["stop_reason"] is None
    assert set(body) == set(receipt) | {"status", "stop_reason", "updated_at"}


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
            "requested_files": {},
            "inventory_errors": [],
        }
        assert payload["readiness"] == {
            "outcome": "needs_clarification" if plan.revision == 1 else "ready",
            "blocking_question_count": 1 if plan.revision == 1 else 0,
            "unsupported_check_ids": ["VC1"] if plan.revision == 1 else [],
            "has_required_supported_check": True,
            "scope_errors": [],
        }
    assert client.get(f"{url}?revision=3").status_code == 404
    assert client.get(f"{url}?revision=0").status_code == 422


@pytest.mark.parametrize(
    "questions, outcome", [([], "unsupported"), (["Which endpoint?"], "needs_clarification")]
)
def test_no_change_outcome_can_be_reviewed_but_cannot_be_approved(
    browser, session_factory, proposal, questions, outcome
):
    client, _, _, _ = browser
    receipt = submit(client)
    url = f"{receipt.headers['Location']}/plan"
    run_id = UUID(receipt.json()["run_id"])
    analyze_fixture_run(session_factory)
    with session_factory.begin() as session:
        plan = save_refactoring_plan(
            session,
            run_id,
            dict(proposal, changes=[], validation_checks=[], blocking_questions=questions),
            expected_revision=0,
        )
    assert client.get(url).json()["readiness"]["outcome"] == outcome
    headers = {"Origin": "https://refactra.example", "X-Refactra-CSRF": "1"}
    approval = {"plan_id": str(plan.id), "revision": 1, "sha256": plan.sha256}
    assert client.post(f"{url}/approve", json=approval, headers=headers).status_code == 409


def test_revising_a_plan_appends_once_and_preserves_previous_revision(
    browser, session_factory, proposal
):
    client, calls, _, _ = browser
    receipt = submit(client)
    url = f"{receipt.headers['Location']}/plan"
    run_id = UUID(receipt.json()["run_id"])
    analyze_fixture_run(session_factory)
    headers = {"Origin": "https://refactra.example", "X-Refactra-CSRF": "1"}
    body = {"expected_revision": 1, "proposal": proposal}

    calls.clear()
    rejected = client.post(url, json=body, headers=headers | {"Origin": "https://other.example"})
    assert rejected.status_code == 403 and calls == []
    assert client.post(url, json=body, headers=headers).status_code == 409

    previous = dict(proposal, blocking_questions=["How should empty carts behave?"])
    with session_factory.begin() as session:
        initial = save_refactoring_plan(session, run_id, previous, expected_revision=0)
    saved = client.post(url, json=body, headers=headers)
    assert saved.status_code == 200 and saved.headers["cache-control"] == "no-store"
    assert saved.json()["artifact"]["revision"] == 2
    assert saved.json()["readiness"]["blocking_question_count"] == 0
    assert client.post(url, json=body, headers=headers).json() == saved.json()

    stale = dict(body, proposal=dict(proposal, summary="A competing plan draft"))
    conflict = client.post(url, json=stale, headers=headers)
    assert (
        conflict.status_code == 409 and conflict.json()["error"]["code"] == "plan_revision_conflict"
    )
    assert client.get(url).json() == saved.json()
    historical = client.get(f"{url}?revision=1").json()
    assert historical["artifact"]["sha256"] == initial.sha256
    assert historical["content"]["blocking_questions"] == previous["blocking_questions"]


@pytest.mark.parametrize("old_feedback", [False, True])
def test_plan_feedback_is_queued_once_and_revises_the_saved_plan(
    browser, session_factory, proposal, monkeypatch, old_feedback
):
    client, _, _, _ = browser
    receipt = submit(client)
    url = f"{receipt.headers['Location']}/plan"
    run_id = UUID(receipt.json()["run_id"])
    analyze_fixture_run(session_factory)
    question = "How should empty carts behave?"
    with session_factory.begin() as session:
        previous = dict(
            proposal,
            blocking_questions=[question],
            changes=[
                *proposal["changes"],
                {"path": ".env", "action": "MODIFY", "intent": "Change settings."},
            ],
        )
        plan = save_refactoring_plan(session, run_id, previous, expected_revision=0)
        stored = json.loads(plan.document) | {"blocking_questions": [NO_QUESTIONS, question]}
        plan.document = json.dumps(stored).encode()
        plan.sha256 = sha256(plan.document).hexdigest()

    headers = {"Origin": "https://refactra.example", "X-Refactra-CSRF": "1"}
    feedback = {
        "expected_revision": 1,
        "notes": "Keep the change small.",
        "answers": ["Return zero."],
    }
    for _ in range(2):
        pending = client.post(url, json=feedback, headers=headers)
        assert pending.status_code == 200 and pending.json()["feedback_status"] == "pending"
    assert client.get(url).json()["feedback_status"] == "pending"
    if old_feedback:
        with session_factory.begin() as session:
            event = session.scalar(
                select(RunEvent).where(
                    RunEvent.run_id == run_id, RunEvent.event_type == "PLAN_FEEDBACK_REQUESTED"
                )
            )
            event.payload = event.payload | {"answers": ["", "Return zero."]}
    approval = {"plan_id": str(plan.id), "revision": 1, "sha256": plan.sha256}
    assert client.post(f"{url}/approve", json=approval, headers=headers).status_code == 409
    edit = {"expected_revision": 1, "proposal": proposal}
    assert client.post(url, json=edit, headers=headers).status_code == 409

    def model(request):
        context = json.loads(json.loads(request.content)["input"])
        assert context["previous_plan"]["blocking_questions"] == [question]
        assert context["previous_plan_readiness"]["scope_errors"] == [
            "MODIFY is unsupported for .env."
        ]
        assert context["reviewer_feedback"]["questions_and_answers"] == [
            {"question": question, "answer": "Return zero."}
        ]
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 100}
            )
        payload = model_response()
        payload["output"][0]["content"][0]["text"] = json.dumps(proposal)
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(plan_worker, "authorize_plan", lambda _: None)
    provider = OpenAIAnalysisProvider("fixture", httpx.MockTransport(model))
    job = claim_command(session_factory, "fixture", workflow=WORKFLOW, kind=GENERATE_PLAN)
    assert plan_worker.process_initial_plan(session_factory, job, "fixture", provider)
    assert claim_command(session_factory, "fixture", workflow=WORKFLOW, kind=GENERATE_PLAN) is None
    updated = client.get(url).json()
    assert updated["artifact"]["revision"] == 2 and updated["feedback_status"] is None
    assert updated["content"]["blocking_questions"] == []
    assert client.get(f"{url}?revision=1").json()["content"]["blocking_questions"] == [question]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("questions", [[], ["Which output format should be used?"]])
def test_placeholder_questions_do_not_block_the_saved_revision(
    browser, session_factory, proposal, legacy, questions
):
    client, _, _, _ = browser
    receipt = submit(client)
    url = f"{receipt.headers['Location']}/plan"
    run_id = UUID(receipt.json()["run_id"])
    analyze_fixture_run(session_factory)
    proposed = ["N/A", NO_QUESTIONS, *questions]
    with session_factory.begin() as session:
        plan = save_refactoring_plan(
            session, run_id, dict(proposal, blocking_questions=proposed), expected_revision=0
        )
        assert json.loads(plan.document)["blocking_questions"] == questions
        if legacy:
            plan.document = json.dumps(
                json.loads(plan.document) | {"blocking_questions": proposed}
            ).encode()
            plan.sha256 = sha256(plan.document).hexdigest()

    saved = client.get(url).json()
    assert saved["content"]["blocking_questions"] == questions
    assert saved["readiness"]["blocking_question_count"] == len(questions)
    assert saved["artifact"]["sha256"] == plan.sha256
    assert saved["readiness"]["outcome"] == ("needs_clarification" if questions else "ready")
    response = client.post(
        f"{url}/approve",
        json={"plan_id": str(plan.id), "revision": 1, "sha256": plan.sha256},
        headers={"Origin": "https://refactra.example", "X-Refactra-CSRF": "1"},
    )
    assert response.status_code == (409 if questions else 200)


@pytest.mark.parametrize("blocker", ["question", "check_id", "file", "test_target", "no_checks"])
def test_approval_binds_the_latest_ready_revision_and_locks_edits(
    browser, session_factory, proposal, blocker
):
    client, calls, _, _ = browser
    receipt = submit(client)
    url = f"{receipt.headers['Location']}/plan"
    run_id = UUID(receipt.json()["run_id"])
    analyze_fixture_run(session_factory)
    headers = {"Origin": "https://refactra.example", "X-Refactra-CSRF": "1"}
    draft = dict(proposal)
    if blocker == "question":
        draft["blocking_questions"] = ["How should empty carts behave?"]
    elif blocker == "check_id":
        draft["validation_checks"] = [dict(proposal["validation_checks"][0], check_id="VC1")]
    elif blocker == "file":
        draft["changes"] = [{"path": ".env", "action": "MODIFY", "intent": "Update values"}]
    elif blocker == "no_checks":
        draft["validation_checks"] = []
    else:
        draft["validation_checks"] = [dict(proposal["validation_checks"][0], paths=["src/main.py"])]
    with session_factory.begin() as session:
        first = save_refactoring_plan(session, run_id, draft, expected_revision=0)
    old = {"plan_id": str(first.id), "revision": 1, "sha256": first.sha256}

    calls.clear()
    rejected = client.post(f"{url}/approve", json=old)
    assert rejected.status_code == 403 and calls == []
    blocked = client.post(f"{url}/approve", json=old, headers=headers)
    if blocker in {"check_id", "test_target", "no_checks"}:
        assert blocked.status_code == 200 and blocked.json()["revision"] == 1
        assert client.get(url).json()["readiness"]["outcome"] == "ready"
        return
    assert blocked.status_code == 409 and "Resolve" in blocked.text
    if blocker == "file":
        assert client.get(url).json()["readiness"]["scope_errors"]

    revised = client.post(url, json={"expected_revision": 1, "proposal": proposal}, headers=headers)
    artifact = revised.json()["artifact"]
    approval = {"plan_id": artifact["id"], "revision": 2, "sha256": artifact["sha256"]}
    for stale in (old, dict(approval, sha256="b" * 64), dict(approval, plan_id=str(uuid4()))):
        response = client.post(f"{url}/approve", json=stale, headers=headers)
        assert response.status_code == 409 and "Plan revision changed" in response.text

    approved = client.post(f"{url}/approve", json=approval, headers=headers)
    assert approved.status_code == 200 and approved.headers["cache-control"] == "no-store"
    record = approved.json()
    assert record["revision"] == 2 and record["sha256"] == artifact["sha256"]
    assert record["approved_by"] == 12345
    assert client.post(f"{url}/approve", json=approval, headers=headers).json() == record
    assert client.get(url).json()["approval"] == record
    assert client.get(f"{url}?revision=1").json()["approval"] is None

    edit = {"expected_revision": 2, "proposal": dict(proposal, summary="A later change")}
    assert client.post(url, json=edit, headers=headers).status_code == 409
    with session_factory() as session:
        events = session.scalars(
            select(RunEvent).where(RunEvent.event_type == "PLAN_APPROVED")
        ).all()
        assert len(events) == 1
        assert events[0].payload == approval | {"approved_by": 12345}
        approvals = select(func.count()).where(RunCommand.kind == "APPLY_PLAN_APPROVAL")
        assert session.scalar(approvals) == 1


def test_read_saved_candidate_result(browser, session_factory, proposal):
    client, _, _, _ = browser
    receipt = submit(client)
    url = f"{receipt.headers['Location']}/result"
    run_id = UUID(receipt.json()["run_id"])
    missing = client.get(url)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "candidate_result_not_found"
    assert missing.headers["cache-control"] == "no-store"
    live_url = f"{url}?progress=true"
    initial = client.get(live_url)
    assert initial.json() == {"status": "idle", "message": None, "result": None}
    assert initial.headers["cache-control"] == "no-store"

    analyze_fixture_run(session_factory)
    with session_factory.begin() as session:
        plan = save_refactoring_plan(session, run_id, proposal, expected_revision=0)
        approve_refactoring_plan(
            session,
            run_id,
            plan_id=plan.id,
            revision=plan.revision,
            sha256=plan.sha256,
            actor_id=12345,
        )
    assert client.get(live_url).json()["status"] == "queued"
    with session_factory.begin() as session:
        job = RunCommand(run_id=run_id, kind=VALIDATE_CANDIDATE, dedupe_key="fixture-validation")
        session.add(job)
    assert client.get(live_url).json()["status"] == "validating"

    with session_factory.begin() as session:
        check = ValidationResult(
            "python.pytest",
            ["tests/test_cart.py"],
            True,
            "PASSED",
            0,
            50,
            "1 passed",
            None,
            "fixture",
        )
        fixtures = []
        for total in (2, 3):
            content = f"def cart(): return {total}\n"
            patch = CandidatePatch(
                base_sha=PIN,
                plan_sha256=plan.sha256,
                edits=[{"path": "src/cart.py", "action": "MODIFY", "content": content}],
            )
            diff = (
                "--- a/src/cart.py\n+++ b/src/cart.py\n@@ -1 +1 @@\n"
                f"-def cart(): return 1\n+{content}"
            )
            fixtures.append(
                (
                    save_validated_candidate(
                        session, run_id, patch, diff.encode(), [check, passing_patch("src/cart.py")]
                    ),
                    diff,
                )
            )

    for path, (saved, diff) in [(url, fixtures[1]), (f"{url}?revision=1", fixtures[0])]:
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        data = response.json()
        assert data["id"] == str(saved.id) and data["revision"] == saved.revision
        assert data["sha256"] == saved.sha256 and data["diff"] == diff
        assert data["approved_plan"] == {"id": str(plan.id), "revision": 1, "sha256": plan.sha256}
        assert data["repository"]["base_sha"] == PIN
        assert data["validation"][0]["output"] == "1 passed"
        assert data["validation"][0]["exit_code"] == 0
        assert data["review"]["criteria"][0]["id"] == "AC-1"
        assert data["publication_target"] is None and data["publication_approval"] is None
        assert data["publication"] is None and data["publication_waivers"] == []
        assert "candidate" not in data
    assert client.get(f"{url}?revision=3").status_code == 404
    assert client.get(live_url).json()["status"] == "preparing"
    with session_factory.begin() as session:
        session.get(RunCommand, job.id).status = "FAILED"
    stopped = client.get(live_url).json()
    assert stopped["status"] == "failed"
    assert stopped["message"] == "The worker could not prepare the reviewed commit."
    assert stopped["result"]["diff"] == fixtures[1][1]

    latest, _ = fixtures[1]
    with session_factory.begin() as session:
        target_sha = save_publication_target(
            session,
            {
                "run_id": str(run_id),
                "result_id": str(latest.id),
                "result_revision": 2,
                "result_sha256": latest.sha256,
                "commit_sha": "c" * 40,
                "destination": {
                    "repository_id": 42,
                    "base_branch": "feature/cart",
                    "base_sha": PIN,
                    "head_branch": f"refactra/{run_id}/r2",
                },
            },
        )
    target = client.get(url).json()["publication_target"]
    assert client.get(live_url).json()["status"] == "idle"
    assert target["sha256"] == target_sha and target["commit_sha"] == "c" * 40
    assert target["destination"]["base_branch"] == "feature/cart"
    body = {"result_id": str(latest.id), "target_sha256": target_sha}
    headers = {"Origin": "https://refactra.example", "X-Refactra-CSRF": "1"}
    waiver = {
        "result_id": str(latest.id),
        "result_sha256": latest.sha256,
        "check_index": 0,
        "reason": "Reviewed manually.",
    }
    assert client.post(f"{url}/waivers", json=waiver).status_code == 403
    passed_check = client.post(f"{url}/waivers", json=waiver, headers=headers)
    assert passed_check.status_code == 409
    assert passed_check.json()["error"]["code"] == "publication_waiver_conflict"
    assert client.post(f"{url}/approve", json=body).status_code == 403
    for stale in (dict(body, result_id=str(fixtures[0][0].id)), dict(body, target_sha256="b" * 64)):
        assert client.post(f"{url}/approve", json=stale, headers=headers).status_code == 409
    for _ in range(2):
        approved = client.post(f"{url}/approve", json=body, headers=headers)
        assert approved.status_code == 200 and approved.headers["cache-control"] == "no-store"
        assert approved.json() == {**body, "result_revision": 2, "approved_by": 12345}
    assert client.get(url).json()["publication_approval"] == approved.json()
    assert client.get(url).json()["publication"] is None
    assert client.get(live_url).json()["status"] == "publishing"
    with session_factory() as session:
        events = session.scalars(
            select(RunEvent).where(RunEvent.event_type == PUBLICATION_APPROVED)
        ).all()
        assert [event.payload for event in events] == [approved.json()]
        commands = session.scalars(
            select(RunCommand).where(RunCommand.kind == PUBLISH_CANDIDATE)
        ).all()
        assert [(job.run_id, job.dedupe_key, job.status) for job in commands] == [
            (run_id, publication_command_key(run_id, target_sha), "PENDING")
        ]

    pushed = {
        "target_sha256": target_sha,
        "commit_sha": target["commit_sha"],
        "head_branch": target["destination"]["head_branch"],
        "repository": "fixture/example",
    }
    link = "https://github.com/fixture/example/pull/7"
    draft = {**pushed, "pull_request_number": 7, "pull_request_url": link}
    with session_factory.begin() as session:
        session.add_all(
            [
                RunEvent(run_id=run_id, event_type=PUBLICATION_BRANCH_PUSHED, payload=pushed),
                RunEvent(
                    run_id=run_id,
                    event_type=PUBLICATION_PR_OPENED,
                    payload={**draft, "commit_sha": "d" * 40},
                ),
            ]
        )
    assert client.get(url).json()["publication"] == {"status": "branch_pushed"}
    with session_factory.begin() as session:
        job = session.scalar(select(RunCommand).where(RunCommand.kind == PUBLISH_CANDIDATE))
        job.status = "FAILED"
    failed = client.get(live_url).json()
    assert failed["status"] == "failed"
    assert failed["message"] == "The worker could not finish publishing the draft PR."
    assert failed["result"]["publication"] == {"status": "branch_pushed"}
    with session_factory.begin() as session:
        session.add(RunEvent(run_id=run_id, event_type=PUBLICATION_PR_OPENED, payload=draft))
    assert client.get(url).json()["publication"] == {
        "status": "draft_opened",
        "number": 7,
        "url": link,
    }
    assert client.get(f"{url}?revision=1").json()["publication"] is None
    published = client.get(live_url).json()
    assert published["status"] == "idle"
    assert published["result"]["publication"]["url"] == link


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("GET", ""),
        ("GET", "/plan"),
        ("GET", "/result"),
        ("GET", "/result?progress=true"),
        ("POST", "/plan"),
        ("POST", "/plan/approve"),
        ("POST", "/result/approve"),
        ("POST", "/result/waivers"),
    ],
)
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
def test_run_routes_recheck_access(
    browser, session_factory, monkeypatch, method, suffix, problem, expected, proposal
):
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
    payload = None
    if suffix == "/result/approve":
        payload = {"result_id": str(uuid4()), "target_sha256": "a" * 64}
    elif suffix == "/result/waivers":
        payload = {
            "result_id": str(uuid4()),
            "result_sha256": "a" * 64,
            "check_index": 0,
            "reason": "Reviewed manually.",
        }
    elif method == "POST":
        payload = (
            {"plan_id": str(uuid4()), "revision": 1, "sha256": "a" * 64}
            if suffix.endswith("/approve")
            else {"expected_revision": 1, "proposal": proposal}
        )
    result = client.request(
        method,
        url + suffix,
        json=payload,
        headers={"Origin": "https://refactra.example", "X-Refactra-CSRF": "1"},
    )
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
    assert "analysis_text" not in result.json()
    assert result.json()["stop_reason"]["code"] == public_code
