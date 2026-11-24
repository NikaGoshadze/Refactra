"""Retrieve a pinned source snapshot and queue analysis outside the HTTP request."""

from datetime import timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from refactra.analysis_context import (
    load_analysis_context,
    prepare_analysis_context,
    save_analysis_context,
)
from refactra.github_app import GitHubAppError
from refactra.models import Run, RunCommand, RunEvent
from refactra.repository_analysis_worker import (
    WORKFLOW,
    _claimed_command,
    enqueue_repository_analysis,
)
from refactra.repository_snapshot import fetch_source_snapshot
from refactra.run_access import RunAccessDenied, authorize_run_repository

PREPARE_SNAPSHOT = "PREPARE_SNAPSHOT"
SNAPSHOT_RETRY = "SNAPSHOT_RETRY"
SNAPSHOT_RETRY_ATTEMPTS = 3
SNAPSHOT_RETRY_DELAY = timedelta(seconds=15)
RETRYABLE = {"analysis_access_unavailable", "snapshot_unavailable"}


def snapshot_command_key(run_id: UUID) -> str:
    return f"repository:snapshot:{run_id}"


def enqueue_snapshot_preparation(session: Session, run_id: UUID) -> UUID:
    """Queue preparation once per run, in the caller's transaction."""
    key = snapshot_command_key(run_id)
    existing = session.scalar(select(RunCommand.id).where(RunCommand.dedupe_key == key))
    if existing is not None:
        return existing
    command = RunCommand(run_id=run_id, kind=PREPARE_SNAPSHOT, dedupe_key=key)
    session.add(command)
    session.flush()
    return command.id


def _complete(session, command) -> None:
    command.status = "DONE"
    command.completed_at = session.scalar(select(func.now()))
    command.lease_until = None


def _stop_or_retry(session_factory, command_id, worker_id, attempt, run_id, code) -> bool:
    with session_factory.begin() as session:
        command = _claimed_command(session, command_id, worker_id)
        if command is None or command.attempts != attempt:
            return False
        now = session.scalar(select(func.now()))
        if code in RETRYABLE and attempt < SNAPSHOT_RETRY_ATTEMPTS:
            command.status = "PENDING"
            command.claimed_by = None
            command.lease_until = None
            command.available_at = now + SNAPSHOT_RETRY_DELAY
            session.add(
                RunEvent(
                    run_id=run_id,
                    event_type=SNAPSHOT_RETRY,
                    payload={"code": code, "attempt": attempt},
                )
            )
            return True
        run = session.get(Run, run_id, with_for_update=True)
        run.status = "FAILED"
        run.version += 1
        run.updated_at = now
        command.status = "FAILED"
        command.completed_at = now
        command.lease_until = None
        session.add(RunEvent(run_id=run_id, event_type="RUN_STOPPED", payload={"code": code}))
        return True


def process_snapshot_preparation(
    session_factory, command_id: UUID, worker_id: str, *, authorize=None
) -> bool:
    """Recheck access, fetch the pinned SHA, save the context and queue analysis atomically."""
    with session_factory.begin() as session:
        command = _claimed_command(session, command_id, worker_id)
        now = session.scalar(select(func.now()))
        if command is None or command.kind != PREPARE_SNAPSHOT or command.lease_until <= now:
            return False
        run = session.get(Run, command.run_id, with_for_update=True)
        run_id, attempt = run.id, command.attempts
        if run.workflow_version != WORKFLOW or command.dedupe_key != snapshot_command_key(run_id):
            raise ValueError("Expected a queued snapshot preparation command.")
        if run.status != "CREATED":
            _complete(session, command)  # The run already moved on or stopped.
            return True
        if load_analysis_context(session, run_id) is not None:
            enqueue_repository_analysis(session, run_id)
            _complete(session, command)
            return True

    try:
        app, token = (authorize or authorize_run_repository)(run)
    except (RunAccessDenied, HTTPException):
        code = "analysis_access_revoked"
    except GitHubAppError:
        code = "analysis_access_unavailable"
    except (KeyError, OSError, ValueError):
        code = "analysis_access_unconfigured"
    else:
        try:
            snapshot = fetch_source_snapshot(run, token, transport=app.transport)
            context = prepare_analysis_context(run, snapshot)
            code = None
        except GitHubAppError:
            code = "snapshot_unavailable"
        except ValueError:
            code = "snapshot_unsupported"
    if code is not None:
        return _stop_or_retry(session_factory, command_id, worker_id, attempt, run_id, code)

    with session_factory.begin() as session:
        command = _claimed_command(session, command_id, worker_id)
        if command is None or command.attempts != attempt:
            return False
        session.get(Run, run_id, with_for_update=True)
        if load_analysis_context(session, run_id) is None:
            save_analysis_context(session, context)
        enqueue_repository_analysis(session, run_id)
        _complete(session, command)
        return True
