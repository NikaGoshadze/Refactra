from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from refactra.analysis_request import BudgetExceeded
from refactra.model_reservations import settle_model_usage
from refactra.models import ModelReservation, RunEvent

from .conftest import migration_config
from .test_model_reservations import prepare, reserve


def settle(factory, key, inputs=0, outputs=0):
    with factory.begin() as session:
        return settle_model_usage(session, key, input_tokens=inputs, output_tokens=outputs)


def test_known_usage_uses_saved_prices_and_retries_survive_restart(session_factory, database):
    request, key = prepare(session_factory, expensive=True), uuid4()
    reserve(session_factory, request, key)
    assert settle(session_factory, key, 12000, 1000) == Decimal("0.65")
    database.dispose()
    assert settle(session_factory, key, 12000, 1000) == Decimal("0.65")
    assert not reserve(session_factory, request, key).created
    with session_factory() as session:
        row = session.get(ModelReservation, key)
        assert row.pricing == {"input": "50", "output": "50"}
        assert row.maximum_cost_usd == Decimal("0.75") and row.actual_output_tokens == 1000
        assert row.settled_at is not None
        events = session.scalars(
            select(RunEvent).where(RunEvent.event_type == "MODEL_USAGE_RECORDED")
        ).all()
        assert len(events) == 1 and not events[0].payload["over_reservation"]


def test_zero_usage_releases_tokens_and_cost_but_never_attempts(session_factory):
    request = prepare(session_factory, expensive=True)
    for _ in range(8):
        key = uuid4()
        reserve(session_factory, request, key)
        assert settle(session_factory, key) == 0
    with pytest.raises(BudgetExceeded, match="run_attempts"):
        reserve(session_factory, request)


def test_unknown_usage_keeps_hold_and_rollbacks_do_not_release_it(session_factory):
    request, key = prepare(session_factory, expensive=True), uuid4()
    reserve(session_factory, request, key)
    with pytest.raises(RuntimeError):
        with session_factory.begin() as session:
            settle_model_usage(session, key, input_tokens=0, output_tokens=0)
            raise RuntimeError("abort")
    with pytest.raises(BudgetExceeded, match="run_cost"):
        reserve(session_factory, request)
    with session_factory() as session:
        assert session.get(ModelReservation, key).settled_at is None
        assert (
            session.scalar(
                select(func.count()).where(RunEvent.event_type == "MODEL_USAGE_RECORDED")
            )
            == 0
        )


def test_monthly_admission_uses_actual_estimates_after_settlement(session_factory):
    keys = []
    for _ in range(16):
        key = uuid4()
        reserve(session_factory, prepare(session_factory, expensive=True), key)
        keys.append(key)
    next_request = prepare(session_factory, expensive=True)
    with pytest.raises(BudgetExceeded, match="monthly_cost"):
        reserve(session_factory, next_request)
    settle(session_factory, keys[0])
    assert reserve(session_factory, next_request).created


def test_overruns_are_recorded_honestly_and_stop_later_admission(session_factory):
    request, key = prepare(session_factory, expensive=True), uuid4()
    reserve(session_factory, request, key)
    assert settle(session_factory, key, 16000, 3000) == Decimal("0.95")
    with pytest.raises(BudgetExceeded, match="run_cost"):
        reserve(session_factory, request)
    with session_factory() as session:
        event = session.scalar(
            select(RunEvent).where(RunEvent.event_type == "MODEL_USAGE_RECORDED")
        )
        assert event.payload["over_reservation"] is True


def test_concurrent_conflicting_settlements_cannot_overwrite_cached_rows(session_factory):
    key = uuid4()
    reserve(session_factory, prepare(session_factory, expensive=True), key)
    barrier = Barrier(2)

    def compete(tokens):
        with session_factory.begin() as session:
            cached = session.get(ModelReservation, key)
            assert cached.actual_input_tokens is None
            barrier.wait(timeout=10)
            try:
                settle_model_usage(session, key, input_tokens=tokens, output_tokens=0)
                return "settled"
            except ValueError:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(compete, [100, 200])) == ["conflict", "settled"]
    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count()).where(RunEvent.event_type == "MODEL_USAGE_RECORDED")
            )
            == 1
        )


@pytest.mark.parametrize("tokens", [None, -1, True, 1.5])
def test_unknown_or_invalid_counts_cannot_release_holds(session_factory, tokens):
    key = uuid4()
    reserve(session_factory, prepare(session_factory), key)
    with pytest.raises(ValueError, match="integer"):
        settle(session_factory, key, tokens)


def test_pricing_changes_and_partial_settlements_are_rejected(session_factory):
    request, key = prepare(session_factory, expensive=True), uuid4()
    reserve(session_factory, request, key)
    changed = replace(request, pricing=replace(request.pricing, input_usd_per_million=Decimal("1")))
    with pytest.raises(ValueError, match="pricing"):
        reserve(session_factory, changed)
    with pytest.raises(IntegrityError):
        with session_factory.begin() as session:
            session.get(ModelReservation, key).actual_input_tokens = 0


def test_migration_preserves_old_holds_without_guessing_prices(session_factory, database):
    request, key = prepare(session_factory, expensive=True), uuid4()
    reserve(session_factory, request, key)
    with database.begin() as connection:
        config = migration_config(connection)
        command.downgrade(config, "0004_model_reservations")
        assert connection.scalar(select(ModelReservation.maximum_cost_usd)) == Decimal("0.75")
        command.upgrade(config, "head")
    assert not reserve(session_factory, request, key).created
    with pytest.raises(ValueError, match="saved pricing"):
        settle(session_factory, key)
