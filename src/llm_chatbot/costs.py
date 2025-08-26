"""Token pricing, cost estimation, and simple billing state."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, Optional

# USD per 1M tokens
PRICING = {
    "gpt-5": {"input": 1.250, "cached_input": 0.125, "output": 10.000},
    "gpt-5-mini": {"input": 0.250, "cached_input": 0.025, "output": 2.000},
    "gpt-5-nano": {"input": 0.050, "cached_input": 0.005, "output": 0.400},
}


def model_tier(model: str) -> str:
    """Map a model name to one of the pricing tiers."""
    m = model.lower()
    if "nano" in m:
        return "gpt-5-nano"
    if "mini" in m:
        return "gpt-5-mini"
    return "gpt-5"


def usd_cost(model: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float:
    """Estimate USD cost from token counts using the tiered pricing table."""
    tier = model_tier(model)
    price = PRICING.get(tier, PRICING["gpt-5"])  # default to gpt-5

    # Convert tokens to million tokens
    def mtok(x: int) -> float:
        return max(0, x) / 1_000_000.0

    return (
        mtok(input_tokens - cached_input_tokens) * price["input"]
        + mtok(cached_input_tokens) * price["cached_input"]
        + mtok(output_tokens) * price["output"]
    )


@dataclass
class Billing:
    """Aggregate cost tracking with budgets and alert thresholds."""

    daily_usd: float = 0.0
    daily_key: str = ""
    monthly_usd: float = 0.0
    monthly_key: str = ""
    by_model: Dict[str, float] = field(default_factory=dict)
    by_feature: Dict[str, float] = field(default_factory=dict)  # e.g., "listen", "mention"
    budget_daily_usd: Optional[float] = None
    budget_monthly_usd: Optional[float] = None
    thresholds: tuple = (0.5, 0.8, 1.0)
    hard_stop: bool = True
    last_daily_alert: float = 0.0
    last_monthly_alert: float = 0.0


def rollover_if_needed(b: Billing) -> None:
    """Reset daily/monthly totals when the day/month changes."""
    today = dt.date.today().isoformat()
    ym = dt.date.today().strftime("%Y-%m")
    if b.daily_key != today:
        b.daily_key = today
        b.daily_usd = 0.0
    if b.monthly_key != ym:
        b.monthly_key = ym
        b.monthly_usd = 0.0
