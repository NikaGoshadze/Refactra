from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from refactra.analysis_context import save_analysis_context
from refactra.api import create_app
from refactra.auth_sessions import GitHubIdentity, issue_session
from refactra.config import Settings
from refactra.models import ModelReservation
from refactra.repository_analysis_worker import WORKFLOW
from refactra.usage_limits import WORKFLOW as COUNTED_WORKFLOW
from refactra.usage_limits import (
    UsageLimitReached,
    UsageLimits,
    _resets,
    check_new_run,
    limit_response,
    read_usage,
)

from .test_analysis_context import prepare

OWNER = 123


def spend(session_factory, run_id, usd):
    with session_factory.begin() as session:
        month = session.scalar(text("SELECT date_trunc('month', timezone('UTC', now()))::date"))
        session.add(
            ModelReservation(
                id=uuid4(),
                run_id=run_id,
                budget_month=month,
                request_hash="h" * 64,
                model="fixture",
                input_tokens=1,
                output_tokens=1,
                maximum_cost_usd=Decimal(usd),
            )
        )


def run_with_context(session_factory):
    run, _, context = prepare(session_factory)
    with session_factory.begin() as session:
        save_analysis_context(session, context)
    return run


def test_limits_come_from_settings_with_safe_defaults(monkeypatch):
    for name in ("REFACTRA_USER_DAILY_RUNS", "REFACTRA_USER_MONTHLY_USD", "REFACTRA_MONTHLY_USD"):
        monkeypatch.delenv(name, raising=False)
    assert UsageLimits.from_env() == UsageLimits(5, Decimal("3"), Decimal("12"))
    monkeypatch.setenv("REFACTRA_USER_DAILY_RUNS", "0")
    with pytest.raises(ValueError, match="positive"):
        UsageLimits.from_env()
    monkeypatch.setenv("REFACTRA_USER_DAILY_RUNS", "five")
    with pytest.raises(ValueError, match="whole number"):
        UsageLimits.from_env()
    assert COUNTED_WORKFLOW == WORKFLOW


def test_usage_counts_todays_runs_and_this_months_cost_for_one_user(session_factory):
    first = run_with_context(session_factory)
    prepare(session_factory)
    yesterday, _, _ = prepare(session_factory)
    with session_factory.begin() as session:
        session.execute(
            text("UPDATE runs SET created_at = now() - interval '1 day' WHERE id = :id"),
            {"id": yesterday.id},
        )
    spend(session_factory, first.id, "0.40")
    limits = UsageLimits(5, Decimal("3"), Decimal("12"))
    with session_factory() as session:
        mine = read_usage(session, OWNER, limits)
        theirs = read_usage(session, 999, limits)
    assert (mine.runs_today, mine.month_cost_usd) == (2, "0.40")
    assert (theirs.runs_today, theirs.month_cost_usd) == (0, "0.00")
    assert mine.day_resets_at > datetime.now(UTC) and mine.month_resets_at.day == 1


@pytest.mark.parametrize(
    "limits,code",
    [
        (UsageLimits(1, Decimal("3"), Decimal("12")), "daily_run_limit"),
        (UsageLimits(5, Decimal("0.50"), Decimal("12")), "user_monthly_cost"),
        (UsageLimits(5, Decimal("3"), Decimal("0.50")), "monthly_cost"),
    ],
)
def test_a_new_run_is_refused_at_each_limit(session_factory, limits, code):
    run = run_with_context(session_factory)
    spend(session_factory, run.id, "0.60")
    with session_factory() as session, pytest.raises(UsageLimitReached) as reached:
        check_new_run(session, OWNER, limits)
    assert reached.value.code == code
    with session_factory() as session:
        check_new_run(session, OWNER, UsageLimits(5, Decimal("3"), Decimal("12")))


def test_the_limit_response_says_when_to_retry():
    reset = datetime(2026, 12, 1, tzinfo=UTC)
    response = limit_response(
        UsageLimitReached("monthly_cost", "Used up.", reset), reset - timedelta(hours=2)
    )
    assert response.status_code == 429
    assert (
        response.headers["retry-after"] == "7200"
        and response.headers["cache-control"] == "no-store"
    )
    assert b'"resets_at":"2026-12-01T00:00:00+00:00"' in response.body


def test_month_and_day_resets_roll_over_the_year():
    day, month = _resets(datetime(2026, 12, 31, 23, 0, tzinfo=UTC))
    assert day == datetime(2027, 1, 1, tzinfo=UTC) and month == datetime(2027, 1, 1, tzinfo=UTC)


def test_the_usage_endpoint_serves_invited_signed_in_users(session_factory, database, monkeypatch):
    monkeypatch.setenv("REFACTRA_ALLOWED_GITHUB_USER_IDS", str(OWNER))
    monkeypatch.setenv("REFACTRA_USER_DAILY_RUNS", "7")
    prepare(session_factory)
    with TestClient(create_app(Settings("unused"), database)) as client:
        assert client.get("/api/v1/usage").status_code == 401
        with session_factory.begin() as session:
            client.cookies.set(
                "refactra_session", issue_session(session, GitHubIdentity(OWNER, "o"))
            )
        response = client.get("/api/v1/usage")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert (body["runs_today"], body["daily_run_limit"], body["monthly_limit_usd"]) == (
        1,
        7,
        "3.00",
    )
