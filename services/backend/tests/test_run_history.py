from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from refactra.analysis_context import save_analysis_context
from refactra.api import create_app
from refactra.auth_sessions import GitHubIdentity, issue_session
from refactra.config import Settings
from refactra.models import Run, RunCommand, RunEvent, RunPlan
from refactra.publication_target import PUBLICATION_APPROVED, PUBLICATION_PR_OPENED
from refactra.refactoring_plan import approve_refactoring_plan, save_refactoring_plan
from refactra.repository_analysis_worker import enqueue_repository_analysis
from refactra.repository_candidate import GENERATE_CANDIDATE
from refactra.repository_revision import RepositoryRevision
from refactra.repository_runs import create_repository_run
from refactra.run_history import list_runs

from .test_analysis_context import prepare
from .test_refactoring_plan import analyze_fixture_run
from .test_refactoring_plan import proposal as proposal  # noqa: F401 - pytest fixture

OWNER = 123


def add_event(session_factory, run_id, kind, payload):
    with session_factory.begin() as session:
        session.add(RunEvent(run_id=run_id, event_type=kind, payload=payload))


def stages(session_factory, **options):
    with session_factory() as session:
        page = list_runs(session, OWNER, **options)
    return {item.task_summary: item for item in page.runs}, page


def test_each_run_shows_its_furthest_stage(session_factory, proposal):
    planned, _, context = prepare(session_factory, task_text="planned")
    with session_factory.begin() as session:
        save_analysis_context(session, context)
        enqueue_repository_analysis(session, planned.id)
    analyze_fixture_run(session_factory)
    with session_factory.begin() as session:
        plan = save_refactoring_plan(session, planned.id, proposal, expected_revision=0)

    fresh, _, _ = prepare(session_factory, task_text="fresh")
    stopped, _, _ = prepare(session_factory, task_text="stopped")
    add_event(session_factory, stopped.id, "RUN_STOPPED", {"code": "analysis_access_revoked"})
    with session_factory.begin() as session:
        session.get(Run, stopped.id).status = "FAILED"  # As the analysis worker does.
    broken, _, _ = prepare(session_factory, task_text="broken")
    with session_factory.begin() as session:
        session.add(
            RunCommand(
                run_id=broken.id, kind=GENERATE_CANDIDATE, status="FAILED", dedupe_key=str(uuid4())
            )
        )
    publishing, _, _ = prepare(session_factory, task_text="publishing")
    add_event(session_factory, publishing.id, PUBLICATION_APPROVED, {})
    published, _, _ = prepare(session_factory, task_text="published")
    add_event(session_factory, published.id, PUBLICATION_APPROVED, {})
    add_event(
        session_factory,
        published.id,
        PUBLICATION_PR_OPENED,
        {"pull_request_number": 5, "pull_request_url": "https://github.com/fixture/repo/pull/5"},
    )

    found, page = stages(session_factory)
    assert [item.task_summary for item in page.runs] == [
        "published",
        "publishing",
        "broken",
        "stopped",
        "fresh",
        "planned",
    ]
    assert {name: item.stage for name, item in found.items()} == {
        "planned": "plan_ready",
        "fresh": "analyzing",
        "stopped": "failed",
        "broken": "failed",
        "publishing": "publishing",
        "published": "published",
    }
    assert found["published"].pull_request.model_dump() == {
        "number": 5,
        "url": "https://github.com/fixture/repo/pull/5",
    }
    assert found["broken"].stop_reason == "The worker could not generate the candidate patch."
    assert found["stopped"].stop_reason and found["fresh"].stop_reason is None
    assert found["planned"].plan_revisions == 1 and found["fresh"].plan_revisions == 0
    # The analysis call was reserved and settled, so the planned run has a cost.
    assert float(found["planned"].cost_usd) > 0 and found["fresh"].cost_usd == "0.0000"
    assert found["planned"].repository_full_name == "fixture/repo"
    assert found["planned"].last_activity_at >= found["planned"].created_at

    with session_factory.begin() as session:
        approve_refactoring_plan(
            session,
            planned.id,
            plan_id=plan.id,
            revision=plan.revision,
            sha256=plan.sha256,
            actor_id=OWNER,
        )
    found, _ = stages(session_factory)
    assert found["planned"].stage == "generating"


def test_pages_search_and_privacy(session_factory):
    for name in ("cart one", "cart two", "orders"):
        prepare(session_factory, task_text=name)
    with session_factory.begin() as session:
        create_repository_run(
            session,
            identity=GitHubIdentity(999, "someone-else"),
            installation_id=77,
            revision=RepositoryRevision(43, "other/private", "main", "main", "b" * 40),
            task_text="cart secret",
        )
        session.add(Run(task_text="cart mock run", requested_by=OWNER))
    _, first = stages(session_factory, limit=2)
    assert [item.task_summary for item in first.runs] == ["orders", "cart two"]
    _, second = stages(session_factory, limit=2, cursor=first.next_cursor)
    assert [item.task_summary for item in second.runs] == ["cart one"]
    assert second.next_cursor is None
    # Search covers the task and the repository name, never another user's or mock runs.
    found, _ = stages(session_factory, query="CART")
    assert set(found) == {"cart one", "cart two"}
    found, _ = stages(session_factory, query="fixture/")
    assert set(found) == {"cart one", "cart two", "orders"}
    found, _ = stages(session_factory, query="100%_")
    assert found == {}


def test_long_tasks_are_summarised(session_factory):
    prepare(session_factory, task_text="word " * 100)
    _, page = stages(session_factory)
    summary = page.runs[0].task_summary
    assert len(summary) == 200 and summary.endswith("…") and "  " not in summary


@pytest.fixture
def client(database, session_factory, monkeypatch):
    monkeypatch.setenv("REFACTRA_ALLOWED_GITHUB_USER_IDS", str(OWNER))
    with TestClient(create_app(Settings("unused"), database)) as client:
        yield client


def sign_in(session_factory, client, user_id=OWNER):
    with session_factory.begin() as session:
        token = issue_session(session, GitHubIdentity(user_id, "fixture"))
    client.cookies.set("refactra_session", token)


def test_the_endpoint_serves_only_invited_signed_in_users(session_factory, client):
    prepare(session_factory, task_text="mine")
    assert client.get("/api/v1/runs").status_code == 401
    sign_in(session_factory, client, user_id=999)
    assert client.get("/api/v1/runs").status_code == 403
    sign_in(session_factory, client)
    response = client.get("/api/v1/runs", params={"limit": 1})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert [run["task_summary"] for run in body["runs"]] == ["mine"]
    assert body["next_cursor"] is None
    assert body["runs"][0]["base_sha"] == "a" * 40
    assert client.get("/api/v1/runs", params={"cursor": "not-a-cursor"}).status_code == 400
    assert client.get("/api/v1/runs", params={"limit": 51}).status_code == 422


def test_new_runs_store_the_repository_name(session_factory):
    run, _, _ = prepare(session_factory)
    with session_factory() as session:
        assert session.scalar(select(Run.repository_full_name).where(Run.id == run.id)) == (
            "fixture/repo"
        )
        assert session.scalar(select(RunPlan.id).where(RunPlan.run_id == run.id)) is None
