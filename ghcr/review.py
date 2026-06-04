"""Orchestrate the review of one PR end-to-end.

The only module that composes I/O (gh + DeepSeek + state). Decision order is
cheap-first so we never pay for a review we can rule out, and the comment is
posted BEFORE the DB row is written (a failed post must not be recorded as done).
"""

from __future__ import annotations

import logging
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from . import comment as comment_mod
from .config import Config
from .cost import estimate_cost_usd, estimate_input_tokens
from .deepseek import DeepSeekError
from .events import AgentEvent, DeepSeekDone
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
    SEVERITY_ORDER,
    LensResult,
    PullRequest,
    ReviewDecision,
    Usage,
    merge_usages,
)
from .pipeline import (
    classify_test_signal,
    dedup_findings,
    parse_lens_payload,
    parse_score,
    synthesize_markdown,
)
from .prior_context import build_prior_context
from .prompts import (
    LENS_PROMPTS,
    SCORING_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_scoring_user_prompt,
    build_user_prompt,
    coverage_hint,
)

_SEV_RANK = {s: i for i, s in enumerate(SEVERITY_ORDER)}

log = logging.getLogger("ghcr.review")


@dataclass
class ReviewOutcome:
    action: str
    cost_usd: float = 0.0
    comment_url: str | None = None
    body: str | None = None  # populated on dry-run (built but not posted)


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
    def review_pr(self, pr: PullRequest, dry_run: bool = False) -> ReviewOutcome:
        # dry_run bypasses the skip gates (the operator picked this PR on purpose)
        # and never posts or records — it builds the review and returns the body.
        if not dry_run:
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
            if not dry_run:
                self.store.record(pr.repo, pr.number, pr.head_sha, ACTION_SKIP_EMPTY, model=model)
            return ReviewOutcome(ACTION_SKIP_EMPTY)

        # Existing PR conversation as context (best-effort; failure → empty block).
        prior_ctx = build_prior_context(
            self._fetch_prior_comments(pr),
            bot_login=self.cfg.github.bot_login,
            max_chars=self.cfg.review.prior_comment_max_chars,
        )
        user_prompt = build_user_prompt(pr, fd, prior_context=prior_ctx)

        if self.cfg.review.mode == "multi":
            return self._review_multi(pr, fd, user_prompt, ts, model, dry_run=dry_run, prior_ctx=prior_ctx)

        est_tokens = estimate_input_tokens(SYSTEM_PROMPT + user_prompt)
        if est_tokens > self.cfg.diff.per_run_input_token_cap:
            return self._handle_oversized(
                pr, ts,
                f"Estimated input ~{est_tokens:,} tokens exceeds the "
                f"{self.cfg.diff.per_run_input_token_cap:,} token cap.",
            )

        if not dry_run:
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

        if dry_run:
            return ReviewOutcome(ACTION_REVIEW, cost_usd=cost, body=body)

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

    def _fetch_prior_comments(self, pr: PullRequest):
        """Existing PR comments (issue timeline + inline review) as review context.

        Best-effort: disabled by config, or any ``gh`` failure, yields ``[]`` — a
        comment-fetch problem must never block or fail the review itself."""
        if not self.cfg.review.read_prior_comments:
            return []
        try:
            issue = self.gh.get_issue_comments(pr.repo, pr.number)
            review = self.gh.get_review_comments(pr.repo, pr.number)
        except GhError as e:
            log.warning("prior-comment fetch failed repo=%s pr=%s: %s", pr.repo, pr.number, e)
            return []
        return list(issue) + list(review)

    # -- multi-pass pipeline --------------------------------------------
    def _review_multi(self, pr: PullRequest, fd, user_prompt: str, ts: str, model: str, dry_run: bool = False, prior_ctx: str = "") -> ReviewOutcome:
        """Fan out diff-only review lenses, dedup, score each finding for
        confidence, then synthesize + post. All model calls run in a thread pool;
        store/bus writes happen only here on the main thread."""
        rc = self.cfg.review
        lenses = list(rc.lenses)

        # Context-size gate: the diff is re-sent to every lens, so estimate the sum.
        est = sum(estimate_input_tokens(LENS_PROMPTS[ln] + user_prompt) for ln in lenses)
        if est > self.cfg.diff.per_run_input_token_cap:
            return self._handle_oversized(
                pr, ts,
                f"Estimated multi-pass input ~{est:,} tokens across {len(lenses)} lenses "
                f"exceeds the {self.cfg.diff.per_run_input_token_cap:,} token cap.",
            )

        if not dry_run:
            cutoff = self._now() - timedelta(hours=24)
            if self.store.usd_spent_since(cutoff) >= self.cfg.budgets.daily_usd_budget:
                return self._handle_budget(pr, ts)

        # Spend money.
        t0 = time.monotonic()
        has_source, has_test = classify_test_signal(fd, self.cfg.diff.test_globs)

        lens_results: list[LensResult] = self._map_parallel(
            lenses, lambda ln: self._run_one_lens(ln, pr, fd, user_prompt, has_source, has_test)
        )

        usages: list[Usage] = [lr.usage for lr in lens_results]
        lens_errors = [lr.lens for lr in lens_results if not lr.ok]
        coverage = next((lr.coverage for lr in lens_results if lr.coverage is not None), None)
        raw_findings = [f for lr in lens_results if lr.ok for f in lr.findings]

        # Every lens failed → record error (cost still counted), do not post.
        if len(lens_errors) == len(lenses):
            usage = merge_usages(usages)
            cost = estimate_cost_usd(usage, self.cfg.deepseek.prices)
            if not dry_run:
                self.store.record(
                    pr.repo, pr.number, pr.head_sha, ACTION_ERROR,
                    model=model, usage=usage, cost_usd=cost, error="all review lenses failed",
                )
            log.error("all lenses failed repo=%s pr=%s", pr.repo, pr.number)
            return ReviewOutcome(ACTION_ERROR, cost_usd=cost)

        findings = [replace(f, id=i) for i, f in enumerate(dedup_findings(raw_findings))]

        # Score each finding (parallel); keep only high-confidence survivors.
        survivors: list = []
        scored_total = 0
        if findings:
            scored = self._map_parallel(findings, lambda f: self._score_finding(f, pr, fd, rc.scoring_votes, prior_ctx))
            for f, conf, reason, score_usages in scored:
                usages.extend(score_usages)
                if conf is None:
                    continue
                scored_total += 1  # got a score → counts toward the dropped tally
                if conf >= rc.confidence_threshold:
                    survivors.append(replace(f, confidence=conf, reason=reason))
            survivors.sort(key=lambda f: (_SEV_RANK.get(f.severity, 9), f.id))

        latency_s = time.monotonic() - t0
        usage = merge_usages(usages)
        cost = estimate_cost_usd(usage, self.cfg.deepseek.prices)
        content = synthesize_markdown(
            survivors, coverage, lens_errors=lens_errors,
            scored_total=scored_total, threshold=rc.confidence_threshold,
        )

        if self.bus:
            self.bus.publish(DeepSeekDone(
                repo=pr.repo, pr_number=pr.number,
                prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens,
                latency_s=latency_s, snippet=content[:280], title=pr.title,
            ))

        body = comment_mod.build_comment(
            content=content, pr=pr, model=model, timestamp=ts,
            kept_files=len(fd.kept_paths), skipped_files=fd.skipped_paths, changed_lines=fd.changed_lines,
        )

        if dry_run:
            return ReviewOutcome(ACTION_REVIEW, cost_usd=cost, body=body)

        # Post FIRST, then record — a failed post must not look "done".
        try:
            url = self.gh.post_comment(pr.repo, pr.number, body)
        except GhError as e:
            self.store.record(pr.repo, pr.number, pr.head_sha, ACTION_ERROR, model=model, usage=usage, cost_usd=cost, error=str(e))
            log.error("post failed repo=%s pr=%s: %s", pr.repo, pr.number, e)
            return ReviewOutcome(ACTION_ERROR, cost_usd=cost)

        self.store.record(
            pr.repo, pr.number, pr.head_sha, "reviewed",
            comment_url=url, usage=usage, cost_usd=cost, model=model,
        )
        return ReviewOutcome(ACTION_REVIEW, cost_usd=cost, comment_url=url)

    def _map_parallel(self, items, fn):
        """Run ``fn`` over ``items`` concurrently, preserving order. Workers must
        only call the model + pure helpers — never touch the store or bus."""
        if not items:
            return []
        workers = rc_max if (rc_max := self.cfg.review.max_parallel) > 0 else min(8, len(items))
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            return [f.result() for f in [ex.submit(fn, it) for it in items]]

    def _emit_agent(self, pr, agent: str, status: str, detail: str = "") -> None:
        """Publish a sub-agent state change for the TUI. Safe from pool threads —
        the bus is thread-safe; no store access here."""
        if self.bus:
            self.bus.publish(AgentEvent(
                repo=pr.repo, pr_number=pr.number, agent=agent, status=status, detail=detail, title=pr.title,
            ))

    def _run_one_lens(self, lens: str, pr, fd, user_prompt: str, has_source: bool, has_test: bool) -> LensResult:
        agent = f"lens:{lens}"
        self._emit_agent(pr, agent, "running")
        up = user_prompt + coverage_hint(has_source, has_test) if lens == "test_coverage" else user_prompt
        t0 = time.monotonic()
        try:
            res = self.deepseek.review(LENS_PROMPTS[lens], up)
        except DeepSeekError as e:
            self._emit_agent(pr, agent, "failed", "api error")
            return LensResult(lens=lens, findings=(), usage=Usage(), ok=False, raw=str(e))
        dt = time.monotonic() - t0
        parsed = parse_lens_payload(res.content, lens)
        if parsed is None:
            self._emit_agent(pr, agent, "failed", "unparseable JSON")
            return LensResult(lens=lens, findings=(), usage=res.usage, ok=False, raw=res.content)
        found, coverage = parsed
        self._emit_agent(
            pr, agent, "done",
            f"{res.usage.prompt_tokens}→{res.usage.completion_tokens} tok {dt:.0f}s · {len(found)} found",
        )
        return LensResult(lens=lens, findings=tuple(found), usage=res.usage, ok=True, raw=res.content, coverage=coverage)

    def _score_finding(self, finding, pr, fd, votes: int, prior_ctx: str = ""):
        """Score one finding with ``votes`` independent calls; return
        ``(finding, median_confidence|None, reason, [usages])``. ``prior_ctx`` lets
        the scorer return 0 for a finding already raised in the PR's discussion."""
        agent = f"score:#{finding.id}"
        self._emit_agent(pr, agent, "running", f"{finding.severity} {finding.file}")
        user = build_scoring_user_prompt(pr, fd, finding, prior_context=prior_ctx)
        confs: list[int] = []
        reason = ""
        usages: list[Usage] = []
        for _ in range(votes):
            try:
                res = self.deepseek.review(SCORING_SYSTEM_PROMPT, user)
            except DeepSeekError:
                continue
            usages.append(res.usage)
            parsed = parse_score(res.content)
            if parsed is not None:
                confs.append(parsed[0])
                reason = reason or parsed[1]
        conf = int(round(statistics.median(confs))) if confs else None
        self._emit_agent(
            pr, agent, "done" if conf is not None else "failed",
            f"confidence {conf}" if conf is not None else "no score",
        )
        return finding, conf, reason, usages

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
