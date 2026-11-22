from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from refactra.diagnostics import diagnostic_id, workflow_log
from refactra.models import RunCommand, RunEvent


def make_engine(url: str) -> Engine:
    if not url.startswith("postgresql+psycopg://"):
        raise ValueError("Refactra requires PostgreSQL with the psycopg driver.")
    return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})


def sessions(engine: Engine) -> sessionmaker[Session]:
    factory = sessionmaker(engine, expire_on_commit=False)
    event.listen(factory, "before_flush", _collect_workflow_events)
    event.listen(factory, "after_commit", _log_workflow_events)
    event.listen(factory, "after_rollback", _discard_workflow_events)
    return factory


def _collect_workflow_events(session, _flush, _instances):
    entries = session.info.setdefault("workflow_log", [])
    for record in session.new:
        if isinstance(record, RunEvent):
            payload = record.payload or {}
            entries.append(
                {
                    "run_id": record.run_id,
                    "stage_event": record.event_type,
                    "command_id": diagnostic_id(payload.get("command_id")),
                    "code": diagnostic_id(payload.get("code")),
                }
            )
    for command in session.dirty:
        if isinstance(command, RunCommand) and inspect(command).attrs.status.history.has_changes():
            entries.append(
                {
                    "run_id": command.run_id,
                    "command_id": command.id,
                    "stage": command.kind,
                    "attempt": command.attempts,
                    "status": command.status,
                }
            )


def _log_workflow_events(session):
    for entry in session.info.pop("workflow_log", ()):
        failed = entry.get("status") == "FAILED" or (
            entry.get("stage_event", "").endswith(("_FAILED", "_REJECTED", "_STOPPED"))
            and entry.get("code") not in {"analysis_ready", "mock_boundary"}
        )
        workflow_log("workflow_transition", warning=failed, **entry)


def _discard_workflow_events(session):
    session.info.pop("workflow_log", None)
