from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from refactra.analysis_context import prepare_analysis_context, save_analysis_context
from refactra.analysis_request import (
    BudgetExceeded,
    BudgetUsage,
    ModelPricing,
    prepare_analysis_request,
)
from refactra.auth_sessions import GitHubIdentity
from refactra.model_reservations import reserve_analysis_request
from refactra.models import ModelReservation, RunAnalysisContext, RunEvent
from refactra.repository_revision import RepositoryRevision
from refactra.repository_runs import create_repository_run
from refactra.repository_snapshot import SourceFile, SourceSnapshot

from .conftest import migration_config


def prepare(session_factory, *, expensive=False, input_tokens=100, output_tokens=100):
    with session_factory.begin() as session:
        run = create_repository_run(
            session,
            identity=GitHubIdentity(123, "fixture"),
            installation_id=77,
            revision=RepositoryRevision(42, "fixture/repo", "main", "main", "a" * 40),
            task_text="Refactor cart",
        )
    snapshot = SourceSnapshot(42, "a" * 40, (SourceFile("src/cart.py", b"def cart(): pass"),))
    context = prepare_analysis_context(run, snapshot)
    with session_factory.begin() as session:
        save_analysis_context(session, context)
    # Synthetic prices/counts: no real model or provider is used.
    rate = Decimal("50") if expensive else Decimal("0")
    return prepare_analysis_request(
        context,
        ModelPricing("fixture", 15000, rate, rate),
        BudgetUsage(),
        monthly_committed_usd=Decimal("0"),
        count_input_tokens=lambda _: 12000 if expensive else input_tokens,
        max_output_tokens=3000 if expensive else output_tokens,
    )


def reserve(session_factory, request, key=None):
    with session_factory.begin() as session:
        return reserve_analysis_request(session, key or uuid4(), request)


def test_restart_retry_reuses_receipt_without_another_allocation_or_event(
    session_factory, database
):
    request, key = prepare(session_factory), uuid4()
    assert reserve(session_factory, request, key).created
    database.dispose()
    assert not reserve(session_factory, request, key).created
    with session_factory() as session:
        row = session.get(ModelReservation, key)
        assert row.run_id == request.run_id and row.budget_month.day == 1
        assert row.input_tokens == row.output_tokens == 100 and len(row.request_hash) == 64
        assert session.scalar(select(func.count()).select_from(ModelReservation)) == 1
        events = session.scalars(
            select(RunEvent).where(RunEvent.event_type == "MODEL_BUDGET_RESERVED")
        ).all()
        assert len(events) == 1 and set(events[0].payload) == {"reservation_id", "maximum_cost_usd"}


def test_rollback_removes_reservation_and_event(session_factory):
    request, key = prepare(session_factory), uuid4()
    with pytest.raises(RuntimeError):
        with session_factory.begin() as session:
            reserve_analysis_request(session, key, request)
            raise RuntimeError("abort")
    with session_factory() as session:
        assert session.get(ModelReservation, key) is None
        assert (
            session.scalar(
                select(func.count()).where(RunEvent.event_type == "MODEL_BUDGET_RESERVED")
            )
            == 0
        )


def test_reused_key_with_changed_request_and_unsaved_input_are_rejected(session_factory):
    request, key = prepare(session_factory), uuid4()
    reserve(session_factory, request, key)
    changed = replace(
        request,
        model_input=replace(
            request.model_input, context_json=request.model_input.context_json + " "
        ),
    )
    with pytest.raises(ValueError, match="different request"):
        reserve(session_factory, changed, key)
    with pytest.raises(ValueError, match="saved context"):
        reserve(session_factory, changed)


@pytest.mark.parametrize(
    "count,tokens,output,expensive,code",
    [
        (8, 100, 100, False, "run_attempts"),
        (1, 100, 100, True, "run_cost"),
        (3, 12000, 100, False, "run_input_tokens"),
        (4, 100, 3000, False, "run_output_tokens"),
    ],
)
def test_stale_preflight_cannot_reset_run_budgets(
    session_factory, count, tokens, output, expensive, code
):
    request = prepare(
        session_factory, expensive=expensive, input_tokens=tokens, output_tokens=output
    )
    for _ in range(count):
        reserve(session_factory, request)
    with pytest.raises(BudgetExceeded, match=code):
        reserve(session_factory, request)


def test_competing_runs_cannot_both_spend_the_last_monthly_allocation(session_factory):
    for _ in range(15):
        reserve(session_factory, prepare(session_factory, expensive=True))
    requests = [prepare(session_factory, expensive=True) for _ in range(2)]

    def compete(request):
        try:
            reserve(session_factory, request)
            return "reserved"
        except BudgetExceeded as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(compete, requests)) == ["monthly_cost", "reserved"]
    with session_factory() as session:
        assert session.scalar(select(func.sum(ModelReservation.maximum_cost_usd))) == Decimal("12")
        assert session.scalar(select(func.count()).select_from(ModelReservation)) == 16


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "AUTOCOMMIT"])
def test_unsupported_isolation_is_rejected_and_migration_preserves_contexts(
    session_factory, database, isolation
):
    request = prepare(session_factory)
    with Session(database.execution_options(isolation_level=isolation)) as session:
        with pytest.raises(ValueError, match="READ COMMITTED"):
            reserve_analysis_request(session, uuid4(), request)
    reserve(session_factory, request)
    with database.begin() as connection:
        config = migration_config(connection)
        command.downgrade(config, "0003_analysis_contexts")
        assert connection.scalar(text("SELECT to_regclass('model_reservations')")) is None
        assert connection.scalar(select(RunAnalysisContext.run_id)) == request.run_id
        command.upgrade(config, "head")
