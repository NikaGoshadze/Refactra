"""Durable admission only. Commit before transport; a replay receipt never permits resending."""

import json
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, Decimal
from hashlib import sha256
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from refactra.analysis_context import load_analysis_context
from refactra.analysis_request import (
    INSTRUCTIONS,
    MAX_MONTH_USD,
    MAX_RUN_ATTEMPTS,
    MAX_RUN_INPUT,
    MAX_RUN_OUTPUT,
    MAX_RUN_USD,
    BudgetExceeded,
    PreparedAnalysisRequest,
)
from refactra.models import ModelReservation, RunEvent
from refactra.planning_request import PLAN_INSTRUCTIONS, PLAN_TEXT_CONFIG, load_planning_context

BUDGET_LOCK = 72341002  # Separate from the mock worker's session-scoped execution lock.


@dataclass(frozen=True)
class ReservationReceipt:
    reservation_id: UUID
    created: bool


def _lock_budget(session: Session) -> None:
    connection = session.connection()
    if (
        connection.get_isolation_level() != "READ COMMITTED"
        or connection.connection.dbapi_connection.autocommit
    ):
        raise ValueError("Budget reservations require READ COMMITTED transactions.")
    session.execute(select(func.pg_advisory_xact_lock(BUDGET_LOCK)))


