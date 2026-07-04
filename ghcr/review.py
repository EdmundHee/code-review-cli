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
from .cost import estimate_cost_usd, estimate_input_tokens, per_chunk_diff_budget
from .deepseek import DeepSeekError
from .events import AgentEvent, DeepSeekDone
from .diff_filter import DiffChunk, chunk_filtered_diff, chunk_view, filter_diff
from .github import GhError
from .models import (
    ACTION_ERROR,
    ACTION_REREVIEWED,
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
    ReferencedSnippet,
    ReviewDecision,
    Usage,
    merge_usages,
)
from .context_request import (
    extract_symbol_snippet,
    module_path_candidates,
    parse_context_requests,
    render_referenced_context,
)
from .pipeline import (
    classify_test_signal,
    dedup_findings,
    merge_coverage,
    parse_lens_payload,
    parse_score,
    synthesize_markdown,
)
from .prior_context import build_prior_context, find_mention_triggers, max_comment_id
from .prompts import (
    CONTEXT_REQUEST_PROMPT,
    LENS_PROMPTS,
    SCORING_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_context_request_user_prompt,
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
    def __init__(self, gh, deepseek, store, config: Config, now=None, bus=None, advisor=None):
        self.gh = gh
        self.deepseek = deepseek            # worker: lenses + single mode
        self.advisor = advisor or deepseek   # planner + scoring (defaults to worker)
        self.store = store
        self.cfg = config
        self.bus = bus
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.worker_prices = config.deepseek.prices
        self.advisor_prices = getattr(self.advisor, "prices", config.deepseek.prices)

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
    def review_pr(self, pr: PullRequest, dry_run: bool = False, trigger: str = "push",
                  prior_comments=None, trigger_note: str = "") -> ReviewOutcome:
        # dry_run bypasses the skip gates (the operator picked this PR on purpose)
        # and never posts or records — it builds the review and returns the body.
        # trigger="mention" is an @mention re-review: skip the head-SHA seen-gate
        # (caller already decided to act) and record a distinct outcome.
        if not dry_run and trigger == "push":
            decision = self.decide(pr)
            if decision.action != ACTION_REVIEW:
                return ReviewOutcome(decision.action)  # transient skip: no DB row

        ts = self._now().strftime("%Y-%m-%dT%H:%MZ")
        model = self.cfg.deepseek.model
        record_outcome = ACTION_REREVIEWED if trigger == "mention" else "reviewed"

        raw = self.gh.get_pr_diff(pr.repo, pr.number)
        # Sanity ceiling on the RAW diff (pathological PRs; gh buffers the whole
        # diff into memory). The real review-size cap is measured post-filter on
        # kept_bytes — junk the filter strips must not disqualify a PR.
        if len(raw.encode("utf-8")) > self.cfg.diff.hard_max_diff_bytes:
            return self._handle_oversized(
                pr, ts,
                f"Raw diff exceeds the {self.cfg.diff.hard_max_diff_bytes:,}-byte hard ceiling.",
            )

        fd = filter_diff(raw, list(self.cfg.diff.skip_globs))
        if fd.changed_lines == 0:
            if not dry_run:
                self.store.record(pr.repo, pr.number, pr.head_sha, ACTION_SKIP_EMPTY, model=model)
            return ReviewOutcome(ACTION_SKIP_EMPTY)

        # Multi mode with chunking on reviews any post-filter size (split into
        # chunks inside _review_multi); everything else keeps the hard cap.
        chunkable = self.cfg.review.mode == "multi" and self.cfg.diff.chunk_reviews
        if not chunkable and fd.kept_bytes > self.cfg.diff.max_diff_bytes:
            return self._handle_oversized(
                pr, ts, f"Filtered diff exceeds {self.cfg.diff.max_diff_bytes:,} bytes."
            )

        # Existing PR conversation as context (best-effort; failure → empty block).
        # A mention re-review passes the comments it already fetched, to avoid a re-fetch.
        if self.cfg.review.read_prior_comments:
            comments = prior_comments if prior_comments is not None else (self._fetch_pr_comments(pr) or [])
        else:
            comments = []
        prior_ctx = build_prior_context(
            comments,
            bot_login=self.cfg.github.bot_login,
            max_chars=self.cfg.review.prior_comment_max_chars,
        )
        user_prompt = build_user_prompt(pr, fd, prior_context=prior_ctx)

        if self.cfg.review.mode == "multi":
            return self._review_multi(pr, fd, user_prompt, ts, model, dry_run=dry_run,
                                      prior_ctx=prior_ctx, record_outcome=record_outcome,
                                      trigger_note=trigger_note)

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

        cost = estimate_cost_usd(result.usage, self.worker_prices)
        body = comment_mod.build_comment(
            content=result.content,
            pr=pr,
            model=model,
            timestamp=ts,
            kept_files=len(fd.kept_paths),
            skipped_files=fd.skipped_paths,
            changed_lines=fd.changed_lines,
            trigger_note=trigger_note,
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
            pr.repo, pr.number, pr.head_sha, record_outcome,
            comment_url=url, usage=result.usage, cost_usd=cost, model=model,
        )
        return ReviewOutcome(ACTION_REVIEW, cost_usd=cost, comment_url=url)

    def _fetch_pr_comments(self, pr: PullRequest):
        """Existing PR comments (issue timeline + inline review). Best-effort: any
        ``gh`` failure yields ``None`` (distinct from ``[]`` = genuinely no comments)
        so callers can tell a fetch error from an empty PR — a comment-fetch problem
        must never block or fail the review. NOT gated by config; callers apply their
        own feature toggle."""
        try:
            issue = self.gh.get_issue_comments(pr.repo, pr.number)
            review = self.gh.get_review_comments(pr.repo, pr.number)
        except GhError as e:
            log.warning("comment fetch failed repo=%s pr=%s: %s", pr.repo, pr.number, e)
            return None
        return list(issue) + list(review)

    def _fetch_referenced_context(self, pr: PullRequest, fd, agent_suffix: str = "") -> tuple[str, list[Usage]]:
        """Resolve the definitions of symbols the diff references but does not define.

        A planner model call lists the symbols; each is resolved to repo source (hint or
        code search) and fetched at the PR head SHA. Returns the rendered block + the
        planner's usage. Best-effort and gated by config: disabled or any failure yields
        ``("", [])`` so the review proceeds diff-only — a fetch must never fail a review."""
        rc = self.cfg.review
        if not rc.fetch_referenced_context:
            return "", []
        agent = f"context{agent_suffix}"
        # Announce the planner as a sub-agent so the TUI shows "reviewing #N" from
        # the moment it starts — this is the one slow pre-lens step, and without a
        # signal the row sits on bare "polling…" for its whole (~minute) duration.
        self._emit_agent(pr, agent, "running")
        try:
            # thinking disabled: listing symbols needs no deep reasoning, and the
            # reasoning tokens bill as output — the planner is pure overhead there.
            res = self.advisor.review(
                CONTEXT_REQUEST_PROMPT, build_context_request_user_prompt(pr, fd), thinking="disabled"
            )
        except DeepSeekError as e:
            log.warning("context planner failed repo=%s pr=%s: %s", pr.repo, pr.number, e)
            self._emit_agent(pr, agent, "failed", "planner api error")
            return "", []
        requests = parse_context_requests(res.content)[: rc.referenced_max_symbols]
        snippets: list[ReferencedSnippet] = []
        unresolved: list[str] = []
        for req in requests:
            s = self._resolve_symbol(pr, req)
            if s:
                snippets.append(s)
            else:
                # Surfaced in the rendered block: an unresolved symbol must stay
                # visibly unknown so the scorer's cap-at-25 rule can fire on it.
                unresolved.append(req.symbol)
        block = render_referenced_context(
            snippets, max_chars=rc.referenced_context_max_chars, unresolved=tuple(unresolved)
        )
        self._emit_agent(pr, agent, "done", f"{len(snippets)}/{len(requests)} symbols")
        return block, [res.usage]

    def _resolve_symbol(self, pr: PullRequest, req) -> ReferencedSnippet | None:
        """Resolve one ContextRequest to a ReferencedSnippet, or None. Hybrid: try the
        module hint's path, else code search; fetch at the head SHA; slice the definition.
        Every ``gh`` call is best-effort — a GhError just means the symbol is unresolved."""
        text, path = None, None
        for hint_path in module_path_candidates(req.module_hint):
            try:
                text, path = self.gh.get_file_content(pr.repo, hint_path, pr.head_sha), hint_path
                break
            except GhError:
                text = None
        if text is None:
            try:
                hits = self.gh.search_code(pr.repo, req.symbol, self.cfg.review.referenced_search_limit)
            except GhError:
                hits = []
            path = hits[0].get("path") if hits and isinstance(hits[0], dict) else None
            if path:
                try:
                    text = self.gh.get_file_content(pr.repo, path, pr.head_sha)
                except GhError:
                    text = None
        if not text or not path:
            return None
        got = extract_symbol_snippet(text, req.symbol)
        if not got:
            return None
        body, kind = got
        return ReferencedSnippet(symbol=req.symbol, path=path, text=body, kind=kind)

    def rereview_if_mentioned(self, pr: PullRequest, just_reviewed: bool = False) -> ReviewOutcome | None:
        """Fire a full re-review when a new comment @mentions the bot.

        Watermark (``store.last_mention_id``) dedups across cycles. First sight of a
        PR baselines silently (never fires on historical @mentions — avoids a deploy
        storm). A failed comment fetch (``None``) skips the cycle entirely, so a
        transient error at first sight never baselines at 0 and replays every historical
        @mention once the fetch recovers. ``just_reviewed`` True means the head-SHA path
        already produced a comment-aware review this cycle, so we advance the watermark
        without firing.
        """
        if not self.cfg.review.rereview_on_mention:
            return None
        comments = self._fetch_pr_comments(pr)
        if comments is None:  # fetch failed — can't baseline or detect; retry next cycle
            return None
        top = max_comment_id(comments)
        wm = self.store.last_mention_id(pr.repo, pr.number)
        if wm is None:
            self.store.set_mention_id(pr.repo, pr.number, top)  # baseline, no fire
            return None
        new = find_mention_triggers(comments, bot_login=self.cfg.github.bot_login, after_id=wm)
        outcome = None
        if new and not just_reviewed:
            latest = max(new, key=lambda c: c.comment_id)
            note = f"Re-review requested by @{latest.author}" if latest.author else "Re-review requested"
            log.info("re-review triggered by @mention repo=%s pr=%s by=%s", pr.repo, pr.number, latest.author)
            outcome = self.review_pr(pr, trigger="mention", prior_comments=comments, trigger_note=note)
        self.store.set_mention_id(pr.repo, pr.number, max(wm, top))
        return outcome

    # -- multi-pass pipeline --------------------------------------------
    def _review_multi(self, pr: PullRequest, fd, user_prompt: str, ts: str, model: str, dry_run: bool = False,
                      prior_ctx: str = "", record_outcome: str = "reviewed", trigger_note: str = "") -> ReviewOutcome:
        """Fan out diff-only review lenses, dedup, score each finding for
        confidence, then synthesize + post. All model calls run in a thread pool;
        store/bus writes happen only here on the main thread."""
        rc = self.cfg.review
        dc = self.cfg.diff
        lenses = list(rc.lenses)

        # Decide chunking BEFORE any spend. A chunk must satisfy the byte cap AND
        # the per-run token sum gate below — chunking at max_diff_bytes alone would
        # still trip the token gate (it binds first; see per_chunk_diff_budget).
        chunks: list[DiffChunk] = []
        if dc.chunk_reviews:
            chunk_budget = per_chunk_diff_budget(
                dc.max_diff_bytes, dc.per_run_input_token_cap, len(lenses),
                self._lens_overhead_tokens(pr, fd, lenses, prior_ctx),
            )
            if chunk_budget <= 0:
                return self._handle_oversized(
                    pr, ts,
                    f"per_run_input_token_cap ({dc.per_run_input_token_cap:,}) is too small "
                    "for the review prompts alone; cannot size a review chunk.",
                )
            if fd.kept_bytes > chunk_budget:
                chunks = chunk_filtered_diff(fd.text, chunk_budget)

        if not chunks:
            # Context-size gate: the diff is re-sent to every lens, so estimate the sum.
            # Chunked reviews skip it — every chunk fits by construction, and a mid-loop
            # _handle_oversized would write a terminal row after money was spent.
            est = sum(estimate_input_tokens(LENS_PROMPTS[ln] + user_prompt) for ln in lenses)
            if est > dc.per_run_input_token_cap:
                return self._handle_oversized(
                    pr, ts,
                    f"Estimated multi-pass input ~{est:,} tokens across {len(lenses)} lenses "
                    f"exceeds the {dc.per_run_input_token_cap:,} token cap.",
                )

        if not dry_run:
            cutoff = self._now() - timedelta(hours=24)
            if self.store.usd_spent_since(cutoff) >= self.cfg.budgets.daily_usd_budget:
                return self._handle_budget(pr, ts)

        # Spend money.
        t0 = time.monotonic()
        # Whole-PR signal even when chunked: the hint must say "this PR ships tests"
        # even if the tests sit in a different chunk than the source.
        has_source, has_test = classify_test_signal(fd, dc.test_globs)

        to_review = chunks[: dc.max_review_chunks]
        unreviewed_files = [p for ch in chunks[dc.max_review_chunks:] for p in ch.paths]
        truncated_files = [p for ch in to_review for p in ch.truncated_paths]

        worker_usages: list[Usage] = []
        advisor_usages: list[Usage] = []
        lens_errors: list[str] = []
        coverages: list = []
        raw_findings: list = []
        total_calls = failed_calls = 0
        # Scoring context per finding: its file's chunk text + that chunk's referenced
        # definitions, so the scoring fallback is bounded by ONE chunk, never the full text.
        score_ctx: dict[str, tuple[str, str]] = {}
        default_ctx = (fd.text, "")

        if not chunks:
            # Fetch the real definitions of symbols the diff references but does not show,
            # so the lenses + scorer judge against ground truth instead of guessing. Best-
            # effort and gated by config; on any failure the review proceeds diff-only.
            ref_ctx, ref_usages = self._fetch_referenced_context(pr, fd)
            if ref_ctx:
                user_prompt = build_user_prompt(pr, fd, prior_context=prior_ctx, referenced_context=ref_ctx)

            lens_results: list[LensResult] = self._map_parallel(
                lenses, lambda ln: self._run_one_lens(ln, pr, fd, user_prompt, has_source, has_test)
            )
            worker_usages = [lr.usage for lr in lens_results]
            advisor_usages = list(ref_usages)
            lens_errors = [lr.lens for lr in lens_results if not lr.ok]
            coverages = [lr.coverage for lr in lens_results]
            raw_findings = [f for lr in lens_results if lr.ok for f in lr.findings]
            total_calls, failed_calls = len(lenses), len(lens_errors)
            default_ctx = (fd.text, ref_ctx)
        else:
            # Sequential chunk loop on the main thread; only the lens fan-out is pooled.
            # No size gate inside the loop — every chunk fits the caps by construction.
            n = len(to_review)
            log.info("chunked review repo=%s pr=%s: %d chunks (%d bytes filtered)",
                     pr.repo, pr.number, len(chunks), fd.kept_bytes)
            for i, ch in enumerate(to_review, start=1):
                cfd = chunk_view(fd, ch)
                ref_ctx, ref_usages = self._fetch_referenced_context(pr, cfd, agent_suffix=f"·c{i}")
                up = build_user_prompt(pr, cfd, prior_context=prior_ctx, referenced_context=ref_ctx,
                                       chunk_index=i, chunk_total=n)
                lens_results = self._map_parallel(
                    lenses,
                    lambda ln, cfd=cfd, up=up, i=i: self._run_one_lens(
                        ln, pr, cfd, up, has_source, has_test, agent_suffix=f"·c{i}"
                    ),
                )
                worker_usages += [lr.usage for lr in lens_results]
                advisor_usages += list(ref_usages)
                errs = [f"{lr.lens} (part {i}/{n})" for lr in lens_results if not lr.ok]
                lens_errors += errs
                coverages += [lr.coverage for lr in lens_results]
                raw_findings += [f for lr in lens_results if lr.ok for f in lr.findings]
                total_calls += len(lenses)
                failed_calls += len(errs)
                for p in ch.paths:
                    score_ctx[p] = (ch.text, ref_ctx)
                if i == 1:
                    default_ctx = (ch.text, ref_ctx)  # bounded fallback for hallucinated paths

        coverage = merge_coverage(coverages)

        # Every call failed → record error (cost still counted), do not post.
        if failed_calls == total_calls:
            worker_u, advisor_u = self._provider_usage(worker_usages, advisor_usages)
            cost = self._split_cost(worker_usages, advisor_usages)
            if not dry_run:
                self.store.record(
                    pr.repo, pr.number, pr.head_sha, ACTION_ERROR,
                    model=model, usage=worker_u, advisor_usage=advisor_u,
                    cost_usd=cost, error="all review lenses failed",
                )
            log.error("all lenses failed repo=%s pr=%s", pr.repo, pr.number)
            return ReviewOutcome(ACTION_ERROR, cost_usd=cost)

        findings = [replace(f, id=i) for i, f in enumerate(dedup_findings(raw_findings))]

        # Score each finding (parallel); keep only high-confidence survivors.
        # Each finding is scored against ITS chunk's text + referenced definitions
        # (unchunked: the whole filtered diff — identical to the old behavior).
        survivors: list = []
        scored_total = 0
        if findings:
            def _score(f):
                ctext, rctx = score_ctx.get(f.file.strip(), default_ctx)
                return self._score_finding(f, pr, replace(fd, text=ctext), rc.scoring_votes, prior_ctx, rctx)

            scored = self._map_parallel(findings, _score)
            for f, conf, reason, score_usages in scored:
                advisor_usages.extend(score_usages)
                if conf is None:
                    continue
                scored_total += 1  # got a score → counts toward the dropped tally
                if conf >= rc.confidence_threshold:
                    survivors.append(replace(f, confidence=conf, reason=reason))
            survivors.sort(key=lambda f: (_SEV_RANK.get(f.severity, 9), f.id))

        latency_s = time.monotonic() - t0
        worker_u, advisor_u = self._provider_usage(worker_usages, advisor_usages)
        cost = self._split_cost(worker_usages, advisor_usages)
        content = synthesize_markdown(
            survivors, coverage, lens_errors=lens_errors,
            scored_total=scored_total, threshold=rc.confidence_threshold,
            unreviewed_files=unreviewed_files,
        )

        if self.bus:
            self.bus.publish(DeepSeekDone(
                repo=pr.repo, pr_number=pr.number,
                prompt_tokens=worker_u.prompt_tokens, completion_tokens=worker_u.completion_tokens,
                advisor_prompt_tokens=advisor_u.prompt_tokens, advisor_completion_tokens=advisor_u.completion_tokens,
                latency_s=latency_s, snippet=content[:280], title=pr.title,
            ))

        body = comment_mod.build_comment(
            content=content, pr=pr, model=model, timestamp=ts,
            kept_files=len(fd.kept_paths), skipped_files=fd.skipped_paths, changed_lines=fd.changed_lines,
            trigger_note=trigger_note,
            chunks=len(to_review) if chunks else 1, truncated_files=truncated_files,
        )

        if dry_run:
            return ReviewOutcome(ACTION_REVIEW, cost_usd=cost, body=body)

        # Post FIRST, then record — a failed post must not look "done".
        try:
            url = self.gh.post_comment(pr.repo, pr.number, body)
        except GhError as e:
            self.store.record(pr.repo, pr.number, pr.head_sha, ACTION_ERROR, model=model,
                              usage=worker_u, advisor_usage=advisor_u, cost_usd=cost, error=str(e))
            log.error("post failed repo=%s pr=%s: %s", pr.repo, pr.number, e)
            return ReviewOutcome(ACTION_ERROR, cost_usd=cost)

        self.store.record(
            pr.repo, pr.number, pr.head_sha, record_outcome,
            comment_url=url, usage=worker_u, advisor_usage=advisor_u, cost_usd=cost, model=model,
        )
        return ReviewOutcome(ACTION_REVIEW, cost_usd=cost, comment_url=url)

    def _provider_usage(self, worker_usages, advisor_usages):
        """Split usage by real provider. When there is no DISTINCT advisor (advisor IS
        the worker), planner+scoring tokens are the worker's — fold them in and report
        zero advisor usage, so a DeepSeek-only review shows no phantom advisor tokens."""
        if self.advisor is self.deepseek:
            return merge_usages(worker_usages + advisor_usages), Usage()
        return merge_usages(worker_usages), merge_usages(advisor_usages)

    def _split_cost(self, worker_usages, advisor_usages) -> float:
        """Bill each provider's usage at its own price. When advisor IS the worker
        (default), both prices are deepseek's → identical to a single-price total."""
        return (
            estimate_cost_usd(merge_usages(worker_usages), self.worker_prices)
            + estimate_cost_usd(merge_usages(advisor_usages), self.advisor_prices)
        )

    def _lens_overhead_tokens(self, pr, fd, lenses, prior_ctx: str) -> int:
        """Everything a lens call sends EXCEPT the diff itself: the lens system
        prompts plus, per lens, the diff-less user prompt (headers + prior
        discussion) and a reserve for the per-chunk referenced-context block.
        Pure string math (no spend) — feeds per_chunk_diff_budget."""
        base_up = build_user_prompt(pr, replace(fd, text=""), prior_context=prior_ctx)
        reserve = self.cfg.review.referenced_context_max_chars // 4
        return (
            sum(estimate_input_tokens(LENS_PROMPTS[ln]) for ln in lenses)
            + len(lenses) * (estimate_input_tokens(base_up) + reserve)
        )

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
        the bus is thread-safe; no store access here. Model derived from the agent
        kind: scorers (``score:*``) and the planner (``context``) run on the advisor;
        lenses run on the worker."""
        if self.bus:
            model = (
                self.advisor.model
                if (agent.startswith("score") or agent.startswith("context"))
                else self.deepseek.model
            )
            self.bus.publish(AgentEvent(
                repo=pr.repo, pr_number=pr.number, agent=agent, status=status,
                detail=detail, title=pr.title, model=model,
            ))

    def _run_one_lens(self, lens: str, pr, fd, user_prompt: str, has_source: bool, has_test: bool,
                      agent_suffix: str = "") -> LensResult:
        # agent_suffix distinguishes per-chunk runs on the TUI agents board
        # (keyed by name — unsuffixed chunk runs would overwrite each other).
        agent = f"lens:{lens}{agent_suffix}"
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

    def _score_finding(self, finding, pr, fd, votes: int, prior_ctx: str = "", referenced_ctx: str = ""):
        """Score one finding with ``votes`` independent calls; return
        ``(finding, median_confidence|None, reason, [usages])``. ``prior_ctx`` lets
        the scorer return 0 for a finding already raised in the PR's discussion;
        ``referenced_ctx`` supplies the fetched definitions it scores against."""
        agent = f"score:#{finding.id}"
        self._emit_agent(pr, agent, "running", f"{finding.severity} {finding.file}")
        user = build_scoring_user_prompt(pr, fd, finding, prior_context=prior_ctx, referenced_context=referenced_ctx)
        confs: list[int] = []
        reason = ""
        usages: list[Usage] = []
        for _ in range(votes):
            try:
                res = self.advisor.review(SCORING_SYSTEM_PROMPT, user)
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
