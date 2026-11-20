import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from uuid import uuid4

import pytest

from refactra.analysis_context import prepare_analysis_context
from refactra.analysis_request import (
    INSTRUCTIONS,
    BudgetExceeded,
    BudgetUsage,
    ModelPricing,
    prepare_analysis_request,
)
from refactra.models import Run
from refactra.repository_snapshot import SourceFile, SourceSnapshot

# Synthetic rates/counts only; these fixtures do not describe a real provider.
PRICING = ModelPricing("fixture-model", 20_000, Decimal("2"), Decimal("4"))
EMPTY_USAGE = BudgetUsage()


@pytest.fixture
def context():
    run = Run(
        id=uuid4(),
        workflow_version="repository-v0.1",
        github_repository_id=42,
        base_sha="a" * 40,
        task_text="Refactor cart",
    )
    snapshot = SourceSnapshot(
        42, "a" * 40, (SourceFile("src/cart.py", b"def cart(): pass\n# source-marker"),)
    )
    return prepare_analysis_context(run, snapshot)


def prepare(
    context, *, pricing=PRICING, usage=EMPTY_USAGE, tokens=1000, monthly=Decimal("1"), output=100
):
    return prepare_analysis_request(
        context,
        pricing,
        usage,
        monthly_committed_usd=monthly,
        count_input_tokens=lambda request: tokens,
        max_output_tokens=output,
    )


def test_full_input_reaches_the_counter_and_reservation_uses_exact_decimal_math(context):
    usage = BudgetUsage(2, 5000, 2000, Decimal("0.10"))
    seen = []

    def count(request):
        seen.append(request)
        assert request.instructions == INSTRUCTIONS and "untrusted" in request.instructions
        assert "source-marker" in json.loads(request.context_json)["content"]["files"][0]["text"]
        assert request.model == "fixture-model" and request.max_output_tokens == 100
        return 1000

    request = prepare_analysis_request(
        context,
        PRICING,
        usage,
        monthly_committed_usd=Decimal("1"),
        count_input_tokens=count,
        max_output_tokens=100,
    )
    assert request.run_id == context.run_id and len(seen) == 1
    assert request.maximum_cost_usd == Decimal("0.0024")
    assert request.reserved_usage == BudgetUsage(3, 6000, 2100, Decimal("0.1024"))
    assert request.monthly_committed_usd == Decimal("1.0024")
    assert usage.attempts == 2 and "source-marker" not in repr(request)
    with pytest.raises(FrozenInstanceError):
        request.input_tokens = 0


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"tokens": 12001}, "request_input_tokens"),
        ({"output": 6001}, "request_output_tokens"),
        ({"usage": BudgetUsage(attempts=8)}, "run_attempts"),
        ({"usage": BudgetUsage(input_tokens=39500)}, "run_input_tokens"),
        ({"usage": BudgetUsage(output_tokens=11950)}, "run_output_tokens"),
        ({"pricing": replace(PRICING, context_tokens=1099)}, "model_context_tokens"),
        ({"usage": BudgetUsage(committed_usd=Decimal("0.749"))}, "run_cost"),
        ({"monthly": Decimal("11.999")}, "monthly_cost"),
    ],
)
def test_each_request_run_model_and_monthly_limit_blocks_admission(context, overrides, code):
    with pytest.raises(BudgetExceeded) as error:
        prepare(context, **overrides)
    assert error.value.code == code and "source-marker" not in str(error.value)


def test_exact_limits_are_inclusive(context):
    request = prepare(
        context,
        tokens=12000,
        output=6000,
        monthly=Decimal("11.25"),
        pricing=ModelPricing("fixture-model", 18000, Decimal("50"), Decimal("25")),
        usage=BudgetUsage(7, 28000, 6000),
    )
    assert request.reserved_usage == BudgetUsage(8, 40000, 12000, Decimal("0.75"))
    assert request.monthly_committed_usd == Decimal("12")


@pytest.mark.parametrize("tokens", [0, -1, True, 1.5, None])
def test_missing_or_invalid_token_counts_fail_closed(context, tokens):
    with pytest.raises(ValueError, match="counter"):
        prepare(context, tokens=tokens)


@pytest.mark.parametrize("value", [Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), 1.5])
def test_invalid_prices_usage_and_monthly_amounts_are_rejected(context, value):
    with pytest.raises(ValueError):
        replace(PRICING, input_usd_per_million=value)
    with pytest.raises(ValueError):
        BudgetUsage(committed_usd=value)
    with pytest.raises(ValueError):
        prepare(context, monthly=value)


def test_exhausted_attempts_do_not_invoke_the_token_counter(context):
    def unexpected(request):
        pytest.fail("No tokenization is needed when attempts are exhausted")

    with pytest.raises(BudgetExceeded, match="run_attempts"):
        prepare_analysis_request(
            context,
            PRICING,
            BudgetUsage(attempts=8),
            monthly_committed_usd=Decimal("0"),
            count_input_tokens=unexpected,
        )


@pytest.mark.parametrize("document", [b"private-marker", b"\xff", b"{}", b"[]"])
def test_bad_context_errors_do_not_echo_data(context, document):
    with pytest.raises(ValueError, match="context") as error:
        prepare(replace(context, document=document))
    assert "private-marker" not in str(error.value)


def test_context_identity_and_version_are_checked_before_tokenization(context):
    for key in ("run_id", "schema_version"):
        payload = json.loads(context.document)
        payload[key] = "mismatch"
        with pytest.raises(ValueError, match="context"):
            prepare(replace(context, document=json.dumps(payload).encode()))
