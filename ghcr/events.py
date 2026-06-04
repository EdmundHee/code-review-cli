"""In-process event bus + typed events for the live TUI.

Dependency-free (like ``models.py``) so any module can publish without import
cycles or pulling in Rich. The poll loop and orchestrator publish; the TUI
subscribes. Headless ``run`` constructs no bus, so every emit site guards with
``if self.bus:`` and stays a no-op.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class CycleStarted:
    repo_count: int
    interval_s: int


@dataclass(frozen=True)
class RepoListed:
    repo: str
    open_prs: int


@dataclass(frozen=True)
class RepoDone:
    """A repo finished its poll this cycle (PRs reviewed/skipped, or list failed).

    Marks the end boundary of working a repo so the TUI can clear the active
    highlight and stamp "last polled" freshness. Carries no duration — the TUI
    computes time-since-poll from when this arrives.
    """

    repo: str


@dataclass(frozen=True)
class DeepSeekDone:
    repo: str
    pr_number: int
    prompt_tokens: int
    completion_tokens: int
    latency_s: float
    snippet: str
    title: str = ""


@dataclass(frozen=True)
class AgentEvent:
    """One multi-pass sub-agent (a lens or a per-finding scorer) changing state.

    Published from the review thread pool, so it must stay pure data — the bus is
    thread-safe and the TUI only mutates in-memory state on receipt.
    """

    repo: str
    pr_number: int
    agent: str  # "lens:security" | "score:#3"
    status: str  # "running" | "done" | "failed"
    detail: str = ""
    title: str = ""


@dataclass(frozen=True)
class PrOutcome:
    repo: str
    pr_number: int
    action: str
    cost_usd: float = 0.0
    comment_url: str | None = None
    title: str = ""
    head_sha: str = ""


@dataclass(frozen=True)
class ConfigReloaded:
    repos: tuple[str, ...]
    interval_s: int
    budget: float


@dataclass(frozen=True)
class LogLine:
    ts: str
    level: str
    name: str
    message: str


# Any of the above. Kept as a union comment rather than a type alias so the
# module imports clean on 3.11 without typing gymnastics.
Event = object


class EventBus:
    """Thread-safe synchronous pub/sub.

    ``publish`` invokes every subscriber under a lock, so subscribers must be
    cheap and non-blocking (the TUI's only does in-memory state mutation).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[object], None]] = []

    def subscribe(self, cb: Callable[[object], None]) -> None:
        with self._lock:
            self._subscribers.append(cb)

    def publish(self, event: object) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for cb in subs:
            cb(event)
