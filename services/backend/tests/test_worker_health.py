from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select

import refactra.worker as worker
from refactra.models import Run, RunCommand, RunEvent, WorkerHeartbeat
from refactra.worker import claim_command
from refactra.worker_health import (
    HEARTBEAT_SLOT,
    MAX_COMMAND_ATTEMPTS,
    clear_heartbeat,
    record_heartbeat,
    worker_status,
)


def mock_run(session_factory, task="Mock task", *, attempts=0, age=timedelta(0)):
    with session_factory.begin() as session:
        run = Run(task_text=task)
        session.add(run)
        session.flush()
        command = RunCommand(
            run_id=run.id,
            kind="START",
            dedupe_key=f"mock:{run.id}",
            attempts=attempts,
            available_at=session.scalar(select(func.now())) - age,
        )
        session.add(command)
        session.flush()
        return run.id, command.id


def run_worker_once(monkeypatch, database):
    monkeypatch.setattr(worker, "make_engine", lambda _url: database)
    monkeypatch.setattr(worker, "WORKER_LOCK", uuid4().int >> 65)
    monkeypatch.setattr(worker.signal, "signal", lambda *_: None)
    monkeypatch.setenv("REFACTRA_ENABLE_MOCK", "true")
    monkeypatch.setattr("sys.argv", ["worker", "--once"])
    worker.main()


def stop_codes(session, run_id):
    return session.scalars(
        select(RunEvent.payload["code"].astext).where(
            RunEvent.run_id == run_id, RunEvent.event_type == "RUN_STOPPED"
        )
    ).all()


def test_heartbeat_reports_running_stale_and_missing_workers(session_factory):
    with session_factory() as session:
        assert worker_status(session).status == "missing"
    record_heartbeat(session_factory, "worker-a", "repository_workflow", started=True)
    with session_factory() as session:
        status = worker_status(session)
        assert (status.status, status.mode, status.healthy) == (
            "running",
            "repository_workflow",
            True,
        )
    with session_factory.begin() as session:
        beat = session.get(WorkerHeartbeat, HEARTBEAT_SLOT)
        beat.beat_at = session.scalar(select(func.now())) - timedelta(minutes=2)
    with session_factory() as session:
        assert worker_status(session).status == "stale"
    clear_heartbeat(session_factory, "another-worker")  # Only the slot's owner clears it.
    with session_factory() as session:
        assert worker_status(session).status == "stale"
    clear_heartbeat(session_factory, "worker-a")
    with session_factory() as session:
        assert worker_status(session).status == "missing"


def test_old_claimable_work_is_reported_as_a_stalled_queue(client, session_factory):
    record_heartbeat(session_factory, "worker-a", "mock", started=True)
    healthy = client.get("/health/worker")
    assert healthy.status_code == 200 and healthy.headers["cache-control"] == "no-store"
    assert healthy.json()["pending_commands"] == 0
    mock_run(session_factory, age=timedelta(minutes=10))
    stalled = client.get("/health/worker")
    assert stalled.status_code == 503
    body = stalled.json()
    assert body["status"] == "running" and body["stalled"] is True
    assert body["pending_commands"] == 1 and body["oldest_pending_seconds"] >= 600
    assert "run_id" not in stalled.text


def test_a_failing_command_stops_only_its_run_and_the_worker_keeps_serving(
    monkeypatch, database, session_factory
):
    poisoned_run, poisoned = mock_run(session_factory, "Poisoned")
    healthy_run, healthy = mock_run(session_factory, "Healthy")
    real = worker.process_command
    seen = []

    def process(factory, command_id, worker_id):
        with factory() as session:
            seen.append(worker_status(session).status)
        if command_id == poisoned:
            raise ValueError("Unexpected mock state; refusing to advance it.")
        return real(factory, command_id, worker_id)

    monkeypatch.setattr(worker, "process_command", process)
    run_worker_once(monkeypatch, database)  # Processes the poisoned command and returns.
    run_worker_once(monkeypatch, database)  # A later pass still serves the next run.

    assert seen == ["running", "running"]
    with session_factory() as session:
        assert session.get(RunCommand, poisoned).status == "FAILED"
        assert session.get(Run, poisoned_run).status == "FAILED"
        assert stop_codes(session, poisoned_run) == ["worker_command_failed"]
        failure = session.scalar(select(RunEvent).where(RunEvent.event_type == "COMMAND_FAILED"))
        assert failure.payload["error_type"] == "ValueError"
        assert "refusing" not in str(failure.payload)
        assert session.get(RunCommand, healthy).status == "DONE"
        assert session.get(Run, healthy_run).status == "NEEDS_HUMAN_REVIEW"
        assert worker_status(session).status == "missing"  # A clean exit clears the slot.


def test_repeatedly_interrupted_commands_are_failed_instead_of_reclaimed(session_factory):
    run_id, command_id = mock_run(session_factory, attempts=MAX_COMMAND_ATTEMPTS)
    assert claim_command(session_factory, "worker") is None
    with session_factory() as session:
        assert session.get(RunCommand, command_id).status == "FAILED"
        assert session.get(Run, run_id).status == "FAILED"
        assert stop_codes(session, run_id) == ["command_attempts_exhausted"]
