"""Worker heartbeat, queue backlog and per-command failure isolation."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from refactra.models import Run, RunCommand, RunEvent, WorkerHeartbeat

HEARTBEAT_SLOT = "execution"
HEARTBEAT_INTERVAL_SECONDS = 15
HEARTBEAT_STALE_SECONDS = 60
# A pending command older than this while a worker is alive suggests the wrong mode is running.
STALLED_QUEUE_SECONDS = 300
# Reclaims beyond this mean the command keeps interrupting its worker (for example, a hard kill).
MAX_COMMAND_ATTEMPTS = 5
ANALYSIS_STATES = {"CREATED", "ANALYZING"}
# Worker modes that prepare and analyze repository runs, and those that can publish drafts.
ANALYSIS_WORKER_MODES = {"repository_analysis", "repository_workflow"}
PUBLICATION_WORKER_MODES = {"repository_workflow", "repository_publication"}


def record_heartbeat(session_factory, worker_id: str, mode: str, *, started: bool = False):
    with session_factory.begin() as session:
        now = func.now()
        values = {"worker_id": worker_id, "mode": mode, "beat_at": now}
        statement = insert(WorkerHeartbeat).values(slot=HEARTBEAT_SLOT, started_at=now, **values)
        if started:
            values["started_at"] = now
        session.execute(
            statement.on_conflict_do_update(index_elements=[WorkerHeartbeat.slot], set_=values)
        )


def clear_heartbeat(session_factory, worker_id: str) -> None:
    with session_factory.begin() as session:
        session.execute(
            delete(WorkerHeartbeat).where(
                WorkerHeartbeat.slot == HEARTBEAT_SLOT, WorkerHeartbeat.worker_id == worker_id
            )
        )


@dataclass(frozen=True)
class WorkerStatus:
    status: str  # running, stale or missing
    mode: str | None
    heartbeat_age_seconds: int | None
    pending_commands: int
    oldest_pending_seconds: int | None
    stalled: bool

    @property
    def healthy(self) -> bool:
        return self.status == "running" and not self.stalled


def worker_status(session: Session) -> WorkerStatus:
    """Report liveness and backlog without exposing run IDs or command details."""
    now = session.scalar(select(func.now()))
    beat = session.get(WorkerHeartbeat, HEARTBEAT_SLOT)
    claimable = or_(
        and_(RunCommand.status == "PENDING", RunCommand.available_at <= now),
        and_(RunCommand.status == "CLAIMED", RunCommand.lease_until <= now),
    )
    pending, oldest = session.execute(
        select(func.count(), func.min(RunCommand.available_at)).where(claimable)
    ).one()
    age = round((now - beat.beat_at).total_seconds()) if beat else None
    oldest_age = round((now - oldest).total_seconds()) if oldest else None
    status = "missing" if beat is None else "stale" if age > HEARTBEAT_STALE_SECONDS else "running"
    return WorkerStatus(
        status=status,
        mode=beat.mode if beat else None,
        heartbeat_age_seconds=age,
        pending_commands=pending,
        oldest_pending_seconds=oldest_age,
        stalled=oldest_age is not None and oldest_age > STALLED_QUEUE_SECONDS,
    )


def stop_command(session: Session, command: RunCommand, *, code: str, error_type: str) -> None:
    """Fail one command and stop its run if the run was still preparing or analyzing."""
    now = session.scalar(select(func.now()))
    command.status = "FAILED"
    command.completed_at = now
    command.lease_until = None
    session.add(
        RunEvent(
            run_id=command.run_id,
            event_type="COMMAND_FAILED",
            payload={
                "command_id": str(command.id),
                "stage": command.kind,
                "code": code,
                "error_type": error_type,
            },
        )
    )
    run = session.get(Run, command.run_id, with_for_update=True)
    if run.status in ANALYSIS_STATES:
        run.status = "FAILED"
        run.version += 1
        run.updated_at = now
        session.add(RunEvent(run_id=run.id, event_type="RUN_STOPPED", payload={"code": code}))


def fail_claimed_command(session_factory, command_id: UUID, worker_id: str, error_type: str):
    """Isolate an unexpected error to its command so the worker can keep serving others."""
    with session_factory.begin() as session:
        command = session.get(RunCommand, command_id, with_for_update=True)
        if command is None or command.status != "CLAIMED" or command.claimed_by != worker_id:
            return False
        stop_command(session, command, code="worker_command_failed", error_type=error_type)
        return True
