"""Shared immutable data types.

Kept dependency-free so every other module can import these without creating
import cycles.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PullRequest:
    repo: str  # "owner/repo"
    number: int
    head_sha: str
    title: str = ""
    author: str = ""
    is_draft: bool = False
    url: str = ""
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    labels: tuple[str, ...] = ()
    base_ref: str = ""
    head_ref: str = ""


@dataclass(frozen=True)
class PriorComment:
    """One existing comment on the PR, fed back to the reviewer as context.

    ``kind`` is "issue" (PR conversation timeline — where the bot's own past
    reviews land) or "review" (inline, anchored to a diff line). ``path``/``line``
    are set only for the inline kind."""

    author: str
    body: str
    created_at: str = ""
    kind: str = "issue"  # "issue" | "review"
    path: str = ""
    line: int | None = None
    comment_id: int = 0  # GitHub comment id; the @mention re-review watermark keys on it


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class ProviderTokens:
    """Summed token counts split by provider (worker = DeepSeek, advisor = Claude),
    e.g. the 24h totals read back from the store for the TUI."""

    worker_prompt: int = 0
    worker_completion: int = 0
    advisor_prompt: int = 0
    advisor_completion: int = 0


def merge_usages(usages: Iterable[Usage]) -> Usage:
    """Sum a sequence of per-call Usage objects into one aggregate.

    Pure — the multi-pass pipeline calls the model many times (lenses + per-finding
    scoring) and totals their token counts here before a single cost calculation.
    """
    p = c = t = 0
    for u in usages:
        p += u.prompt_tokens
        c += u.completion_tokens
        t += u.total_tokens
    return Usage(prompt_tokens=p, completion_tokens=c, total_tokens=t)


@dataclass(frozen=True)
class ReviewResult:
    content: str
    usage: Usage
    model: str


# -- referenced-context fetch (multi-pass) ----------------------------------
@dataclass(frozen=True)
class ContextRequest:
    """One symbol the planner pass asks to see defined before reviewing. ``module_hint``
    is whatever the diff revealed about where it lives (an import or dotted path); it may
    be empty, in which case resolution falls back to code search."""

    symbol: str
    module_hint: str = ""
    reason: str = ""


@dataclass(frozen=True)
class ReferencedSnippet:
    """One resolved snippet fetched from the repo for a symbol the diff references
    but does not show. ``kind`` is "definition" (a recognized def/class/binding —
    authoritative ground truth) or "usage" (only a mention window was found — must
    NOT be presented as a verified definition)."""

    symbol: str
    path: str
    text: str
    kind: str = "definition"  # "definition" | "usage"


# -- multi-pass review pipeline types --------------------------------------
# Severities a finding may carry, ordered most→least severe (drives grouping).
SEVERITY_ORDER = ("BLOCKER", "WARNING", "MINOR")


@dataclass(frozen=True)
class Finding:
    """One issue raised by a review lens. ``confidence``/``reason`` are filled by
    the scoring pass; ``id`` is assigned after merge/dedup to correlate scores."""

    severity: str  # BLOCKER | WARNING | MINOR
    file: str
    area: str = ""
    issue: str = ""
    fix: str = ""
    lens: str = ""
    confidence: int | None = None
    reason: str = ""
    id: int = 0


@dataclass(frozen=True)
class CoverageVerdict:
    """The test_coverage lens's structured read on whether the PR ships tests for
    the behavior it changes. ``has_tests`` True also covers 'no tests needed'.

    Named without a ``Test`` prefix so pytest does not try to collect it."""

    has_tests: bool
    detail: str = ""


@dataclass(frozen=True)
class LensResult:
    """One lens's outcome. ``ok`` False means the call or its JSON parse failed —
    findings are then empty but ``usage`` may still be non-zero (we paid for it)."""

    lens: str
    findings: tuple[Finding, ...]
    usage: Usage
    ok: bool
    raw: str = ""
    coverage: TestCoverageVerdict | None = None


# Terminal action constants — recorded once per head SHA, block future re-review.
ACTION_REVIEW = "review"
ACTION_REREVIEWED = "rereviewed"  # @mention-triggered re-review; NOT in the seen-set
ACTION_SKIP_SEEN = "skip_seen"
ACTION_SKIP_DRAFT = "skip_draft"
ACTION_SKIP_AUTHOR = "skip_author"
ACTION_SKIP_EMPTY = "skip_empty"
ACTION_SKIP_OVERSIZED = "skip_oversized"
ACTION_SKIP_BUDGET = "skip_budget"
ACTION_ERROR = "error"


@dataclass(frozen=True)
class ReviewDecision:
    action: str
    reason: str = ""