def reserve_analysis_request(
    session: Session,
    reservation_id: UUID,
    request: PreparedAnalysisRequest,
    *,
    ledger_limit_usd: Decimal | None = None,
) -> ReservationReceipt:
    """Use trusted preflight output; disregard its potentially stale usage snapshots.

    The caller owns a short transaction. No timeout, retry or restart releases an
    unknown charge; only confirmed usage can replace a hold in budget calculations.
    An optional ledger limit also caps lifetime effective cost across this schema,
    under the same lock; use a dedicated schema for benchmark-wide admission.
    """
    if not isinstance(reservation_id, UUID):
        raise ValueError("A stable attempt UUID is required.")
    if ledger_limit_usd is not None and (
        not isinstance(ledger_limit_usd, Decimal)
        or not ledger_limit_usd.is_finite()
        or ledger_limit_usd <= 0
    ):
        raise ValueError("Ledger limit must be a finite positive Decimal.")
    if request.pricing.model != request.model_input.model:
        raise ValueError("Pricing must describe the requested model.")
    pricing = {
        "input": str(request.pricing.input_usd_per_million),
        "output": str(request.pricing.output_usd_per_million),
    }
    cost = request.maximum_cost_usd.quantize(Decimal("0.000000000001"), rounding=ROUND_CEILING)
    expected = (
        (
            request.input_tokens * request.pricing.input_usd_per_million
            + request.model_input.max_output_tokens * request.pricing.output_usd_per_million
        )
        / Decimal(1_000_000)
    ).quantize(Decimal("0.000000000001"), rounding=ROUND_CEILING)
    if cost != expected:
        raise ValueError("Request estimate must match its saved pricing.")
    model_input = asdict(request.model_input)
    if request.model_input.instructions == PLAN_INSTRUCTIONS:
        model_input["text"] = PLAN_TEXT_CONFIG
    fingerprint = sha256(
        json.dumps(
            {
                "run_id": str(request.run_id),
                "input": model_input,
                "tokens": request.input_tokens,
                "cost": str(cost),
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    # Current one-worker scale: one transaction lock covers all run/month ledger sums.
    _lock_budget(session)
    existing = session.get(ModelReservation, reservation_id)
    if existing is not None:
        if existing.request_hash != fingerprint or (
            existing.pricing is not None and existing.pricing != pricing
        ):
            raise ValueError("Attempt UUID is already bound to a different request.")
        return ReservationReceipt(reservation_id, False)
    if ledger_limit_usd is not None:
        committed = session.scalar(
            select(
                func.coalesce(
                    func.sum(
                        func.coalesce(
                            ModelReservation.actual_cost_usd, ModelReservation.maximum_cost_usd
                        )
                    ),
                    0,
                )
            )
        )
        if committed + cost > ledger_limit_usd:
            raise BudgetExceeded("benchmark_cost")
    if request.model_input.instructions == INSTRUCTIONS:
        context = load_analysis_context(session, request.run_id)
    elif request.model_input.instructions == PLAN_INSTRUCTIONS:
        context = load_planning_context(session, request.run_id)
    else:
        raise ValueError("Request must use trusted analysis or planning instructions.")
    if context is None or context.document != request.model_input.context_json.encode():
        raise ValueError("Request must match the saved context for this step.")
    month = (
        session.scalar(select(func.timezone("UTC", func.clock_timestamp()))).date().replace(day=1)
    )
    attempts, inputs, outputs, run_cost = session.execute(
        select(
            func.count(ModelReservation.id),
            func.coalesce(
                func.sum(
                    func.coalesce(
                        ModelReservation.actual_input_tokens, ModelReservation.input_tokens
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    func.coalesce(
                        ModelReservation.actual_output_tokens, ModelReservation.output_tokens
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    func.coalesce(
                        ModelReservation.actual_cost_usd, ModelReservation.maximum_cost_usd
                    )
                ),
                0,
            ),
        ).where(ModelReservation.run_id == request.run_id)
    ).one()
    month_cost = session.scalar(
        select(
            func.coalesce(
                func.sum(
                    func.coalesce(
                        ModelReservation.actual_cost_usd, ModelReservation.maximum_cost_usd
                    )
                ),
                0,
            )
        ).where(ModelReservation.budget_month == month)
    )
    checks = (
        (attempts < MAX_RUN_ATTEMPTS, "run_attempts"),
        (inputs + request.input_tokens <= MAX_RUN_INPUT, "run_input_tokens"),
        (outputs + request.model_input.max_output_tokens <= MAX_RUN_OUTPUT, "run_output_tokens"),
        (run_cost + cost <= MAX_RUN_USD, "run_cost"),
        (month_cost + cost <= MAX_MONTH_USD, "monthly_cost"),
    )
    for allowed, code in checks:
        if not allowed:
            raise BudgetExceeded(code)
    session.add(
        ModelReservation(
            id=reservation_id,
            run_id=request.run_id,
            budget_month=month,
            request_hash=fingerprint,
            model=request.model_input.model,
            input_tokens=request.input_tokens,
            output_tokens=request.model_input.max_output_tokens,
            maximum_cost_usd=cost,
            pricing=pricing,
        )
    )
    session.add(
        RunEvent(
            run_id=request.run_id,
            event_type="MODEL_BUDGET_RESERVED",
            payload={"reservation_id": str(reservation_id), "maximum_cost_usd": str(cost)},
        )
    )
    session.flush()
    return ReservationReceipt(reservation_id, True)


def settle_model_usage(
    session: Session, reservation_id: UUID, *, input_tokens: int, output_tokens: int
) -> Decimal:
    """Only trusted, confirmed provider usage; unknown outcomes must retain their holds.

    Cost is an estimate at original uncached rates, not a provider invoice. Record
    overruns honestly so future admission sees them. Caller commits usage/event together.
    """
    if any(type(value) is not int or value < 0 for value in (input_tokens, output_tokens)):
        raise ValueError("Confirmed usage requires nonnegative integer token counts.")
    _lock_budget(session)
    row = session.get(ModelReservation, reservation_id, populate_existing=True)
    if row is None or row.pricing is None:
        raise ValueError("A reservation with saved pricing is required.")
    if row.actual_input_tokens is not None:
        if (row.actual_input_tokens, row.actual_output_tokens) != (input_tokens, output_tokens):
            raise ValueError("Conflicting confirmed usage cannot replace an earlier settlement.")
        return row.actual_cost_usd
    cost = (
        (
            input_tokens * Decimal(row.pricing["input"])
            + output_tokens * Decimal(row.pricing["output"])
        )
        / Decimal(1_000_000)
    ).quantize(Decimal("0.000000000001"), rounding=ROUND_CEILING)
    row.actual_input_tokens, row.actual_output_tokens, row.actual_cost_usd = (
        input_tokens,
        output_tokens,
        cost,
    )
    row.settled_at = session.scalar(select(func.now()))
    session.add(
        RunEvent(
            run_id=row.run_id,
            event_type="MODEL_USAGE_RECORDED",
            payload={
                "reservation_id": str(row.id),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_cost_usd": str(cost),
                "over_reservation": cost > row.maximum_cost_usd
                or input_tokens > row.input_tokens
                or output_tokens > row.output_tokens,
            },
        )
    )
    session.flush()
    return cost
