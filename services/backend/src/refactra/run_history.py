"""GET /api/v1/runs: the signed-in user's own runs, newest first, for the history page.

It reads only Refactra's records of runs the user started, so it needs no GitHub call; opening
a run still rechecks repository access. Stages are derived from saved artifacts and events.
"""

import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.orm import Session

from refactra.github_auth import GitHubOAuthConfig
from refactra.models import (
    ModelReservation,
    Run,
    RunCandidateResult,
    RunCommand,
    RunEvent,
    RunPlan,
    RunPlanApproval,
)
from refactra.publication_target import PUBLICATION_APPROVED, PUBLICATION_PR_OPENED
from refactra.repository_access import RepositoryAccessPolicy
from refactra.repository_analysis_worker import WORKFLOW
from refactra.run_stages import ANALYSIS_FAILURE_MESSAGES, CANDIDATE_STAGES

Stage = Literal[
    "analyzing", "plan_ready", "generating", "ready_to_publish", "publishing", "published", "failed"
]
MAX_PAGE = 50
TASK_SUMMARY = 200


class PullRequestLink(BaseModel):
    number: int
    url: str


class RunSummary(BaseModel):
    run_id: UUID
    repository_id: int
    repository_full_name: str | None
    base_branch: str
    base_sha: str
    task_summary: str
    stage: Stage
    stop_reason: str | None
    plan_revisions: int
    pull_request: PullRequestLink | None
    cost_usd: str
    created_at: datetime
    last_activity_at: datetime


class RunHistoryPage(BaseModel):
    runs: list[RunSummary]
    next_cursor: str | None


