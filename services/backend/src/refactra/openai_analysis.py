"""Operator-invoked read-only analysis; no automatic retries or workflow scheduling."""

from dataclasses import dataclass, field
from decimal import Decimal
from time import monotonic
from typing import ClassVar
from uuid import UUID

import httpx

from refactra.analysis_context import (
    PreparedAnalysisContext,
    compact_read_only_context,
    load_analysis_context,
)
from refactra.analysis_request import (
    DEFAULT_REQUEST_OUTPUT,
    INSTRUCTIONS,
    MAX_REQUEST_OUTPUT,
    AnalysisModelInput,
    BudgetExceeded,
    BudgetUsage,
    ModelPricing,
    prepare_analysis_request,
)
from refactra.candidate_request import CANDIDATE_INSTRUCTIONS, candidate_text_config
from refactra.diagnostics import diagnostic_id, workflow_log
from refactra.model_reservations import reserve_analysis_request, settle_model_usage
from refactra.planning_request import PLAN_INSTRUCTIONS, PLAN_TEXT_CONFIG, load_planning_context

# Standard, uncached USD rates verified against OpenAI documentation on 2026-11-19.
PRICING = ModelPricing("gpt-5.4-mini-2026-03-17", 400_000, Decimal("0.75"), Decimal("4.50"))


class AnalysisProviderError(RuntimeError):
    def __init__(
        self,
        usage: tuple[int, int] | None = None,
        *,
        reason: str = "invalid_response",
        http_status: int | None = None,
    ):
        self.usage = usage
        self.reason = reason
        self.http_status = http_status
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
        body = {
            "model": request.model,
            "instructions": request.instructions,
            "input": request.context_json,
            "reasoning": {"effort": "none"},
        }
        if request.instructions == PLAN_INSTRUCTIONS:
            body["text"] = PLAN_TEXT_CONFIG
        elif request.instructions == CANDIDATE_INSTRUCTIONS:
            body["text"] = candidate_text_config(request.context_json)
        return body

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
        except httpx.HTTPStatusError as error:
            raise AnalysisProviderError(
                reason="http_error", http_status=error.response.status_code
            ) from None
        except httpx.TimeoutException:
            raise AnalysisProviderError(reason="timeout") from None
        except httpx.HTTPError:
            raise AnalysisProviderError(reason="transport_error") from None
        except (ValueError, RecursionError):
            raise AnalysisProviderError from None

    def count_input_tokens(self, request: AnalysisModelInput) -> int:
        payload = self._post("/input_tokens", self._input(request))
        tokens = payload.get("input_tokens")
        if (
            payload.get("object") != "response.input_tokens"
            or type(tokens) is not int
            or tokens <= 0
        ):
            raise AnalysisProviderError(reason="invalid_token_count")
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
            raise AnalysisProviderError(reason="unexpected_model_profile")
        usage = payload.get("usage")
        counts = None
        if isinstance(usage, dict):
            values = [usage.get(key) for key in ("input_tokens", "output_tokens", "total_tokens")]
            if all(type(value) is int and value >= 0 for value in values):
                if values[0] + values[1] == values[2]:
                    counts = (values[0], values[1])
        if payload.get("status") == "incomplete":
            detail = payload.get("incomplete_details")
            reason = detail.get("reason") if isinstance(detail, dict) else None
            raise AnalysisProviderError(
                counts,
                reason=reason
                if reason in ("max_output_tokens", "content_filter")
                else "incomplete_response",
            )
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
                    if content["type"] == "refusal":
                        raise AnalysisProviderError(counts, reason="model_refusal")
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
    check_admission=None,
    planning: bool = False,
    save_response=None,
):
    """Trusted caller must authorize repository access before invoking this helper.

    Reload saved input, count it, commit admission, then make one physical call outside
    database transactions. Unknown outcomes keep the hold; existing receipts never resend.
    The optional admission check runs in the reservation transaction. The caller
    may save the result in the settlement transaction with save_response.
    """
    with session_factory() as session:
        context = (
            load_planning_context(session, run_id, attempt_id)
            if planning
            else load_analysis_context(session, run_id)
        )
    if context is None:
        raise ValueError("A saved analysis context is required.")
    instructions = PLAN_INSTRUCTIONS if planning else INSTRUCTIONS

    def request(prompt):
        return execute_model_request(
            session_factory,
            attempt_id,
            provider,
            prompt,
            instructions=instructions,
            max_output_tokens=max_output_tokens,
            ledger_limit_usd=ledger_limit_usd,
            check_admission=check_admission,
            save_response=save_response,
        )

    try:
        return request(context)
    except BudgetExceeded as error:
        trace = {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "stage": "plan" if planning else "analysis",
        }
        if ledger_limit_usd is None and error.code in {
            "request_input_tokens",
            "model_context_tokens",
            "run_input_tokens",
        }:
            compact = compact_read_only_context(context)
            if compact.document != context.document:
                workflow_log("read_only_context_compacted", **trace, reason=error.code)
                try:
                    return request(compact)
                except BudgetExceeded as compact_error:
                    workflow_log("read_only_budget_rejected", **trace, reason=compact_error.code)
                    raise
        workflow_log("read_only_budget_rejected", **trace, reason=error.code)
        raise


