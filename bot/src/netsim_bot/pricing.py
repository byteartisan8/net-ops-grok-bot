"""What a Grok call costs (USD per 1M tokens), from docs.x.ai/developers/models on 2026-10-20.

A request whose prompt reaches 200k tokens is billed at the higher rate for all of its tokens. Reasoning tokens are
counted as output tokens. Unknown models are priced like ``grok-4.7`` (the most expensive listed text model), so the
budget errs on the safe side; the NOC page marks such costs as estimates.
"""

from __future__ import annotations

from dataclasses import dataclass

from netsim_bot.grok import Usage

LONG_CONTEXT = 200_000


@dataclass(frozen=True)
class Price:
    input: float
    cached: float
    output: float
    input_long: float
    cached_long: float
    output_long: float


PRICES: dict[str, Price] = {
    "grok-4.7": Price(2.00, 0.50, 6.00, 4.00, 1.00, 12.00),
    "grok-4.6": Price(2.00, 0.50, 6.00, 4.00, 1.00, 12.00),
    "grok-4.5": Price(2.00, 0.30, 6.00, 4.00, 0.60, 12.00),
    "grok-4.3": Price(1.25, 0.20, 2.50, 2.50, 0.40, 5.00),
    "grok-4.20-0309-reasoning": Price(1.25, 0.20, 2.50, 2.50, 0.40, 5.00),
    "grok-4.20-0309-non-reasoning": Price(1.25, 0.20, 2.50, 2.50, 0.40, 5.00),
    "grok-4.20-multi-agent-0309": Price(1.25, 0.20, 2.50, 2.50, 0.40, 5.00),
    "grok-build-0.1": Price(1.00, 0.20, 2.00, 2.00, 0.40, 4.00),
}


def price_for(model: str) -> tuple[Price, bool]:
    """The price of ``model`` (aliases like ``grok-4.7-latest`` map to their base) and whether it is listed."""
    if model in PRICES:
        return PRICES[model], True
    for name in sorted(PRICES, key=len, reverse=True):
        if model.startswith(name):
            return PRICES[name], True
    return PRICES["grok-4.7"], False


def cost_usd(model: str, usage: Usage, prompt_tokens: int | None = None) -> float:
    """Cost of one call (or of summed calls, if each stayed below the long-context threshold)."""
    price, _ = price_for(model)
    long = (prompt_tokens if prompt_tokens is not None else usage.input_tokens) >= LONG_CONTEXT
    rate_in = price.input_long if long else price.input
    rate_cached = price.cached_long if long else price.cached
    rate_out = price.output_long if long else price.output
    cached = min(usage.cached_tokens, usage.input_tokens)
    fresh = usage.input_tokens - cached
    return (fresh * rate_in + cached * rate_cached + usage.output_tokens * rate_out) / 1_000_000
