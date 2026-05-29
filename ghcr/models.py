"""Shared immutable data types.

Kept dependency-free so every other module can import these without creating
import cycles.
"""

from __future__ import annotations

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
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class ReviewResult:
    content: str
    usage: Usage
    model: str


# Terminal action constants — recorded once per head SHA, block future re-review.
ACTION_REVIEW = "review"
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
