"""DeepSeek V4.1 Flash, restricted to text-only, non-thinking analysis requests."""

import json
from dataclasses import dataclass, field
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import ClassVar

from tokenizers import Tokenizer

from refactra.analysis_request import AnalysisModelInput, ModelPricing
from refactra.openai_analysis import AnalysisProviderError, OpenAIAnalysisProvider

# Peak, uncached upper estimates; official prices checked 2026-11-19.
PRICING = ModelPricing("deepseek-flash", 1_000_000, Decimal("0.30"), Decimal("1.20"))
TOKENIZER_SHA256 = "81f64d1248a68ce3663e07ab3ee48b851e5df0e32d27cb98e4c9a268151e8d99"
RECIPE_COMMIT = "935817c66f5d6e64838f419692545f06787e485f"
TOKENIZER_URL = (
    f"https://raw.githubusercontent.com/deepseek-ai/deepseek-recipe/{RECIPE_COMMIT}/"
    "static/tokenizers/v41/tokenizer.json"
)


@dataclass(frozen=True)
class DeepSeekAnalysisProvider(OpenAIAnalysisProvider):
    pricing: ClassVar[ModelPricing] = PRICING
    responses_url: ClassVar[str] = "https://api.deepseek.com/responses"
    tokenizer_path: Path = field(kw_only=True, repr=False)
    _tokenizer: Tokenizer = field(init=False, repr=False)

    def __post_init__(self):
        super().__post_init__()
        try:
            with self.tokenizer_path.open("rb") as stream:
                data = stream.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024 or sha256(data).hexdigest() != TOKENIZER_SHA256:
                raise ValueError
            config = json.loads(data)
            # User-supplied special-token spellings are ordinary text, not control tokens.
            config["added_tokens"] = []
            object.__setattr__(self, "_tokenizer", Tokenizer.from_str(json.dumps(config)))
        except Exception:
            raise ValueError("The verified local DeepSeek V4.1 tokenizer is required.") from None

    def count_input_tokens(self, request: AnalysisModelInput) -> int:
        body = self._input(request)
        try:
            encoded = self._tokenizer.encode_batch(
                [body["instructions"], body["input"]], add_special_tokens=False
            )
            # Official V4.1 envelope for exactly system + user, no tools/thinking:
            # BOS, SYSTEM, instructions, USER, input, ASSISTANT, THINKING_END.
            # See deepseek-recipe at RECIPE_COMMIT, encoding/src/v4/dsv41.rs.
            return 5 + sum(len(part.ids) for part in encoded)
        except Exception:
            raise AnalysisProviderError from None

    def _generation_options(self, request: AnalysisModelInput) -> dict:
        # store/service_tier/truncation are unsupported by DeepSeek; omit them.
        return {"max_output_tokens": request.max_output_tokens, "tools": [], "tool_choice": "none"}

    def _response_matches_profile(self, payload: dict) -> bool:
        return payload.get("object") == "response" and payload.get("model") == self.pricing.model
