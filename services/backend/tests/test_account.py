from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from refactra import repository_routes
from refactra.account import EMPTY_CONTEXT, RunInProgress, clean_deleted_artifacts, delete_account
from refactra.analysis_context import save_analysis_context
from refactra.api import create_app
from refactra.auth_sessions import GitHubIdentity, issue_session, resolve_session
from refactra.candidate_patch import CandidatePatch
from refactra.candidate_results import save_validated_candidate
from refactra.config import Settings
from refactra.github_auth import CALLBACK_PATH
from refactra.models import (
    AuthSession,
    ModelReservation,
    Run,
    RunAnalysisContext,
    RunAnalysisResult,
    RunCandidateResult,
    RunCommand,
    RunEvent,
    RunPlan,
    RunPlanApproval,
    UserAccess,
)
from refactra.refactoring_plan import approve_refactoring_plan, save_refactoring_plan
from refactra.repository_analysis_worker import (
    APPLY_PLAN_APPROVAL,
    WORKFLOW,
    enqueue_repository_analysis,
)
from refactra.repository_validation import ValidationResult
from refactra.run_history import list_runs
from refactra.usage_limits import monthly_cost
from refactra.worker import claim_command

from .test_analysis_context import prepare
from .test_candidate_results import passing_patch
from .test_refactoring_plan import analyze_fixture_run
from .test_refactoring_plan import proposal as proposal  # noqa: F401 - pytest fixture
from .test_repository_routes import browser as browser
from .test_repository_routes import submit

OWNER = 123
ORIGIN = "https://refactra.example"


def full_run(session_factory, proposal, task="Refactor the secret cart"):
    """A run with saved records at every stage up to a validated candidate."""
    run, _, context = prepare(session_factory, task_text=task)
    with session_factory.begin() as session:
        save_analysis_context(session, context)
        enqueue_repository_analysis(session, run.id)
    analyze_fixture_run(session_factory)
    with session_factory.begin() as session:
        plan = save_refactoring_plan(session, run.id, proposal, expected_revision=0)
        approve_refactoring_plan(
            session, run.id, plan_id=plan.id, revision=1, sha256=plan.sha256, actor_id=OWNER
        )
        candidate = CandidatePatch(
            base_sha="a" * 40,
            plan_sha256=plan.sha256,
            edits=[
                {"path": "src/cart.py", "action": "MODIFY", "content": "def cart(): return 2\n"}
            ],
        )
        pytest_check = ValidationResult(
            "python.pytest",
            ["tests/test_cart.py"],
            True,
            "PASSED",
            0,
            50,
            "3 passed",
            None,
            "fixture",
        )
        save_validated_candidate(
            session,
            run.id,
            candidate,
            b"--- a/src/cart.py\n",
            [pytest_check, passing_patch("src/cart.py")],
        )
    return run.id


def count(session, table, run_id):
    return session.scalar(select(func.count()).select_from(table).where(table.run_id == run_id))


def test_deleting_an_account_removes_content_but_keeps_anonymous_costs(session_factory, proposal):
    mine = full_run(session_factory, proposal)
    with session_factory.begin() as session:
        token = issue_session(session, GitHubIdentity(OWNER, "owner"))
        session.add(UserAccess(github_user_id=OWNER, github_login="owner", status="invited"))
        month = session.scalar(select(ModelReservation.budget_month).limit(1))
        before = monthly_cost(session, month)
        assert count(session, RunCandidateResult, mine) == 1
    with session_factory.begin() as session:
        assert delete_account(session, OWNER) == 1
    with session_factory() as session:
        run = session.get(Run, mine)
        assert (run.requested_by, run.repository_full_name, run.task_text, run.status) == (
            0,
            None,
            "Deleted by its owner.",
            "FAILED",
        )
        for table in (
            RunCandidateResult,
            RunPlanApproval,
            RunPlan,
            RunAnalysisResult,
            RunEvent,
            RunCommand,
        ):
            assert count(session, table, mine) == 0, table.__name__
        assert session.get(RunAnalysisContext, mine).payload == EMPTY_CONTEXT
        # The overall monthly budget still counts what was spent.
        assert count(session, ModelReservation, mine) > 0
        assert monthly_cost(session, month) == before > Decimal(0)
        assert resolve_session(session, token) is None
        assert session.scalar(select(func.count()).select_from(AuthSession)) == 0
        assert session.get(UserAccess, OWNER) is None
        assert list_runs(session, OWNER).runs == []


