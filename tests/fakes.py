"""Test doubles + a Config builder so unit tests need no network or real gh."""

from __future__ import annotations

import threading

from ghcr.config import (
    BudgetConfig,
    Config,
    DeepSeekConfig,
    DiffConfig,
    GithubConfig,
    ReviewModeConfig,
    ReviewPolicy,
)
from ghcr.prompts import LENS_NAMES
from ghcr.cost import Prices
from ghcr.github import GhError
from ghcr.models import PullRequest, ReviewResult, Usage

DEFAULT_SKIP = ("**/*.lock", "**/*.png", "**/dist/**")


def make_config(
    *,
    db_path: str,
    bot_login: str = "reviewbot",
    ignore_authors=("dependabot[bot]",),
    skip_drafts: bool = True,
    review_backlog_on_start: bool = False,
    max_diff_bytes: int = 400_000,
    per_run_input_token_cap: int = 250_000,
    oversized_behavior: str = "notice",
    daily_usd_budget: float = 5.0,
    budget_exceeded_behavior: str = "notice",
    skip_globs=DEFAULT_SKIP,
    model: str = "deepseek-v4-pro",
    review_mode: str = "single",
    lenses=LENS_NAMES,
    confidence_threshold: int = 80,
    scoring_votes: int = 1,
    max_parallel: int = 0,
    test_globs=("**/test_*.py", "**/tests/**"),
    read_prior_comments: bool = True,
    prior_comment_max_chars: int = 6000,
    rereview_on_mention: bool = True,
) -> Config:
    return Config(
        github=GithubConfig(gh_path="/usr/bin/true", token="t", bot_login=bot_login, request_timeout_seconds=60),
        deepseek=DeepSeekConfig(
            api_key="k", base_url="https://api.deepseek.com", model=model,
            thinking="enabled", reasoning_effort="high", request_timeout_seconds=600,
            prices=Prices(input_per_1m=0.28, output_per_1m=3.48),
        ),
        repos=("owner/repo",),
        poll_interval_seconds=120,
        review_policy=ReviewPolicy(
            skip_drafts=skip_drafts,
            review_backlog_on_start=review_backlog_on_start,
            ignore_authors=frozenset(ignore_authors),
        ),
        diff=DiffConfig(
            max_diff_bytes=max_diff_bytes,
            per_run_input_token_cap=per_run_input_token_cap,
            skip_globs=tuple(skip_globs),
            oversized_behavior=oversized_behavior,
            test_globs=tuple(test_globs),
        ),
        budgets=BudgetConfig(daily_usd_budget=daily_usd_budget, budget_exceeded_behavior=budget_exceeded_behavior),
        review=ReviewModeConfig(
            mode=review_mode,
            lenses=tuple(lenses),
            confidence_threshold=confidence_threshold,
            scoring_votes=scoring_votes,
            max_parallel=max_parallel,
            read_prior_comments=read_prior_comments,
            prior_comment_max_chars=prior_comment_max_chars,
            rereview_on_mention=rereview_on_mention,
        ),
        db_path=db_path,
        log_level="INFO",
    )


def make_pr(**kw) -> PullRequest:
    base = dict(repo="owner/repo", number=1, head_sha="a" * 40, title="T", author="alice", is_draft=False)
    base.update(kw)
    return PullRequest(**base)


class FakeGhClient:
    def __init__(self, *, diff="", who="reviewbot", prs=None, post_raises=False,
                 issue_comments=None, review_comments=None, comments_raise=False):
        self.diff = diff
        self.who = who
        self.prs = prs or []
        self.post_raises = post_raises
        self.issue_comments = issue_comments or []
        self.review_comments = review_comments or []
        self.comments_raise = comments_raise
        self.posted: list[tuple[str, int, str]] = []
        self.diff_calls = 0
        self.comment_calls = 0

    def whoami(self):
        return self.who

    def get_issue_comments(self, repo, number):
        self.comment_calls += 1
        if self.comments_raise:
            raise GhError(["api", "issues"], 1, "boom")
        return list(self.issue_comments)

    def get_review_comments(self, repo, number):
        self.comment_calls += 1
        if self.comments_raise:
            raise GhError(["api", "pulls"], 1, "boom")
        return list(self.review_comments)

    def list_open_prs(self, repo, limit=100):
        return list(self.prs)

    def get_pr(self, repo, number):
        return make_pr(repo=repo, number=number)

    def get_pr_diff(self, repo, number):
        self.diff_calls += 1
        return self.diff

    def post_comment(self, repo, number, body):
        if self.post_raises:
            raise GhError(["pr", "comment"], 1, "boom")
        self.posted.append((repo, number, body))
        return f"https://github.com/{repo}/pull/{number}#issuecomment-1"


class FakeDeepSeekClient:
    """Routes a canned response by matching a substring of the system prompt.

    ``responses`` maps a substring (e.g. "## LENS: security" or "## PASS: scoring")
    to either a string or a ``callable(system, user) -> str`` (so scoring can vary
    by the finding embedded in the user prompt). Unmatched calls fall back to
    ``content`` — keeping single-pass tests unchanged. The call counter and seen
    log are lock-guarded because the multi-pass pipeline calls from a thread pool.
    """

    def __init__(self, *, content="## Summary\nlooks fine", usage=None, raises=False, responses=None):
        self.content = content
        self.usage = usage or Usage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500)
        self.raises = raises
        self.responses = responses or {}
        self._lock = threading.Lock()
        self.calls = 0
        self.systems: list[str] = []
        self.users: list[str] = []

    def review(self, system_prompt, user_prompt):
        with self._lock:
            self.calls += 1
            self.systems.append(system_prompt)
            self.users.append(user_prompt)
        if self.raises:
            from ghcr.deepseek import DeepSeekError

            raise DeepSeekError("api down")
        return ReviewResult(content=self._route(system_prompt, user_prompt), usage=self.usage, model="deepseek-v4-pro")

    def _route(self, system_prompt, user_prompt):
        for key, val in self.responses.items():
            if key in system_prompt:
                return val(system_prompt, user_prompt) if callable(val) else val
        return self.content

    def calls_matching(self, substr: str) -> int:
        with self._lock:
            return sum(1 for s in self.systems if substr in s)

    def user_for(self, sys_substr: str) -> list[str]:
        """User prompts paired with system prompts containing ``sys_substr``."""
        with self._lock:
            return [u for s, u in zip(self.systems, self.users) if sys_substr in s]
