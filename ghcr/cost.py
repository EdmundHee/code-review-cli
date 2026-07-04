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


def per_chunk_diff_budget(
    max_diff_bytes: int,
    token_cap: int,
    n_lenses: int,
    overhead_tokens: int,
    safety: float = 0.9,
) -> int:
    """Largest per-chunk diff byte size satisfying BOTH size gates at once.

    The multi pipeline re-sends the diff to every lens, so a chunk must fit the
    byte cap AND keep the summed lens input under ``token_cap``:
    ``overhead_tokens + n_lenses * diff_tokens <= token_cap`` with diff_tokens ≈
    bytes/4 (see ``estimate_input_tokens``; bytes >= chars, so the byte
    denomination is conservative). ``overhead_tokens`` is everything except the
    diff (lens system prompts + diff-less user prompt + referenced-context
    reserve), already summed across lenses. May return <= 0 on pathological
    config (token cap smaller than the prompts themselves) — callers must treat
    that as unreviewable, pre-spend.
    """
    token_room = token_cap - overhead_tokens
    return min(max_diff_bytes, int(safety * 4 * token_room / max(1, n_lenses)))
