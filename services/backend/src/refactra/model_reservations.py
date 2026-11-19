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

BUDGET_LOCK = 72341002  # Separate from the mock worker's session-scoped execution lock.


@dataclass(frozen=True)
class ReservationReceipt:
    reservation_id: UUID
    created: bool


def reserve_analysis_request(
    session: Session, reservation_id: UUID, request: PreparedAnalysisRequest
) -> ReservationReceipt:
    """Use trusted preflight output; disregard its potentially stale usage snapshots.

    The caller owns a short transaction. All reservations remain held until later
    reconciliation; no timeout, retry or restart releases an unknown charge here.
    """
    if not isinstance(reservation_id, UUID):
        raise ValueError("A stable attempt UUID is required.")
    connection = session.connection()
    if (
        connection.get_isolation_level() != "READ COMMITTED"
        or connection.connection.dbapi_connection.autocommit
    ):
        raise ValueError("Budget reservations require READ COMMITTED transactions.")
    cost = request.maximum_cost_usd.quantize(Decimal("0.000000000001"), rounding=ROUND_CEILING)
    fingerprint = sha256(
        json.dumps(
            {
                "run_id": str(request.run_id),
                "input": asdict(request.model_input),
                "tokens": request.input_tokens,
                "cost": str(cost),
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    # Current one-worker scale: one transaction lock covers all run/month ledger sums.
    session.execute(select(func.pg_advisory_xact_lock(BUDGET_LOCK)))
    existing = session.get(ModelReservation, reservation_id)
    if existing is not None:
        if existing.request_hash != fingerprint:
            raise ValueError("Attempt UUID is already bound to a different request.")
        return ReservationReceipt(reservation_id, False)
    context = load_analysis_context(session, request.run_id)
    if (
        context is None
        or context.document != request.model_input.context_json.encode()
        or request.model_input.instructions != INSTRUCTIONS
    ):
        raise ValueError("Request must use the saved context and trusted analysis instructions.")
    month = (
        session.scalar(select(func.timezone("UTC", func.clock_timestamp()))).date().replace(day=1)
    )
    attempts, inputs, outputs, run_cost = session.execute(
        select(
            func.count(ModelReservation.id),
            func.coalesce(func.sum(ModelReservation.input_tokens), 0),
            func.coalesce(func.sum(ModelReservation.output_tokens), 0),
            func.coalesce(func.sum(ModelReservation.maximum_cost_usd), 0),
        ).where(ModelReservation.run_id == request.run_id)
    ).one()
    month_cost = session.scalar(
        select(func.coalesce(func.sum(ModelReservation.maximum_cost_usd), 0)).where(
            ModelReservation.budget_month == month
        )
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