def execute_model_request(
    session_factory,
    attempt_id: UUID,
    provider,
    context: PreparedAnalysisContext,
    *,
    instructions: str,
    max_output_tokens: int,
    ledger_limit_usd=None,
    check_admission=None,
    save_response=None,
):
    """Count and reserve one model call using a trusted context and instruction set."""
    stage = (
        "candidate"
        if instructions == CANDIDATE_INSTRUCTIONS
        else "plan"
        if instructions == PLAN_INSTRUCTIONS
        else "analysis"
    )
    trace = {"run_id": context.run_id, "attempt_id": attempt_id, "stage": stage}
    start = monotonic()
    try:
        request = prepare_analysis_request(
            context,
            provider.pricing,
            BudgetUsage(),
            monthly_committed_usd=Decimal("0"),
            count_input_tokens=provider.count_input_tokens,
            max_output_tokens=max_output_tokens,
            instructions=instructions,
        )
    except AnalysisProviderError as error:
        workflow_log(
            "model_token_count_failed",
            warning=True,
            **trace,
            reason=error.reason,
            http_status=error.http_status,
            duration_ms=round((monotonic() - start) * 1000),
        )
        raise
    # Durable admission recalculates real balances; preflight snapshots are not authority.
    with session_factory.begin() as session:
        if check_admission is not None:
            check_admission(session)
        receipt = reserve_analysis_request(
            session, attempt_id, request, ledger_limit_usd=ledger_limit_usd
        )
    if not receipt.created:
        raise ValueError("This attempt already has a receipt; resending is forbidden.")
    try:
        start = monotonic()
        result = provider._generate(request.model_input)
    except AnalysisProviderError as error:
        workflow_log(
            "model_request_failed",
            warning=True,
            **trace,
            reason=error.reason,
            http_status=error.http_status,
            usage=error.usage,
            duration_ms=round((monotonic() - start) * 1000),
        )
        if error.usage is not None:
            with session_factory.begin() as session:
                settle_model_usage(
                    session, attempt_id, input_tokens=error.usage[0], output_tokens=error.usage[1]
                )
        raise
    workflow_log(
        "model_response_received",
        **trace,
        response_id=diagnostic_id(result.response_id),
        input_tokens=result.usage[0],
        output_tokens=result.usage[1],
        output_limit=max_output_tokens,
        duration_ms=round((monotonic() - start) * 1000),
    )
    with session_factory.begin() as session:
        settle_model_usage(
            session, attempt_id, input_tokens=result.usage[0], output_tokens=result.usage[1]
        )
        if save_response is not None:
            save_response(session, result)
    return result
