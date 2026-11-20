"""Process mock commands, repository analysis, or approved plan handoffs."""

import argparse
import json
import logging
import os
import signal
import threading
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import SQLAlchemyError

from refactra.config import Settings
from refactra.db import make_engine, sessions
from refactra.models import Run, RunCommand, RunEvent
from refactra.openai_analysis import OpenAIAnalysisProvider
from refactra.refactoring_plan import APPLY_PLAN_APPROVAL
from refactra.repository_analysis_worker import (
    WORKFLOW,
    process_repository_analysis,
    process_repository_plan_approval,
)

logger = logging.getLogger("refactra.worker")
WORKER_LOCK = 72341001


def claim_command(
    session_factory, worker_id: str, *, workflow="mock-v0.1", kind="START"
) -> UUID | None:
    # Commit this short claim before executing work; never hold a queue row lock while idle.
    with session_factory.begin() as session:
        now = session.scalar(select(func.now()))
        command = session.scalar(
            select(RunCommand)
            .where(
                RunCommand.run_id.in_(select(Run.id).where(Run.workflow_version == workflow)),
                RunCommand.kind == kind,
                RunCommand.available_at <= now,
                or_(
                    RunCommand.status == "PENDING",
                    and_(RunCommand.status == "CLAIMED", RunCommand.lease_until <= now),
                ),
            )
            .order_by(RunCommand.available_at, RunCommand.created_at, RunCommand.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if command is None:
            return None
        command.status = "CLAIMED"
        command.claimed_by = worker_id
        command.lease_until = now + timedelta(seconds=60)
        command.attempts += 1
        return command.id


def process_command(session_factory, command_id: UUID, worker_id: str) -> bool:
    # This stub has no external effects. Its events, final status and DONE are one transaction.
    # Real workflow execution will need durable LangGraph checkpoints and reconciliation.
    with session_factory.begin() as session:
        command = session.scalar(
            select(RunCommand).where(RunCommand.id == command_id).with_for_update()
        )
        now = session.scalar(select(func.now()))
        if (
            command is None
            or command.status != "CLAIMED"
            or command.claimed_by != worker_id
            or command.lease_until <= now
        ):
            return False
        run = session.scalar(select(Run).where(Run.id == command.run_id).with_for_update())
        if run.workflow_version != "mock-v0.1" or command.kind != "START":
            raise ValueError("The foundation worker only accepts mock START commands.")
        if run.status == "CREATED":
            session.add(
                RunEvent(
                    run_id=run.id,
                    event_type="WORKER_CLAIMED",
                    payload={
                        "message": "Worker claimed the persisted command.",
                        "attempt": command.attempts,
                    },
                )
            )
            run.status = "ANALYZING"
            session.add(
                RunEvent(
                    run_id=run.id,
                    event_type="MOCK_ANALYSIS",
                    payload={
                        "message": "Mock stub executed. No repository or model was accessed.",
                        "status": "ANALYZING",
                        "mode": "mock",
                    },
                )
            )
            run.status = "NEEDS_HUMAN_REVIEW"
            run.version += 2
            run.updated_at = now
            session.add(
                RunEvent(
                    run_id=run.id,
                    event_type="RUN_STOPPED",
                    payload={
                        "code": "mock_boundary",
                        "message": "Queue demonstration finished. Planning, approvals, editing, "
                        "validation and GitHub publication are not implemented yet.",
                    },
                )
            )
        elif run.status not in {"NEEDS_HUMAN_REVIEW", "FAILED"}:
            raise ValueError("Unexpected mock state; refusing to advance it.")
        command.status = "DONE"
        command.completed_at = now
        command.lease_until = None
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Process at most one command and exit.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--repository-analysis", action="store_true", help="Process prepared repository runs."
    )
    modes.add_argument(
        "--repository-approval", action="store_true", help="Verify approved plan handoffs."
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings.from_env()
    provider = None
    mode = "mock"
    kind = "START"
    if args.repository_analysis:
        key = os.getenv("REFACTRA_OPENAI_API_KEY", "").strip()
        if not key:
            raise SystemExit("Repository analysis requires REFACTRA_OPENAI_API_KEY.")
        provider = OpenAIAnalysisProvider(key)
        mode = "repository_analysis"
    elif args.repository_approval:
        mode = "repository_approval"
        kind = APPLY_PLAN_APPROVAL
    elif not settings.enable_mock:
        raise SystemExit("Mock mode is disabled.")
    workflow = "mock-v0.1" if mode == "mock" else WORKFLOW
    engine = make_engine(settings.database_url)
    session_factory = sessions(engine)
    worker_id = str(uuid4())
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    try:
        # A session-scoped advisory lock enforces the current one-worker topology.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as guard:
            if not guard.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK}):
                raise SystemExit("Another Refactra worker holds the local execution slot.")
            logger.info(json.dumps({"event": "worker_started", "mode": mode}))
            while not stopped.is_set():
                guard.execute(text("SELECT 1"))  # Loss of the lock connection stops this worker.
                command_id = claim_command(session_factory, worker_id, workflow=workflow, kind=kind)
                if command_id:
                    if args.repository_approval:
                        processed = process_repository_plan_approval(
                            session_factory, command_id, worker_id
                        )
                    elif provider is None:
                        processed = process_command(session_factory, command_id, worker_id)
                    else:
                        processed = process_repository_analysis(
                            session_factory, command_id, worker_id, provider
                        )
                    logger.info(
                        json.dumps(
                            {
                                "event": "command_processed" if processed else "command_claim_lost",
                                "command_id": str(command_id),
                                "mode": mode,
                            }
                        )
                    )
                if args.once:
                    break
                if command_id is None:
                    stopped.wait(1)
            guard.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": WORKER_LOCK})
    except (SQLAlchemyError, ValueError):
        logger.error(
            json.dumps(
                {
                    "event": "worker_stopped",
                    "reason": "database_or_state_error",
                    "message": "Inspect the database and restart the worker.",
                }
            )
        )
        raise SystemExit(1) from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
