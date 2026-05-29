"""Test doubles + a Config builder so unit tests need no network or real gh."""

from __future__ import annotations

from ghcr.config import (
    BudgetConfig,
    Config,
    DeepSeekConfig,
    DiffConfig,
    GithubConfig,
    ReviewPolicy,
)
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
        ),
        budgets=BudgetConfig(daily_usd_budget=daily_usd_budget, budget_exceeded_behavior=budget_exceeded_behavior),
        db_path=db_path,
        log_level="INFO",
    )


def make_pr(**kw) -> PullRequest:
    base = dict(repo="owner/repo", number=1, head_sha="a" * 40, title="T", author="alice", is_draft=False)
    base.update(kw)
    return PullRequest(**base)


class FakeGhClient:
    def __init__(self, *, diff="", who="reviewbot", prs=None, post_raises=False):
        self.diff = diff
        self.who = who
        self.prs = prs or []
        self.post_raises = post_raises
        self.posted: list[tuple[str, int, str]] = []
        self.diff_calls = 0

    def whoami(self):
        return self.who

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
    def __init__(self, *, content="## Summary\nlooks fine", usage=None, raises=False):
        self.content = content
        self.usage = usage or Usage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500)
        self.raises = raises
        self.calls = 0

    def review(self, system_prompt, user_prompt):
        self.calls += 1
        if self.raises:
            from ghcr.deepseek import DeepSeekError

            raise DeepSeekError("api down")
        return ReviewResult(content=self.content, usage=self.usage, model="deepseek-v4-pro")
