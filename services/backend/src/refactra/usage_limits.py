"""Per-user and overall spending limits, and GET /api/v1/usage for the account bar.

Days and months are UTC. Cost is the same ledger the model reservations use: settled usage, or
the reserved maximum while a call is unsettled. New runs are refused at submission when a limit
is reached; every model reservation also rechecks the owner's monthly cost (model_reservations).
"""

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from refactra.github_auth import GitHubOAuthConfig
from refactra.models import ModelReservation, Run
from refactra.repository_access import RepositoryAccessPolicy

# Same as repository_analysis_worker.WORKFLOW; importing it here would create an import cycle
# through model_reservations, so a test keeps the two equal.
WORKFLOW = "repository-v0.1"
DEFAULTS = {
    "REFACTRA_USER_DAILY_RUNS": "5",
    "REFACTRA_USER_MONTHLY_USD": "3",
    "REFACTRA_MONTHLY_USD": "12",
}


@dataclass(frozen=True)
class UsageLimits:
    daily_runs: int
    user_monthly_usd: Decimal
    monthly_usd: Decimal

    @classmethod
    def from_env(cls) -> "UsageLimits":
        values = {
            name: os.getenv(name, "").strip() or default for name, default in DEFAULTS.items()
        }
        try:
            runs = int(values["REFACTRA_USER_DAILY_RUNS"])
            user, total = (
                Decimal(values["REFACTRA_USER_MONTHLY_USD"]),
                Decimal(values["REFACTRA_MONTHLY_USD"]),
            )
        except (ValueError, InvalidOperation):
            raise ValueError(
                "Usage limits must be a whole number of runs and dollar amounts."
            ) from None
        if runs < 1 or not all(value.is_finite() and value > 0 for value in (user, total)):
            raise ValueError("Usage limits must be positive.")
        return cls(runs, user, total)


class UsageLimitReached(Exception):
    def __init__(self, code: str, message: str, resets_at: datetime):
        super().__init__(message)
        self.code, self.message, self.resets_at = code, message, resets_at


def _spent():
    return func.coalesce(
        func.sum(
            func.coalesce(ModelReservation.actual_cost_usd, ModelReservation.maximum_cost_usd)
        ),
        0,
    )


def _now(session: Session) -> datetime:
    """The database's current UTC time, timezone-aware."""
    return session.scalar(select(func.timezone("UTC", func.clock_timestamp()))).replace(tzinfo=UTC)


def _day_start(now: datetime) -> datetime:
    """Midnight UTC as a naive value, to compare with timezone('UTC', created_at)."""
    return datetime(now.year, now.month, now.day)


def month_start(now: datetime) -> date:
    return now.date().replace(day=1)


def monthly_cost(session: Session, month: date, user_id: int | None = None) -> Decimal:
    """Committed cost this month, for one user's runs or for everyone."""
    statement = select(_spent()).where(ModelReservation.budget_month == month)
    if user_id is not None:
        statement = statement.join(Run, Run.id == ModelReservation.run_id).where(
            Run.requested_by == user_id
        )
    return Decimal(session.scalar(statement))


class UsageRead(BaseModel):
    runs_today: int
    daily_run_limit: int
    month_cost_usd: str
    monthly_limit_usd: str
    day_resets_at: datetime
    month_resets_at: datetime


def _resets(now: datetime) -> tuple[datetime, datetime]:
    day = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)
    month = datetime(now.year + now.month // 12, now.month % 12 + 1, 1, tzinfo=UTC)
    return day, month


def read_usage(session: Session, user_id: int, limits: UsageLimits) -> UsageRead:
    now = _now(session)
    day_reset, month_reset = _resets(now)
    runs = session.scalar(
        select(func.count(Run.id)).where(
            Run.requested_by == user_id,
            Run.workflow_version == WORKFLOW,
            func.timezone("UTC", Run.created_at) >= _day_start(now),
        )
    )
    return UsageRead(
        runs_today=runs,
        daily_run_limit=limits.daily_runs,
        month_cost_usd=f"{monthly_cost(session, month_start(now), user_id):.2f}",
        monthly_limit_usd=f"{limits.user_monthly_usd:.2f}",
        day_resets_at=day_reset,
        month_resets_at=month_reset,
    )


def check_new_run(session: Session, user_id: int, limits: UsageLimits) -> None:
    """Raise UsageLimitReached when the user may not start another run now."""
    usage = read_usage(session, user_id, limits)
    if usage.runs_today >= limits.daily_runs:
        raise UsageLimitReached(
            "daily_run_limit",
            f"You have started {limits.daily_runs} runs today, the daily limit. "
            "You can start more after midnight UTC.",
            usage.day_resets_at,
        )
    if Decimal(usage.month_cost_usd) >= limits.user_monthly_usd:
        raise UsageLimitReached(
            "user_monthly_cost",
            f"You have used this month's ${limits.user_monthly_usd:.2f} model allowance. "
            "It renews on the first of next month (UTC).",
            usage.month_resets_at,
        )
    if monthly_cost(session, month_start(_now(session))) >= limits.monthly_usd:
        raise UsageLimitReached(
            "monthly_cost",
            "Refactra has used this month's model budget. New runs can start next month.",
            usage.month_resets_at,
        )


def limit_response(error: UsageLimitReached, now: datetime) -> JSONResponse:
    seconds = max(1, int((error.resets_at - now).total_seconds()))
    return JSONResponse(
        status_code=429,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "resets_at": error.resets_at.isoformat(),
            }
        },
        headers={"Cache-Control": "no-store", "Retry-After": str(seconds)},
    )


def create_usage_router(session_factory) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["Usage"])
    oauth = GitHubOAuthConfig.from_env()
    UsageLimits.from_env()  # Invalid limits stop the API at startup; requests reread them.

    @router.get("/usage", response_model=UsageRead)
    def usage(request: Request):
        try:
            with session_factory() as session:
                identity = RepositoryAccessPolicy.from_env().require_allowed_user(
                    session, request.cookies.get(oauth.session_cookie_name)
                )
                body = read_usage(session, identity.user_id, UsageLimits.from_env())
        except HTTPException as exc:
            return JSONResponse(
                {"error": exc.detail},
                status_code=exc.status_code,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(body.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    return router
