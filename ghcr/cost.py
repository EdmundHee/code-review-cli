"""Pure token -> USD math and a cheap pre-call token estimate."""

from __future__ import annotations

from dataclasses import dataclass

from .models import Usage


@dataclass(frozen=True)
class Prices:
    input_per_1m: float
    output_per_1m: float


def estimate_cost_usd(usage: Usage, prices: Prices) -> float:
    """Authoritative cost from a completed call's usage object."""
    return (
        usage.prompt_tokens / 1_000_000 * prices.input_per_1m
        + usage.completion_tokens / 1_000_000 * prices.output_per_1m
    )


def estimate_input_tokens(text: str) -> int:
    """Cheap pre-call gate heuristic (~4 chars/token). Intentionally rough."""
    return (len(text) + 3) // 4
