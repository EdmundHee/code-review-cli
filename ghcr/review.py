"""Orchestrate the review of one PR end-to-end.

The only module that composes I/O (gh + DeepSeek + state). Decision order is
cheap-first so we never pay for a review we can rule out, and the comment is
posted BEFORE the DB row is written (a failed post must not be recorded as done).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import comment as comment_mod
from .config import Config
from .cost import estimate_cost_usd, estimate_input_tokens
from .deepseek import DeepSeekError
from .events import DeepSeekDone
from .diff_filter import filter_diff
from .github import GhError
from .models import (
    ACTION_ERROR,
    ACTION_REVIEW,
    ACTION_SKIP_AUTHOR,
    ACTION_SKIP_BUDGET,
    ACTION_SKIP_DRAFT,
    ACTION_SKIP_EMPTY,
    ACTION_SKIP_OVERSIZED,
    ACTION_SKIP_SEEN,
    PullRequest,
    ReviewDecision,
)
from .prompts import SYSTEM_PROMPT, build_user_prompt

log = logging.getLogger("ghcr.review")


@dataclass
class ReviewOutcome:
    action: str
    cost_usd: float = 0.0
    comment_url: str | None = None


class ReviewOrchestrator:
    def __init__(self, gh, deepseek, store, config: Config, now=None, bus=None):
        self.gh = gh
        self.deepseek = deepseek
        self.store = store
        self.cfg = config
        self.bus = bus
        self._now = now or (lambda: datetime.now(timezone.utc))

    # -- decision (no spend, no DB write) --------------------------------
    def decide(self, pr: PullRequest) -> ReviewDecision:
        rp = self.cfg.review_policy
        ignore = {a.lower() for a in rp.ignore_authors} | {self.cfg.github.bot_login.lower()}
        if (pr.author or "").lower() in ignore:
            return ReviewDecision(ACTION_SKIP_AUTHOR, f"author {pr.author} ignored")
        if pr.is_draft and rp.skip_drafts:
            return ReviewDecision(ACTION_SKIP_DRAFT, "draft PR")
        if self.store.already_reviewed(pr.repo, pr.number, pr.head_sha):
            return ReviewDecision(ACTION_SKIP_SEEN, "head SHA already handled")
        return ReviewDecision(ACTION_REVIEW, "")

    # -- full pipeline ---------------------------------------------------
    def review_pr(self, pr: PullRequest) -> ReviewOutcome:
        decision = self.decide(pr)
        if decision.action != ACTION_REVIEW:
            return ReviewOutcome(decision.action)  # transient skip: no DB row

        ts = self._now().strftime("%Y-%m-%dT%H:%MZ")
        model = self.cfg.deepseek.model

        raw = self.gh.get_pr_diff(pr.repo, pr.number)
        if len(raw.encode("utf-8")) > self.cfg.diff.max_diff_bytes:
            return self._handle_oversized(
                pr, ts, f"Raw diff exceeds {self.cfg.diff.max_diff_bytes:,} bytes."
            )

        fd = filter_diff(raw, list(self.cfg.diff.skip_globs))
        if fd.changed_lines == 0:
            self.store.record(pr.repo, pr.number, pr.head_sha, ACTION_SKIP_EMPTY, model=model)
            return ReviewOutcome(ACTION_SKIP_EMPTY)

        user_prompt = build_user_prompt(pr, fd)
        est_tokens = estimate_input_tokens(SYSTEM_PROMPT + user_prompt)
        if est_tokens > self.cfg.diff.per_run_input_token_cap:
            return self._handle_oversized(
                pr, ts,
                f"Estimated input ~{est_tokens:,} tokens exceeds the "
                f"{self.cfg.diff.per_run_input_token_cap:,} token cap.",
            )

        cutoff = self._now() - timedelta(hours=24)
        if self.store.usd_spent_since(cutoff) >= self.cfg.budgets.daily_usd_budget:
            return self._handle_budget(pr, ts)

        # Spend money.
        t0 = time.monotonic()
        try:
            result = self.deepseek.review(SYSTEM_PROMPT, user_prompt)
        except DeepSeekError as e:
            self.store.record(pr.repo, pr.number, pr.head_sha, ACTION_ERROR, model=model, error=str(e))
            log.error("deepseek failed repo=%s pr=%s: %s", pr.repo, pr.number, e)
            return ReviewOutcome(ACTION_ERROR)
        latency_s = time.monotonic() - t0

        if self.bus:
            self.bus.publish(DeepSeekDone(
                repo=pr.repo, pr_number=pr.number,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                latency_s=latency_s, snippet=result.content[:280], title=pr.title,
            ))

        cost = estimate_cost_usd(result.usage, self.cfg.deepseek.prices)
        body = comment_mod.build_comment(
            content=result.content,
            pr=pr,
            model=model,
            timestamp=ts,
            kept_files=len(fd.kept_paths),
            skipped_files=fd.skipped_paths,
            changed_lines=fd.changed_lines,
        )

        # Post FIRST, then record — a failed post must not look "done".
        try:
            url = self.gh.post_comment(pr.repo, pr.number, body)
        except GhError as e:
            self.store.record(pr.repo, pr.number, pr.head_sha, ACTION_ERROR, model=model, error=str(e))
            log.error("post failed repo=%s pr=%s: %s", pr.repo, pr.number, e)
            return ReviewOutcome(ACTION_ERROR)

        self.store.record(
            pr.repo, pr.number, pr.head_sha, "reviewed",
            comment_url=url, usage=result.usage, cost_usd=cost, model=model,
        )
        return ReviewOutcome(ACTION_REVIEW, cost_usd=cost, comment_url=url)

    # -- guardrail handlers ---------------------------------------------
    def _handle_oversized(self, pr: PullRequest, ts: str, detail: str) -> ReviewOutcome:
        return self._notice_or_skip(pr, ts, ACTION_SKIP_OVERSIZED, "oversized", detail,
                                    self.cfg.diff.oversized_behavior)

    def _handle_budget(self, pr: PullRequest, ts: str) -> ReviewOutcome:
        detail = f"Daily review budget of ${self.cfg.budgets.daily_usd_budget:.2f} reached."
        return self._notice_or_skip(pr, ts, ACTION_SKIP_BUDGET, "budget", detail,
                                    self.cfg.budgets.budget_exceeded_behavior)

    def _notice_or_skip(self, pr, ts, action, kind, detail, behavior) -> ReviewOutcome:
        model = self.cfg.deepseek.model
        url = None
        if behavior == "notice":
            body = comment_mod.build_notice_comment(
                kind=kind, pr=pr, model=model, timestamp=ts, detail=detail
            )
            try:
                url = self.gh.post_comment(pr.repo, pr.number, body)
            except GhError as e:
                log.error("notice post failed repo=%s pr=%s: %s", pr.repo, pr.number, e)
        self.store.record(pr.repo, pr.number, pr.head_sha, action, comment_url=url, model=model)
        return ReviewOutcome(action, comment_url=url)
