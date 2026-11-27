"""Process mock commands, repository analysis, or approved plan handoffs."""

import argparse
import json
import logging
import os
import signal
import sys
import threading
from datetime import timedelta
from pathlib import Path
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import SQLAlchemyError

from refactra.candidate_validation_worker import process_candidate_validation
from refactra.config import Settings
from refactra.db import make_engine, sessions
from refactra.diagnostics import database_error_details
from refactra.models import Run, RunCommand, RunEvent
from refactra.openai_analysis import OpenAIAnalysisProvider
from refactra.production import ProductionConfigError, require_production_ready
from refactra.publication_pr import process_approved_publication
from refactra.publication_target import PUBLISH_CANDIDATE
from refactra.refactoring_plan import APPLY_PLAN_APPROVAL
from refactra.repository_analysis_worker import (
    GENERATE_CANDIDATE,
    GENERATE_PLAN,
    WORKFLOW,
    process_repository_analysis,
    process_repository_plan_approval,
)
from refactra.repository_candidate import VALIDATE_CANDIDATE, process_repository_candidate
from refactra.repository_plan_worker import process_initial_plan
from refactra.repository_validation import reconcile_validation_containers
from refactra.snapshot_preparation import PREPARE_SNAPSHOT, process_snapshot_preparation
from refactra.worker_health import (
    HEARTBEAT_INTERVAL_SECONDS,
    MAX_COMMAND_ATTEMPTS,
    clear_heartbeat,
    fail_claimed_command,
    record_heartbeat,
    stop_command,
    worker_status,
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
        if command.attempts >= MAX_COMMAND_ATTEMPTS:
            # Repeated reclaims mean this command keeps interrupting its worker.
            stop_command(
                session, command, code="command_attempts_exhausted", error_type="Interrupted"
            )
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
    modes.add_argument(
        "--repository-candidate",
        action="store_true",
        help="Process approved candidates and publish when artifact flags are set.",
    )
    modes.add_argument(
        "--repository-workflow",
        action="store_true",
        help="Process the full repository workflow with both artifact flags set.",
    )
    parser.add_argument("--candidate-source", type=Path, help="Trusted local Git clone.")
    parser.add_argument(
        "--candidate-artifacts", type=Path, help="Directory for prepared artifacts."
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="Exit 0 when a worker heartbeat is fresh; for container health checks.",
    )
    args = parser.parse_args()
    if args.health:
        engine = make_engine(Settings.from_env().database_url)
        try:
            with sessions(engine)() as session:
                raise SystemExit(0 if worker_status(session).status == "running" else 1)
        finally:
            engine.dispose()
    if args.repository_workflow or args.candidate_source or args.candidate_artifacts:
        if not (
            (args.repository_candidate or args.repository_workflow)
            and args.candidate_source
            and args.candidate_artifacts
            and args.candidate_source.is_dir()
            and args.candidate_artifacts.is_dir()
        ):
            parser.error(
                "Validation requires a candidate or workflow mode and both existing directories."
            )
    try:
        require_production_ready("worker")
    except ProductionConfigError as error:
        # Exit code 2 tells run-worker.ps1 not to restart: a restart cannot fix settings.
        print(error, file=sys.stderr)
        raise SystemExit(2) from None
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.from_env()
    provider = None
    mode = "mock"
    kind = "START"
    if args.repository_analysis or args.repository_candidate or args.repository_workflow:
        key = os.getenv("REFACTRA_OPENAI_API_KEY", "").strip()
        if not key:
            raise SystemExit("Repository model work requires REFACTRA_OPENAI_API_KEY.")
        provider = OpenAIAnalysisProvider(key)
        mode = "repository_analysis"
    elif args.repository_approval:
        mode = "repository_approval"
        kind = APPLY_PLAN_APPROVAL
    elif not settings.enable_mock:
        raise SystemExit("Mock mode is disabled.")
    kinds = [kind]
    if args.repository_analysis:
        kinds = [PREPARE_SNAPSHOT, "START"]
    if args.repository_candidate or args.repository_workflow:
        # A candidate worker with an artifact directory also validates and publishes.
        mode = (
            "repository_workflow"
            if args.repository_workflow
            else "repository_publication"
            if args.candidate_artifacts
            else "repository_candidate"
        )
        kinds = [GENERATE_CANDIDATE]
        if args.candidate_artifacts:
            kinds.extend((VALIDATE_CANDIDATE, PUBLISH_CANDIDATE))
        kinds.append(APPLY_PLAN_APPROVAL)
        if args.repository_workflow:
            kinds.extend((GENERATE_PLAN, PREPARE_SNAPSHOT, "START"))
    workflow = "mock-v0.1" if mode == "mock" else WORKFLOW
    engine = make_engine(settings.database_url)
    session_factory = sessions(engine)
    worker_id = str(uuid4())

    def dispatch(claimed_kind: str, command_id: UUID) -> bool:
        if claimed_kind == APPLY_PLAN_APPROVAL:
            return process_repository_plan_approval(session_factory, command_id, worker_id)
        if claimed_kind == VALIDATE_CANDIDATE:
            # Remove runners left by interrupted attempts before starting new ones.
            reconcile_validation_containers(session_factory)
            return process_candidate_validation(
                session_factory,
                command_id,
                worker_id,
                source=args.candidate_source,
                artifacts=args.candidate_artifacts,
            )
        if claimed_kind == PUBLISH_CANDIDATE:
            return process_approved_publication(
                session_factory,
                command_id,
                worker_id,
                artifacts=args.candidate_artifacts,
                source=args.candidate_source,
            )
        if claimed_kind == GENERATE_CANDIDATE:
            return process_repository_candidate(session_factory, command_id, worker_id, provider)
        if claimed_kind == PREPARE_SNAPSHOT:
            return process_snapshot_preparation(session_factory, command_id, worker_id)
        if claimed_kind == GENERATE_PLAN:
            return process_initial_plan(session_factory, command_id, worker_id, provider)
        if provider is None:
            return process_command(session_factory, command_id, worker_id)
        return process_repository_analysis(
            session_factory, command_id, worker_id, provider, queue_plan=args.repository_workflow
        )

    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    try:
        # A session-scoped advisory lock enforces the current one-worker topology.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as guard:
            if not guard.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK}):
                raise SystemExit("Another Refactra worker holds the local execution slot.")
            record_heartbeat(session_factory, worker_id, mode, started=True)
            if args.candidate_artifacts:
                reconcile_validation_containers(session_factory)
            last_beat = monotonic()
            logger.info(json.dumps({"event": "worker_started", "mode": mode}))
            while not stopped.is_set():
                guard.execute(text("SELECT 1"))  # Loss of the lock connection stops this worker.
                if monotonic() - last_beat >= HEARTBEAT_INTERVAL_SECONDS:
                    record_heartbeat(session_factory, worker_id, mode)
                    last_beat = monotonic()
                for claimed_kind in kinds:
                    command_id = claim_command(
                        session_factory, worker_id, workflow=workflow, kind=claimed_kind
                    )
                    if command_id is not None:
                        break
                if command_id:
                    started = monotonic()
                    event = "command_processed"
                    try:
                        if not dispatch(claimed_kind, command_id):
                            event = "command_claim_lost"
                    except SQLAlchemyError:
                        raise
                    except Exception as error:
                        # Isolate a deterministic failure to its command; keep serving others.
                        event = "command_failed"
                        fail_claimed_command(
                            session_factory, command_id, worker_id, type(error).__name__
                        )
                    logger.info(
                        json.dumps(
                            {
                                "event": event,
                                "command_id": str(command_id),
                                "mode": mode,
                                "stage": claimed_kind,
                                "duration_ms": round((monotonic() - started) * 1000),
                            }
                        )
                    )
                    record_heartbeat(session_factory, worker_id, mode)
                    last_beat = monotonic()
                if args.once:
                    break
                if command_id is None:
                    stopped.wait(1)
            clear_heartbeat(session_factory, worker_id)
            guard.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": WORKER_LOCK})
    except SQLAlchemyError as error:
        logger.error(
            json.dumps(
                {
                    "event": "worker_stopped",
                    "reason": "database_error",
                    **database_error_details(error),
                    "message": "Check the database; a supervised worker restarts automatically.",
                }
            )
        )
        raise SystemExit(1) from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
