from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from refactra.auth_sessions import GitHubIdentity
from refactra.github_app import InstallationToken
from refactra.models import Run, RunCommand, RunEvent
from refactra.repository_revision import RepositoryRevision, resolve_revision
from refactra.repository_runs import create_repository_run
from refactra.worker import claim_command

IDENTITY = GitHubIdentity(123, "fixture-user")
REVISION = RepositoryRevision(42, "fixture/example", "main", "feature/refactor", "a" * 40)


def create(session, **overrides):
    arguments = dict(
        identity=IDENTITY,
        installation_id=77,
        revision=REVISION,
        task_text="  Extract duplicated logic.  ",
    )
    return create_repository_run(session, **(arguments | overrides))


def test_persisted_revision_survives_new_connection_and_a_moving_branch(session_factory, database):
    head = "a" * 40

    def handler(request):
        if request.url.path == "/repositories/42":
            payload = {"id": 42, "full_name": "fixture/example", "default_branch": "main"}
        else:
            payload = {
                "ref": "refs/heads/feature/refactor",
                "object": {"type": "commit", "sha": head},
            }
        return httpx.Response(200, json=payload)

    token = InstallationToken("fixture-token", 42, datetime.now(UTC) + timedelta(minutes=10))
    transport = httpx.MockTransport(handler)
    revision = resolve_revision(token, base_branch=REVISION.base_branch, transport=transport)
    with session_factory.begin() as session:
        run_id = create(session, revision=revision).id
    head = "b" * 40
    assert (
        resolve_revision(token, base_branch=REVISION.base_branch, transport=transport).commit_sha
        == head
    )
    database.dispose()  # Discard pooled connections; reload only from PostgreSQL.
    with session_factory() as session:
        run = session.get(Run, run_id)
        assert (run.requested_by, run.github_installation_id, run.github_repository_id) == (
            123,
            77,
            42,
        )
        assert run.base_branch == "feature/refactor" and run.base_sha == "a" * 40
        assert run.task_text == "Extract duplicated logic."
        assert (run.status, run.workflow_version, run.version) == ("CREATED", "repository-v0.1", 1)
        assert run.created_at is not None and run.updated_at is not None
        event = session.scalar(select(RunEvent).where(RunEvent.run_id == run_id))
        assert event.event_type == "RUN_CREATED"
        assert event.payload == {
            "mode": "repository",
            "base_branch": "feature/refactor",
            "base_sha": "a" * 40,
        }


def test_caller_rollback_removes_both_run_and_event(session_factory):
    with pytest.raises(RuntimeError, match="abort"):
        with session_factory.begin() as session:
            create(session)
            raise RuntimeError("abort")
    with session_factory() as session:
        for model in (Run, RunEvent):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_repository_record_is_not_queued_or_exposed_as_a_mock(client, session_factory):
    with session_factory.begin() as session:
        run_id = create(session).id
    assert claim_command(session_factory, "mock-worker") is None
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(RunCommand)) == 0
        assert session.get(Run, run_id).status == "CREATED"
    assert client.get(f"/api/v1/mock/runs/{run_id}").status_code == 404
    assert client.get(f"/api/v1/mock/runs/{run_id}/events").status_code == 404
    response = client.post("/api/v1/runs", json={"task_text": "Refactor"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "live_execution_disabled"


@pytest.mark.parametrize(
    "overrides",
    [
        {"installation_id": 0},
        {"installation_id": True},
        {"revision": replace(REVISION, repository_id=0)},
        {"revision": replace(REVISION, base_branch="HEAD")},
        {"revision": replace(REVISION, commit_sha="a" * 7)},
        {"revision": replace(REVISION, commit_sha="g" * 40)},
        {"task_text": " "},
        {"task_text": "x" * 4001},
    ],
)
def test_invalid_record_inputs_write_nothing(session_factory, overrides):
    with session_factory.begin() as session:
        with pytest.raises(ValueError):
            create(session, **overrides)
    with session_factory() as session:
        for model in (Run, RunEvent):
            assert session.scalar(select(func.count()).select_from(model)) == 0
