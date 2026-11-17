from datetime import timedelta
from uuid import UUID, uuid4

from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from refactra.api import create_app
from refactra.config import Settings
from refactra.models import Run, RunCommand, RunEvent
from refactra.worker import claim_command, process_command

from .conftest import migration_config


def submit(client, key=None, task="Extract duplicated calculation logic."):
    return client.post(
        "/api/v1/mock/runs",
        json={"task_text": task},
        headers={"Idempotency-Key": str(key or uuid4())},
    )


def test_liveness_and_disabled_features_do_not_require_a_database():
    settings = Settings("postgresql+psycopg://unused:unused@localhost:1/unused")
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        assert client.get("/api/v1/capabilities").json() == {
            "mock_enabled": False,
            "live_execution_enabled": False,
            "approvals_enabled": False,
            "github_publication_enabled": False,
        }
        assert submit(client).json()["error"]["code"] == "mock_disabled"
        assert client.post("/api/v1/runs").json()["error"]["code"] == "live_execution_disabled"
        assert client.get("/health/ready").status_code == 503


def test_submission_is_atomic_idempotent_and_conflict_checked(client, session_factory):
    key = uuid4()
    first = submit(client, key)
    assert first.status_code == 202
    assert first.headers["Location"] == first.json()["status_url"]
    second = submit(client, key)
    assert second.status_code == 200
    assert first.json()["run_id"] == second.json()["run_id"]
    assert submit(client, key, "Different task").status_code == 409
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 1
        assert session.scalar(select(func.count()).select_from(RunCommand)) == 1
        assert session.scalar(select(func.count()).select_from(RunEvent)) == 1


def test_mock_inputs_are_bounded_and_unknown_fields_rejected(client):
    for payload in (
        {"task_text": " "},
        {"task_text": "x" * 4001},
        {"task_text": "Valid task", "status": "COMPLETED"},
    ):
        response = client.post(
            "/api/v1/mock/runs", json=payload, headers={"Idempotency-Key": str(uuid4())}
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"
    assert client.post("/api/v1/mock/runs", json={"task_text": "Task"}).status_code == 422
    assert client.get(f"/api/v1/mock/runs/{uuid4()}").status_code == 404
    assert client.get("/health/ready").status_code == 200


def test_worker_consumes_persisted_command_without_claiming_real_work(client, session_factory):
    receipt = submit(client).json()
    command_id = claim_command(session_factory, "worker-a")
    assert command_id is not None
    assert claim_command(session_factory, "worker-b") is None
    assert process_command(session_factory, command_id, "worker-b") is False
    assert process_command(session_factory, command_id, "worker-a") is True
    assert process_command(session_factory, command_id, "worker-a") is False
    result = client.get(receipt["status_url"]).json()
    assert result["status"] == "NEEDS_HUMAN_REVIEW"
    assert result["stop_reason"]["code"] == "mock_boundary"
    assert result["validation"] is None and result["pull_request"] is None
    events = client.get(f"{receipt['status_url']}/events?limit=2").json()
    assert events["has_more"] is True
    tail = client.get(f"{receipt['status_url']}/events?after_id={events['next_after_id']}").json()
    all_events = events["items"] + tail["items"]
    assert [event["event_type"] for event in all_events] == [
        "RUN_CREATED",
        "WORKER_CLAIMED",
        "MOCK_ANALYSIS",
        "RUN_STOPPED",
    ]
    with session_factory() as session:
        assert session.get(RunCommand, command_id).status == "DONE"


def test_queue_skips_a_locked_row(client, session_factory):
    first_id = UUID(submit(client).json()["run_id"])
    second_id = UUID(submit(client).json()["run_id"])
    with session_factory.begin() as locking_session:
        first = locking_session.scalar(
            select(RunCommand).where(RunCommand.run_id == first_id).with_for_update()
        )
        command_id = claim_command(session_factory, "worker-b")
        assert command_id != first.id
        with session_factory() as session:
            assert session.get(RunCommand, command_id).run_id == second_id


def test_expired_mock_claim_recovers_and_old_owner_cannot_process(client, session_factory):
    receipt = submit(client).json()
    command_id = claim_command(session_factory, "old-worker")
    with session_factory.begin() as session:
        command_row = session.get(RunCommand, command_id)
        command_row.lease_until = session.scalar(select(func.now())) - timedelta(seconds=1)
    assert claim_command(session_factory, "restarted-worker") == command_id
    assert process_command(session_factory, command_id, "old-worker") is False
    assert process_command(session_factory, command_id, "restarted-worker") is True
    with session_factory() as session:
        assert session.get(RunCommand, command_id).attempts == 2
    assert client.get(receipt["status_url"]).json()["stop_reason"]["code"] == "mock_boundary"


def test_a_new_api_instance_reads_the_persisted_run(client, database, session_factory):
    receipt = submit(client).json()
    with TestClient(create_app(Settings("unused", enable_mock=True), database)) as restarted:
        assert restarted.get(receipt["status_url"]).json()["status"] == "CREATED"
        command_id = claim_command(session_factory, "new-worker")
        process_command(session_factory, command_id, "new-worker")
        assert restarted.get(receipt["status_url"]).json()["status"] == "NEEDS_HUMAN_REVIEW"


def test_migration_downgrade_and_upgrade(database):
    with database.begin() as connection:
        config = migration_config(connection)
        command.downgrade(config, "base")
        assert connection.scalar(text("SELECT to_regclass('runs')")) is None
        command.upgrade(config, "head")
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version")) == "0001_foundation"
        )