def test_other_users_and_in_progress_runs_are_protected(session_factory, proposal):
    mine = full_run(session_factory, proposal)
    assert claim_command(session_factory, "worker", workflow=WORKFLOW, kind=APPLY_PLAN_APPROVAL)
    with session_factory.begin() as session, pytest.raises(RunInProgress):
        delete_account(session, OWNER)
    with session_factory.begin() as session:
        assert delete_account(session, 999) == 0
    with session_factory() as session:
        assert session.get(Run, mine).task_text == "Refactor the secret cart"


def test_worker_cleans_only_deleted_owners_artifacts(session_factory, tmp_path):
    mine, _, _ = prepare(session_factory)
    other, _, _ = prepare(session_factory)
    with session_factory.begin() as session:
        session.get(Run, other.id).requested_by = OWNER + 1
        assert delete_account(session, OWNER) == 1
    for run in (mine, other):
        prepared = tmp_path / f"{run.id}-r1"
        prepared.mkdir()
        (prepared / "receipt.json").write_text("receipt")
        (tmp_path / f"{run.id}.asset").write_bytes(b"asset")
    unrelated = tmp_path / f"{mine.id}-review"
    unrelated.touch()
    for _ in range(2):
        clean_deleted_artifacts(session_factory, tmp_path)
    assert {p.name for p in tmp_path.iterdir()} == {
        unrelated.name,
        f"{other.id}-r1",
        f"{other.id}.asset",
    }
    with session_factory() as session:
        assert session.get(Run, other.id).requested_by == OWNER + 1


@pytest.fixture
def client(database, monkeypatch):
    monkeypatch.setenv("REFACTRA_GITHUB_CALLBACK_URL", f"{ORIGIN}{CALLBACK_PATH}")
    with TestClient(create_app(Settings("unused"), database), base_url=ORIGIN) as client:
        yield client


def post(client, confirm, *, trusted=True):
    headers = {"Origin": ORIGIN, "X-Refactra-CSRF": "1"} if trusted else {}
    return client.post("/api/v1/account/delete", json={"confirm_login": confirm}, headers=headers)


def test_the_endpoint_needs_a_trusted_origin_a_session_and_the_login(client, session_factory):
    prepare(session_factory)
    assert post(client, "owner", trusted=False).status_code == 403
    assert post(client, "owner").status_code == 401
    with session_factory.begin() as session:
        token = issue_session(session, GitHubIdentity(OWNER, "Owner-Name"))
    client.cookies.set("__Host-refactra_session", token, domain="refactra.example", path="/")
    assert post(client, "someone-else").json()["error"]["code"] == "confirmation_mismatch"
    response = post(client, " owner-name ")
    assert response.status_code == 200 and response.json() == {"deleted": True, "runs_cleared": 1}
    assert "__Host-refactra_session=" in response.headers["set-cookie"]
    with session_factory() as session:
        assert resolve_session(session, token) is None


def test_account_deleted_during_branch_lookup_cannot_create_a_run(
    browser, session_factory, monkeypatch
):
    client, *_ = browser
    pin = repository_routes.resolve_revision

    def delete_while_pinning(*args, **kwargs):
        revision = pin(*args, **kwargs)
        assert post(client, "old-login").status_code == 200
        return revision

    monkeypatch.setattr(repository_routes, "resolve_revision", delete_while_pinning)
    assert submit(client, prepare=False).status_code == 401
    with session_factory() as session:
        assert session.scalar(select(Run.id)) is None
