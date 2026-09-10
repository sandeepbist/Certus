from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


OPENAI_STANDARD_PRICING_PROFILE = "openai_standard_text:2026-08-30"
OPENAI_PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"
_MILLION = Decimal("1000000")


@dataclass(frozen=True)
class TokenRates:
    input_usd_per_million: Decimal
    cached_input_usd_per_million: Decimal
    output_usd_per_million: Decimal


@dataclass(frozen=True)
class ProviderCostEstimate:
    amount_usd: float
    pricing_profile: str
    priced_model: str
    input_usd_per_million: float
    cached_input_usd_per_million: float
    output_usd_per_million: float

    def as_profile(self) -> dict[str, object]:
        return {
            "amount_usd": self.amount_usd,
            "currency": "USD",
            "pricing_profile": self.pricing_profile,
            "pricing_source": OPENAI_PRICING_SOURCE,
            "priced_model": self.priced_model,
            "input_usd_per_million": self.input_usd_per_million,
            "cached_input_usd_per_million": self.cached_input_usd_per_million,
            "output_usd_per_million": self.output_usd_per_million,
        }


_STANDARD_TEXT_RATES = {
    "gpt-5.4-mini": TokenRates(Decimal("0.75"), Decimal("0.075"), Decimal("4.5")),
    "gpt-5.5": TokenRates(Decimal("5"), Decimal("0.5"), Decimal("30")),
}
_GPT_5_5_LONG_CONTEXT_RATES = TokenRates(
    Decimal("12.5"), Decimal("1.25"), Decimal("75")
)
_EMBEDDING_RATES = {
    "text-embedding-3-small": Decimal("0.02"),
    "text-embedding-3-large": Decimal("0.13"),
}


def _canonical_priced_model(model: str) -> Optional[str]:
    normalized = model.strip()
    for candidate in _STANDARD_TEXT_RATES:
        if normalized == candidate:
            return candidate
        if re.fullmatch(
            rf"{re.escape(candidate)}-20[0-9]{{2}}-[0-9]{{2}}-[0-9]{{2}}",
            normalized,
        ):
            return candidate
    return None


def estimate_openai_text_generation_cost(
    *,
    model: str,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    service_tier: str | None,
) -> ProviderCostEstimate | None:
    """Estimate direct OpenAI standard-tier text cost without guessing.

    The Responses API reports token usage but not billed currency. Only known
    model aliases/snapshots and the standard/default tier are priced here.
    """
    if min(input_tokens, cached_input_tokens, output_tokens) < 0:
        raise ValueError("Provider token usage cannot be negative")
    if cached_input_tokens > input_tokens:
        raise ValueError("Cached input tokens cannot exceed total input tokens")
    if service_tier not in {None, "default"}:
        return None

    priced_model = _canonical_priced_model(model)
    if priced_model is None:
        return None
    rates = _STANDARD_TEXT_RATES[priced_model]
    pricing_profile = OPENAI_STANDARD_PRICING_PROFILE
    if priced_model == "gpt-5.5" and input_tokens >= 272_000:
        rates = _GPT_5_5_LONG_CONTEXT_RATES
        pricing_profile = f"{pricing_profile}:gpt-5.5-272k-plus"

    uncached_input_tokens = input_tokens - cached_input_tokens
    amount = (
        Decimal(uncached_input_tokens) * rates.input_usd_per_million
        + Decimal(cached_input_tokens) * rates.cached_input_usd_per_million
        + Decimal(output_tokens) * rates.output_usd_per_million
    ) / _MILLION
    return ProviderCostEstimate(
        amount_usd=float(amount),
        pricing_profile=pricing_profile,
        priced_model=priced_model,
        input_usd_per_million=float(rates.input_usd_per_million),
        cached_input_usd_per_million=float(rates.cached_input_usd_per_million),
        output_usd_per_million=float(rates.output_usd_per_million),
    )


def estimate_openai_embedding_cost(
    *,
    model: str,
    input_tokens: int,
) -> ProviderCostEstimate | None:
    if input_tokens < 0:
        raise ValueError("Provider token usage cannot be negative")
    rate = _EMBEDDING_RATES.get(model.strip())
    if rate is None:
        return None
    amount = Decimal(input_tokens) * rate / _MILLION
    return ProviderCostEstimate(
        amount_usd=float(amount),
        pricing_profile=OPENAI_STANDARD_PRICING_PROFILE,
        priced_model=model.strip(),
        input_usd_per_million=float(rate),
        cached_input_usd_per_million=0.0,
        output_usd_per_million=0.0,
    )