def _encode_cursor(run: Run) -> str:
    raw = json.dumps([run.created_at.isoformat(), str(run.id)]).encode()
    return urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        created, run_id = json.loads(urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        return datetime.fromisoformat(created), UUID(run_id)
    except (ValueError, TypeError):
        raise HTTPException(
            400, {"code": "invalid_cursor", "message": "Start the history again."}
        ) from None


def list_runs(
    session: Session,
    user_id: int,
    *,
    limit: int = 20,
    cursor: str | None = None,
    query: str | None = None,
) -> RunHistoryPage:
    statement = select(Run).where(Run.requested_by == user_id, Run.workflow_version == WORKFLOW)
    if query:
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        statement = statement.where(
            or_(
                Run.task_text.ilike(pattern, escape="\\"),
                Run.repository_full_name.ilike(pattern, escape="\\"),
            )
        )
    if cursor:
        created, run_id = _decode_cursor(cursor)
        statement = statement.where(tuple_(Run.created_at, Run.id) < tuple_(created, run_id))
    runs = session.scalars(
        statement.order_by(Run.created_at.desc(), Run.id.desc()).limit(limit + 1)
    ).all()
    page, more = runs[:limit], len(runs) > limit
    return RunHistoryPage(
        runs=summarize_runs(session, page),
        next_cursor=_encode_cursor(page[-1]) if more else None,
    )


def summarize_runs(session: Session, page: list[Run]) -> list[RunSummary]:
    """Summaries for already-authorized runs, with a few batched queries per page."""
    ids = [run.id for run in page]
    if not ids:
        return []
    revisions = dict(
        session.execute(
            select(RunPlan.run_id, func.max(RunPlan.revision))
            .where(RunPlan.run_id.in_(ids))
            .group_by(RunPlan.run_id)
        ).all()
    )
    approved = set(
        session.scalars(select(RunPlanApproval.run_id).where(RunPlanApproval.run_id.in_(ids)))
    )
    candidates = set(
        session.scalars(
            select(RunCandidateResult.run_id).where(RunCandidateResult.run_id.in_(ids)).distinct()
        )
    )
    costs = dict(
        session.execute(
            select(
                ModelReservation.run_id,
                func.sum(
                    func.coalesce(
                        ModelReservation.actual_cost_usd, ModelReservation.maximum_cost_usd
                    )
                ),
            )
            .where(ModelReservation.run_id.in_(ids))
            .group_by(ModelReservation.run_id)
        ).all()
    )
    activity = dict(
        session.execute(
            select(RunEvent.run_id, func.max(RunEvent.occurred_at))
            .where(RunEvent.run_id.in_(ids))
            .group_by(RunEvent.run_id)
        ).all()
    )
    events: dict[UUID, dict[str, dict]] = {}
    for run_id, kind, payload in session.execute(
        select(RunEvent.run_id, RunEvent.event_type, RunEvent.payload)
        .where(
            RunEvent.run_id.in_(ids),
            RunEvent.event_type.in_(["RUN_STOPPED", PUBLICATION_APPROVED, PUBLICATION_PR_OPENED]),
        )
        .order_by(RunEvent.id)
    ):
        events.setdefault(run_id, {})[kind] = payload if isinstance(payload, dict) else {}
    # The most recent failed candidate step per run; later rows overwrite earlier ones.
    failed_commands = dict(
        session.execute(
            select(RunCommand.run_id, RunCommand.kind)
            .where(
                RunCommand.run_id.in_(ids),
                RunCommand.kind.in_(list(CANDIDATE_STAGES)),
                RunCommand.status == "FAILED",
            )
            .order_by(RunCommand.created_at, RunCommand.id)
        ).all()
    )

    summaries = []
    for run in page:
        seen = events.get(run.id, {})
        opened = seen.get(PUBLICATION_PR_OPENED)
        pull_request = None
        stop_reason = None
        if opened and type(opened.get("pull_request_number")) is int:
            stage: Stage = "published"
            pull_request = PullRequestLink(
                number=opened["pull_request_number"], url=str(opened.get("pull_request_url", ""))
            )
        elif run.status == "FAILED":
            # Analysis records RUN_STOPPED on success too; only the status marks a failure.
            stage = "failed"
            code = seen.get("RUN_STOPPED", {}).get("code")
            stop_reason = ANALYSIS_FAILURE_MESSAGES.get(code, "Analysis could not be completed.")
        elif run.id in failed_commands:
            stage = "failed"
            stop_reason = CANDIDATE_STAGES[failed_commands[run.id]][1]
        elif PUBLICATION_APPROVED in seen:
            stage = "publishing"
        elif run.id in candidates:
            stage = "ready_to_publish"
        elif run.id in approved:
            stage = "generating"
        elif run.id in revisions:
            stage = "plan_ready"
        else:
            stage = "analyzing"
        task = " ".join(run.task_text.split())
        summaries.append(
            RunSummary(
                run_id=run.id,
                repository_id=run.github_repository_id,
                repository_full_name=run.repository_full_name,
                base_branch=run.base_branch,
                base_sha=run.base_sha,
                task_summary=task if len(task) <= TASK_SUMMARY else task[: TASK_SUMMARY - 1] + "…",
                stage=stage,
                stop_reason=stop_reason,
                plan_revisions=revisions.get(run.id, 0),
                pull_request=pull_request,
                cost_usd=f"{Decimal(costs.get(run.id) or 0):.4f}",
                created_at=run.created_at,
                last_activity_at=max(run.created_at, activity.get(run.id) or run.created_at),
            )
        )
    return summaries


class TimelineEntry(BaseModel):
    at: datetime
    label: str
    link: str | None = None


class RunDetail(BaseModel):
    summary: RunSummary
    task_text: str
    timeline: list[TimelineEntry]


def _revision(payload: dict) -> str:
    revision = payload.get("revision")
    return f" {revision}" if type(revision) is int else ""


def _timeline_entry(kind: str, payload: dict, succeeded: bool) -> tuple[str, str | None] | None:
    """A plain-language label for events users care about; internal events return None."""
    if kind == "RUN_STOPPED":
        code = payload.get("code")
        if not succeeded and code in ANALYSIS_FAILURE_MESSAGES:
            return f"Analysis stopped: {ANALYSIS_FAILURE_MESSAGES[code]}", None
        return "Analysis finished", None
    if kind == "PUBLICATION_PR_OPENED":
        number, url = payload.get("pull_request_number"), payload.get("pull_request_url")
        if type(number) is int and isinstance(url, str) and url.startswith("https://github.com/"):
            return f"Draft PR #{number} opened", url
        return "Draft PR opened", None
    labels = {
        "RUN_CREATED": "Run started",
        "ANALYSIS_CONTEXT_SAVED": "Repository snapshot prepared",
        "ANALYSIS_STARTED": "Analysis started",
        "PLAN_SAVED": f"Plan revision{_revision(payload)} saved",
        "PLAN_FEEDBACK_REQUESTED": "Plan changes requested",
        "PLAN_GENERATION_FAILED": "Plan generation failed",
        "PLAN_APPROVED": f"Plan revision{_revision(payload)} approved",
        "CANDIDATE_GENERATION_FAILED": "Change generation failed",
        "CANDIDATE_VALIDATION_REPAIR_QUEUED": "A failed check is being repaired",
        "CANDIDATE_RESULT_SAVED": "Change generated and validated",
        "CANDIDATE_REVIEW_READY": "Ready for publication review",
        "PUBLICATION_CHECK_WAIVED": "A required check was waived",
        "PUBLICATION_APPROVED": "Publication approved",
        "PUBLICATION_RETRY_SCHEDULED": "Publication will be retried",
        "PUBLICATION_BRANCH_PUSHED": "Branch pushed to GitHub",
        "COMMAND_FAILED": "A worker step failed",
    }
    label = labels.get(kind)
    return (label, None) if label else None


def load_run_detail(session: Session, user_id: int, run_id: UUID) -> RunDetail | None:
    """The full record of one of the user's runs; the caller has rechecked access."""
    run = session.scalar(
        select(Run).where(
            Run.id == run_id, Run.requested_by == user_id, Run.workflow_version == WORKFLOW
        )
    )
    if run is None:
        return None
    timeline = []
    for kind, payload, at in session.execute(
        select(RunEvent.event_type, RunEvent.payload, RunEvent.occurred_at)
        .where(RunEvent.run_id == run_id)
        .order_by(RunEvent.id)
    ):
        entry = _timeline_entry(
            kind, payload if isinstance(payload, dict) else {}, run.status != "FAILED"
        )
        if entry is not None:
            timeline.append(TimelineEntry(at=at, label=entry[0], link=entry[1]))
    (summary,) = summarize_runs(session, [run])
    return RunDetail(summary=summary, task_text=run.task_text, timeline=timeline)


def create_run_history_router(session_factory) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["Run history"])
    oauth = GitHubOAuthConfig.from_env()

    @router.get("/runs", response_model=RunHistoryPage)
    def run_history(
        request: Request,
        limit: int = Query(20, ge=1, le=MAX_PAGE),
        cursor: str | None = Query(None, max_length=200),
        q: str | None = Query(None, max_length=100),
    ):
        try:
            with session_factory() as session:
                identity = RepositoryAccessPolicy.from_env().require_allowed_user(
                    session, request.cookies.get(oauth.session_cookie_name)
                )
                page = list_runs(
                    session, identity.user_id, limit=limit, cursor=cursor, query=(q or "").strip()
                )
        except HTTPException as exc:
            return JSONResponse(
                {"error": exc.detail},
                status_code=exc.status_code,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(page.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    return router
