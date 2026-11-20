"""Operator-invoked read-only analysis; no automatic retries or workflow scheduling."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import ClassVar
from uuid import UUID

import httpx

from refactra.analysis_context import load_analysis_context
from refactra.analysis_request import (
    DEFAULT_REQUEST_OUTPUT,
    MAX_REQUEST_OUTPUT,
    AnalysisModelInput,
    BudgetUsage,
    ModelPricing,
    prepare_analysis_request,
)
from refactra.model_reservations import reserve_analysis_request, settle_model_usage

# Standard, uncached USD rates verified against OpenAI documentation on 2026-11-19.
PRICING = ModelPricing("gpt-5.4-mini-2026-03-17", 400_000, Decimal("0.75"), Decimal("4.50"))


class AnalysisProviderError(RuntimeError):
    def __init__(self, usage: tuple[int, int] | None = None):
        self.usage = usage
        super().__init__("Read-only model analysis failed; no automatic retry was attempted.")


@dataclass(frozen=True)
class AnalysisResult:
    response_id: str
    text: str = field(repr=False)
    usage: tuple[int, int]
    cached_input_tokens: int | None = None


@dataclass(frozen=True)
class OpenAIAnalysisProvider:
    pricing: ClassVar[ModelPricing] = PRICING
    responses_url: ClassVar[str] = "https://api.openai.com/v1/responses"
    api_key: str = field(repr=False)
    transport: httpx.BaseTransport | None = field(default=None, repr=False)

    def __post_init__(self):
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ValueError("A server-side provider API key is required.")

    def _input(self, request: AnalysisModelInput) -> dict:
        if (
            request.model != self.pricing.model
            or not 1 <= request.max_output_tokens <= MAX_REQUEST_OUTPUT
        ):
            raise ValueError("Unsupported read-only model configuration.")
        return {
            "model": request.model,
            "instructions": request.instructions,
            "input": request.context_json,
            "reasoning": {"effort": "none"},
        }

    def _post(self, path: str, body: dict) -> dict:
        try:
            with httpx.Client(
                transport=self.transport, timeout=90, trust_env=False, follow_redirects=False
            ) as client:
                with client.stream(
                    "POST",
                    f"{self.responses_url}{path}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=body,
                ) as response:
                    response.raise_for_status()
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > 1024 * 1024:
                            raise ValueError
                    payload = httpx.Response(200, content=bytes(data)).json()
                    if not isinstance(payload, dict):
                        raise ValueError
                    return payload
        except (httpx.HTTPError, ValueError, RecursionError):
            raise AnalysisProviderError from None

    def count_input_tokens(self, request: AnalysisModelInput) -> int:
        payload = self._post("/input_tokens", self._input(request))
        tokens = payload.get("input_tokens")
        if (
            payload.get("object") != "response.input_tokens"
            or type(tokens) is not int
            or tokens <= 0
        ):
            raise AnalysisProviderError
        return tokens

    def _generation_options(self, request: AnalysisModelInput) -> dict:
        return {
            "max_output_tokens": request.max_output_tokens,
            "store": False,
            "service_tier": "default",
            "truncation": "disabled",
            "tools": [],
        }

    def _response_matches_profile(self, payload: dict) -> bool:
        return (
            payload.get("object") == "response"
            and payload.get("model") == self.pricing.model
            and payload.get("service_tier") == "default"
        )

    def _generate(self, request: AnalysisModelInput) -> AnalysisResult:
        payload = self._post("", self._input(request) | self._generation_options(request))
        # Only metadata for this provider's model/profile can release the saved hold.
        if not self._response_matches_profile(payload):
            raise AnalysisProviderError
        usage = payload.get("usage")
        counts = None
        if isinstance(usage, dict):
            values = [usage.get(key) for key in ("input_tokens", "output_tokens", "total_tokens")]
            if all(type(value) is int and value >= 0 for value in values):
                if values[0] + values[1] == values[2]:
                    counts = (values[0], values[1])
        try:
            if counts is None or payload["status"] != "completed" or payload.get("error"):
                raise ValueError
            response_id = payload["id"]
            # Provider IDs are opaque strings; DeepSeek uses UUIDs rather than resp_ IDs.
            if not isinstance(response_id, str) or not response_id.strip():
                raise ValueError
            texts = []
            for item in payload["output"]:
                if item["type"] == "reasoning":
                    continue
                if (item["type"], item["role"], item["status"]) != (
                    "message",
                    "assistant",
                    "completed",
                ):
                    raise ValueError
                for content in item["content"]:
                    if content["type"] != "output_text" or not isinstance(content["text"], str):
                        raise ValueError
                    texts.append(content["text"])
            text = "\n".join(texts)
            if not text.strip():
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise AnalysisProviderError(counts) from None
        details = usage.get("input_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        if type(cached) is not int or not 0 <= cached <= counts[0]:
            cached = None
        return AnalysisResult(response_id, text, counts, cached)


def execute_read_only_analysis(
    session_factory,
    run_id: UUID,
    attempt_id: UUID,
    provider,
    *,
    ledger_limit_usd=None,
    max_output_tokens: int = DEFAULT_REQUEST_OUTPUT,
):
    """Trusted caller must authorize repository access before invoking this helper.

    Reload saved input, count it, commit admission, then make one physical call outside
    database transactions. Unknown outcomes keep the hold; existing receipts never resend.
    Results are returned to the caller, not yet persisted as workflow checkpoints.
    """
    with session_factory() as session:
        context = load_analysis_context(session, run_id)
    if context is None:
        raise ValueError("A saved analysis context is required.")
    request = prepare_analysis_request(
        context,
        provider.pricing,
        BudgetUsage(),
        monthly_committed_usd=Decimal("0"),
        count_input_tokens=provider.count_input_tokens,
        max_output_tokens=max_output_tokens,
    )
    # Durable admission recalculates real balances; preflight snapshots are not authority.
    with session_factory.begin() as session:
        receipt = reserve_analysis_request(
            session, attempt_id, request, ledger_limit_usd=ledger_limit_usd
        )
    if not receipt.created:
        raise ValueError("This attempt already has a receipt; resending is forbidden.")
    try:
        result = provider._generate(request.model_input)
    except AnalysisProviderError as error:
        if error.usage is not None:
            with session_factory.begin() as session:
                settle_model_usage(
                    session, attempt_id, input_tokens=error.usage[0], output_tokens=error.usage[1]
                )
        raise
    with session_factory.begin() as session:
        settle_model_usage(
            session, attempt_id, input_tokens=result.usage[0], output_tokens=result.usage[1]
        )
    return result
