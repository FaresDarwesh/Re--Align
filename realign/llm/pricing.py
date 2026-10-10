"""Model price table (USD per 1,000,000 tokens) + cost math.

Prices are configurable via env-less defaults and can be overridden at runtime
by editing this table — it is deliberately a plain dict, not a remote call.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# (input $/1M, output $/1M)
PRICES: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "o3-mini": (1.10, 4.40),
    # Anthropic
    "claude-opus-4-1": (15.00, 75.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-3-5-haiku-20241022": (0.80, 4.00),
    # Google
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
    # Open-source / self-hosted -> treat infra cost as ~0 but not literally 0
    "mock": (0.0, 0.0),
}
DEFAULT_PRICE = (1.00, 4.00)
LOCAL_PRICE = (0.05, 0.15)  # self-hosted: electricity+amortisation allowance


def unit_price(model: str, provider: str = "") -> tuple[float, float]:
    key = (model or "").strip()
    if key in PRICES:
        return PRICES[key]
    # fuzzy: prefix match on family
    for known, price in PRICES.items():
        if key.startswith(known.rsplit("-", 1)[0]) and len(known.rsplit("-", 1)[0]) >= 6:
            return price
    if provider in {"ollama", "local", "vllm", "lmstudio"}:
        return LOCAL_PRICE
    return DEFAULT_PRICE


def compute_cost_micros(model: str, tokens_in: int, tokens_out: int, provider: str = "") -> int:
    in_price, out_price = unit_price(model, provider)
    cost = (Decimal(tokens_in) / Decimal(1_000_000)) * Decimal(str(in_price)) + (
        Decimal(tokens_out) / Decimal(1_000_000)
    ) * Decimal(str(out_price))
    return int((cost * Decimal(1_000_000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
