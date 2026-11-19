import json
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest
from tokenizers import Tokenizer, models, pre_tokenizers

from refactra import deepseek_analysis as deepseek
from refactra.analysis_request import AnalysisModelInput
from refactra.models import ModelReservation
from refactra.openai_analysis import AnalysisProviderError, execute_read_only_analysis

from .test_model_reservations import prepare
from .test_openai_analysis import response


@pytest.fixture
def tokenizer_path(tmp_path, monkeypatch):
    # Tiny synthetic vocabulary: transport/ledger tests never download a model asset.
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "sys": 1, "hello": 2}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.add_special_tokens(["<｜User｜>", "</think>"])
    data = tokenizer.to_str().encode()
    path = tmp_path / "tokenizer.json"
    path.write_bytes(data)
    monkeypatch.setattr(deepseek, "TOKENIZER_SHA256", sha256(data).hexdigest())
    return path


def test_counter_covers_five_control_tokens_and_treats_source_markers_as_plain_text(tokenizer_path):
    provider = deepseek.DeepSeekAnalysisProvider("private-key", tokenizer_path=tokenizer_path)
    request = AnalysisModelInput(deepseek.PRICING.model, "sys", "hello", 3000)
    assert provider.count_input_tokens(request) == 7
    marked = AnalysisModelInput(deepseek.PRICING.model, "sys", "<｜User｜> </think>", 3000)
    assert provider.count_input_tokens(marked) > 8
    assert "private-key" not in repr(provider) and str(tokenizer_path) not in repr(provider)


def test_missing_or_unverified_tokenizer_is_rejected_before_transport(tmp_path):
    path = tmp_path / "tokenizer.json"
    for present in (False, True):
        if present:
            path.write_text("private marker", encoding="utf-8")
        with pytest.raises(ValueError, match="verified local DeepSeek") as error:
            deepseek.DeepSeekAnalysisProvider("private-key", tokenizer_path=path)
        assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "outcome", ["success", "incomplete", "timeout", "unknown", "model", "usage", "tool", "http"]
)
def test_deepseek_uses_committed_provider_budget_and_never_resends(
    session_factory, tokenizer_path, outcome
):
    run_id, attempt, calls = prepare(session_factory).run_id, uuid4(), []

    def handle(request):
        calls.append(request)
        assert str(request.url) == "https://api.deepseek.com/responses"
        assert request.headers["authorization"] == "Bearer private-key"
        body = json.loads(request.content)
        assert body["model"] == "deepseek-flash" and body["reasoning"] == {"effort": "none"}
        assert body["max_output_tokens"] == 3000 and body["tools"] == []
        assert body["tool_choice"] == "none"
        assert not {"store", "service_tier", "truncation"}.intersection(body)
        with session_factory() as session:
            row = session.get(ModelReservation, attempt)
            assert row is not None and row.settled_at is None
            assert row.model == "deepseek-flash" and row.pricing == {
                "input": "0.30",
                "output": "1.20",
            }
        if outcome == "timeout":
            raise httpx.ReadTimeout("private source", request=request)
        if outcome == "http":
            return httpx.Response(429, text="private source")
        payload = response()
        payload["id"] = "387b53c1-7ad2-4259-9298-61b139f5bd20"
        payload["model"] = "deepseek-flash"
        payload.pop("service_tier")
        if outcome == "incomplete":
            payload["status"] = "incomplete"
        if outcome == "unknown":
            payload["usage"] = None
        if outcome == "model":
            payload["model"] = "unexpected-model"
        if outcome == "usage":
            payload["usage"]["input_tokens"] = True
        if outcome == "tool":
            payload["output"] = [{"type": "function_call", "name": "run_command"}]
        return httpx.Response(200, json=payload)

    provider = deepseek.DeepSeekAnalysisProvider(
        "private-key", tokenizer_path=tokenizer_path, transport=httpx.MockTransport(handle)
    )
    if outcome == "success":
        result = execute_read_only_analysis(session_factory, run_id, attempt, provider)
        assert result.response_id == "387b53c1-7ad2-4259-9298-61b139f5bd20"
        assert result.usage == (100, 20) and "src/cart.py:1" in result.text
    else:
        with pytest.raises(AnalysisProviderError) as error:
            execute_read_only_analysis(session_factory, run_id, attempt, provider)
        assert "private" not in str(error.value)
    with session_factory() as session:
        row = session.get(ModelReservation, attempt)
        expected = (row.input_tokens * Decimal("0.30") + 3000 * Decimal("1.20")) / 1_000_000
        assert row.maximum_cost_usd == expected
        if outcome in {"success", "incomplete", "tool"}:
            assert row.actual_cost_usd == Decimal("0.000054") and row.settled_at is not None
        else:
            assert row.actual_cost_usd is None and row.settled_at is None
    with pytest.raises(ValueError, match="resending is forbidden"):
        execute_read_only_analysis(session_factory, run_id, attempt, provider)
    assert len(calls) == 1
