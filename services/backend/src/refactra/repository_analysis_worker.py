"""Process read-only repository workflow commands."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from refactra.analysis_context import load_analysis_context
from refactra.analysis_request import BudgetExceeded
from refactra.models import ModelReservation, Run, RunAnalysisResult, RunCommand, RunEvent
from refactra.openai_analysis import (
    AnalysisProviderError,
    AnalysisResult,
    execute_read_only_analysis,
)
from refactra.refactoring_plan import (
    APPLY_PLAN_APPROVAL,
    PlanRevisionConflict,
    approval_command_key,
    load_approved_plan,
)

WORKFLOW = "repository-v0.1"


def enqueue_repository_analysis(session: Session, run_id: UUID) -> UUID:
    """Trusted caller must authorize model use for this repository before queuing."""
    run = session.get(Run, run_id, with_for_update=True)
    if run is None or run.workflow_version != WORKFLOW:
        raise ValueError("A repository run is required.")
    key = f"repository:analysis:{run_id}"
    existing = session.scalar(select(RunCommand.id).where(RunCommand.dedupe_key == key))
    if existing is not None:
        return existing
    if run.status != "CREATED" or load_analysis_context(session, run_id) is None:
        raise ValueError("Only a new repository run with saved analysis context can be queued.")
    command = RunCommand(run_id=run_id, dedupe_key=key)
    session.add(command)
    session.flush()
    return command.id


def _claimed_command(session, command_id, worker_id):
    command = session.get(RunCommand, command_id, with_for_update=True)
    if command is None or command.status != "CLAIMED" or command.claimed_by != worker_id:
        return None
    return command


def _finish(session, command, run, now, *, code, result: AnalysisResult | None = None):
    if result is not None:
        session.add(
            RunAnalysisResult(
                run_id=run.id,
                reservation_id=command.id,
                response_id=result.response_id,
                analysis_text=result.text,
            )
        )
    success = result is not None or code == "analysis_ready"
    run.status = "NEEDS_HUMAN_REVIEW" if success else "FAILED"
    run.version += 1
    run.updated_at = now
    command.status = "DONE" if success else "FAILED"
    command.completed_at = now
    command.lease_until = None
    session.add(RunEvent(run_id=run.id, event_type="RUN_STOPPED", payload={"code": code}))


def process_repository_analysis(
    session_factory, command_id: UUID, worker_id: str, provider
) -> bool:
    with session_factory.begin() as session:
        command = _claimed_command(session, command_id, worker_id)
        now = session.scalar(select(func.now()))
        if command is None or command.lease_until <= now:
            return False
        run = session.get(Run, command.run_id, with_for_update=True)
        run_id = run.id
        if (
            run.workflow_version != WORKFLOW
            or command.kind != "START"
            or command.dedupe_key != f"repository:analysis:{run_id}"
        ):
            raise ValueError("Expected a queued repository analysis command.")
        if session.get(RunAnalysisResult, run_id) is not None:
            _finish(session, command, run, now, code="analysis_ready")
            return True
        receipt = session.get(ModelReservation, command_id)
        if receipt is not None:
            code = (
                "analysis_outcome_unknown"
                if receipt.settled_at is None
                else "analysis_result_missing"
            )
            _finish(session, command, run, now, code=code)
            return True
        if run.status not in {"CREATED", "ANALYZING"}:
            raise ValueError("Unexpected repository analysis state.")
        if run.status == "CREATED":
            run.status = "ANALYZING"
            run.version += 1
            run.updated_at = now
            session.add(RunEvent(run_id=run_id, event_type="ANALYSIS_STARTED", payload={}))
        attempt = command.attempts

    def check_admission(session):
        command = _claimed_command(session, command_id, worker_id)
        if command is None or command.attempts != attempt:
            raise ValueError("Repository analysis claim was lost before admission.")

    result = None
    try:
        result = execute_read_only_analysis(
            session_factory, run_id, command_id, provider, check_admission=check_admission
        )
        code = "analysis_ready"
    except BudgetExceeded:
        code = "analysis_budget_exceeded"
    except AnalysisProviderError:
        code = "analysis_provider_failed"
    except ValueError:
        code = "analysis_input_invalid"

    with session_factory.begin() as session:
        command = _claimed_command(session, command_id, worker_id)
        # A slow call may outlast the lease. Only the same, unreclaimed claim can finish.
        if command is None or command.attempts != attempt:
            return False
        if code == "analysis_provider_failed":
            receipt = session.get(ModelReservation, command_id)
            if receipt is not None and receipt.settled_at is None:
                code = "analysis_outcome_unknown"
        run = session.get(Run, run_id, with_for_update=True)
        _finish(session, command, run, session.scalar(select(func.now())), code=code, result=result)
        return True


def process_repository_plan_approval(session_factory, command_id: UUID, worker_id: str) -> bool:
    """Verify the durable handoff without executing repository changes."""
    with session_factory.begin() as session:
        command = _claimed_command(session, command_id, worker_id)
        now = session.scalar(select(func.now()))
        if command is None or command.kind != APPLY_PLAN_APPROVAL or command.lease_until <= now:
            return False
        run = session.get(Run, command.run_id, with_for_update=True)
        try:
            plan = load_approved_plan(session, run)
            if command.dedupe_key != approval_command_key(run.id, plan.revision):
                raise PlanRevisionConflict("Approval command targets a different revision.")
        except PlanRevisionConflict:
            command.status = "FAILED"
            event_type = "PLAN_APPROVAL_REJECTED"
            payload = {"code": "approval_mismatch"}
        else:
            command.status = "DONE"
            event_type = "PLAN_APPROVAL_VERIFIED"
            payload = {"plan_id": str(plan.id), "revision": plan.revision, "sha256": plan.sha256}
        command.completed_at = now
        command.lease_until = None
        session.add(
            RunEvent(
                run_id=run.id,
                event_type=event_type,
                payload={**payload, "command_id": str(command.id)},
            )
        )
        return True
