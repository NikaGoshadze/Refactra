import json
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

from refactra.analysis_request import AnalysisModelInput, BudgetExceeded
from refactra.models import ModelReservation
from refactra.openai_analysis import (
    PRICING,
    AnalysisProviderError,
    OpenAIAnalysisProvider,
    execute_read_only_analysis,
)

from .test_model_reservations import prepare


def test_expanded_output_is_reserved_and_sent(session_factory):
    run_id, attempt = prepare(session_factory).run_id, uuid4()

    def handle(request):
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 100}
            )
        with session_factory() as session:
            row = session.get(ModelReservation, attempt)
            assert row.output_tokens == json.loads(request.content)["max_output_tokens"] == 6000
            assert row.maximum_cost_usd == Decimal("0.027075")
        return httpx.Response(200, json=response())

    provider = OpenAIAnalysisProvider("fixture", httpx.MockTransport(handle))
    execute_read_only_analysis(session_factory, run_id, attempt, provider, max_output_tokens=6000)


def response():
    return {
        "object": "response",
        "id": "resp_fixture",
        "model": PRICING.model,
        "service_tier": "default",
        "status": "completed",
        "error": None,
        "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "src/cart.py:1 needs review"}],
            }
        ],
    }


@pytest.mark.parametrize("cached,expected", [(20, 20), (True, None), (101, None)])
def test_cached_usage_is_validated_provider_metadata(cached, expected):
    payload = response()
    payload["usage"]["input_tokens_details"] = {"cached_tokens": cached}
    provider = OpenAIAnalysisProvider(
        "private-key", httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    result = provider._generate(AnalysisModelInput(PRICING.model, "instructions", "{}", 3000))
    assert result.cached_input_tokens == expected


@pytest.mark.parametrize("response_id", [None, 123, "", " \n"])
def test_missing_or_blank_response_ids_are_rejected_with_confirmed_usage(response_id):
    payload = response()
    payload["id"] = response_id
    provider = OpenAIAnalysisProvider(
        "private-key", httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    with pytest.raises(AnalysisProviderError) as error:
        provider._generate(AnalysisModelInput(PRICING.model, "instructions", "{}", 3000))
    assert error.value.usage == (100, 20)


@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "incomplete",
        "refusal",
        "timeout",
        "unknown",
        "model",
        "tier",
        "bad_usage",
        "tool",
    ],
)
def test_committed_admission_settlement_and_no_replay(session_factory, outcome):
    run_id, attempt = prepare(session_factory).run_id, uuid4()
    calls, counted = [], {}

    def handle(request):
        body = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer private-key"
        assert request.url.host == "api.openai.com"
        if request.url.path.endswith("/input_tokens"):
            counted.update(body)
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 100}
            )
        calls.append(request)
        # Independent transaction sees the hold before any paid transport takes place.
        with session_factory() as session:
            row = session.get(ModelReservation, attempt)
            assert row is not None and row.settled_at is None
        assert all(body[key] == value for key, value in counted.items())
        assert body["reasoning"] == {"effort": "none"} and body["model"] == PRICING.model
        assert body["max_output_tokens"] == 3000 and body["store"] is False
        assert body["service_tier"] == "default" and body["truncation"] == "disabled"
        assert body["tools"] == []
        data = response()
        if outcome == "timeout":
            raise httpx.ReadTimeout("private source", request=request)
        if outcome == "incomplete":
            data["status"] = "incomplete"
        if outcome == "refusal":
            data["output"][0]["content"] = [{"type": "refusal", "refusal": "private source"}]
        if outcome == "unknown":
            data["usage"] = None
        if outcome == "model":
            data["model"] = "unexpected-model"
        if outcome == "tier":
            data["service_tier"] = "priority"
        if outcome == "bad_usage":
            data["usage"]["total_tokens"] = 1
        if outcome == "tool":
            data["output"] = [{"type": "function_call", "name": "run_command"}]
        return httpx.Response(200, json=data)

    provider = OpenAIAnalysisProvider("private-key", httpx.MockTransport(handle))
    if outcome == "success":
        result = execute_read_only_analysis(session_factory, run_id, attempt, provider)
        assert result.response_id == "resp_fixture" and result.usage == (100, 20)
        assert "src/cart.py:1" in result.text and "src/cart.py" not in repr(result)
    else:
        with pytest.raises(AnalysisProviderError) as error:
            execute_read_only_analysis(session_factory, run_id, attempt, provider)
        assert "private" not in str(error.value)
    assert "private-key" not in repr(provider)
    with session_factory() as session:
        row = session.get(ModelReservation, attempt)
        assert row.maximum_cost_usd == Decimal("0.013575")
        if outcome in {"success", "incomplete", "refusal", "tool"}:
            assert row.actual_cost_usd == Decimal("0.000165") and row.settled_at is not None
        else:
            assert row.actual_cost_usd is None and row.settled_at is None
    with pytest.raises(ValueError, match="resending is forbidden"):
        execute_read_only_analysis(session_factory, run_id, attempt, provider)
    assert len(calls) == 1


@pytest.mark.parametrize("tokens", [True, 0, "100", 12001])
def test_invalid_or_excessive_count_never_allocates_or_generates(session_factory, tokens):
    run_id, attempt = prepare(session_factory).run_id, uuid4()

    def handle(request):
        assert request.url.path.endswith("/input_tokens")
        return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": tokens})

    with pytest.raises((AnalysisProviderError, BudgetExceeded)):
        execute_read_only_analysis(
            session_factory,
            run_id,
            attempt,
            OpenAIAnalysisProvider("private-key", httpx.MockTransport(handle)),
        )
    with session_factory() as session:
        assert session.get(ModelReservation, attempt) is None


@pytest.mark.parametrize(
    "status,content",
    [
        (401, b"private-key"),
        (429, b"private source"),
        (302, b""),
        (200, b"invalid JSON"),
        (200, b"x" * (1024 * 1024 + 1)),
    ],
)
def test_transport_failures_are_sanitized_and_never_retried(status, content):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, content=content, headers={"location": "https://example.com"})

    provider = OpenAIAnalysisProvider("private-key", httpx.MockTransport(handle))
    with pytest.raises(AnalysisProviderError) as error:
        provider.count_input_tokens(AnalysisModelInput(PRICING.model, "private source", "{}", 3000))
    assert "private" not in str(error.value) and len(calls) == 1
