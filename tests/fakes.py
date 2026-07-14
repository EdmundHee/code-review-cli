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
from ghcr.gitrepo import GitError
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
    chunk_reviews: bool = True,
    hard_max_diff_bytes: int = 5_000_000,
    max_review_chunks: int = 10,
    daily_usd_budget: float = 5.0,
    budget_exceeded_behavior: str = "notice",
    skip_globs=DEFAULT_SKIP,
    model: str = "deepseek-v4-pro",
    review_mode: str = "single",
    # Pinned to the original 4 lenses (NOT LENS_NAMES, which now includes
    # "consistency") so existing multi-pass call-count assertions stay stable;
    # production defaults to all of LENS_NAMES. Consistency tests opt in explicitly.
    lenses=("correctness", "security", "maintainability", "test_coverage"),
    confidence_threshold: int = 80,
    scoring_votes: int = 1,
    max_parallel: int = 0,
    test_globs=("**/test_*.py", "**/tests/**"),
    read_prior_comments: bool = True,
    prior_comment_max_chars: int = 6000,
    rereview_on_mention: bool = True,
    # Referenced-context fetch defaults OFF in tests so existing multi-pass
    # call-count assertions stay stable; production defaults ON (see config.py).
    fetch_referenced_context: bool = False,
    referenced_max_symbols: int = 6,
    referenced_context_max_chars: int = 6000,
    referenced_search_limit: int = 5,
    advisor_provider: str = "deepseek",
    agentic_scoring: bool = False,
    local_checkout: bool = False,
    # Convention fetch defaults OFF in tests (like fetch_referenced_context) so
    # existing call-count assertions stay stable; production defaults ON.
    fetch_conventions: bool = False,
    conventions_max_chars: int = 6000,
    git_path: str = "git",
    repos_dir: str = "/tmp/ghcr-test-repos",
) -> Config:
    return Config(
        github=GithubConfig(gh_path="/usr/bin/true", token="t", bot_login=bot_login, request_timeout_seconds=60, git_path=git_path),
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
            chunk_reviews=chunk_reviews,
            hard_max_diff_bytes=hard_max_diff_bytes,
            max_review_chunks=max_review_chunks,
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
            fetch_referenced_context=fetch_referenced_context,
            referenced_max_symbols=referenced_max_symbols,
            referenced_context_max_chars=referenced_context_max_chars,
            referenced_search_limit=referenced_search_limit,
            advisor_provider=advisor_provider,
            agentic_scoring=agentic_scoring,
            local_checkout=local_checkout,
            fetch_conventions=fetch_conventions,
            conventions_max_chars=conventions_max_chars,
        ),
        db_path=db_path,
        log_level="INFO",
        repos_dir=repos_dir,
    )


def make_pr(**kw) -> PullRequest:
    base = dict(repo="owner/repo", number=1, head_sha="a" * 40, title="T", author="alice", is_draft=False)
    base.update(kw)
    return PullRequest(**base)


class FakeGhClient:
    def __init__(self, *, diff="", who="reviewbot", prs=None, post_raises=False,
                 issue_comments=None, review_comments=None, comments_raise=False,
                 search_results=None, file_contents=None, code_raises=False):
        self.diff = diff
        self.who = who
        self.prs = prs or []
        self.post_raises = post_raises
        self.issue_comments = issue_comments or []
        self.review_comments = review_comments or []
        self.comments_raise = comments_raise
        # Referenced-context fetch doubles. search_results/file_contents key on the
        # query/path, with "*" as a catch-all default; code_raises simulates GhError.
        self.search_results = search_results or {}
        self.file_contents = file_contents or {}
        self.code_raises = code_raises
        self.posted: list[tuple[str, int, str]] = []
        self.diff_calls = 0
        self.comment_calls = 0
        self.search_calls = 0
        self.file_calls = 0

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

    def search_code(self, repo, query, limit=5):
        self.search_calls += 1
        if self.code_raises:
            raise GhError(["search", "code"], 1, "boom")
        if query in self.search_results:
            return list(self.search_results[query])
        return list(self.search_results.get("*", []))

    def get_file_content(self, repo, path, ref):
        self.file_calls += 1
        if self.code_raises:
            raise GhError(["api", "contents"], 1, "boom")
        if path in self.file_contents:
            return self.file_contents[path]
        if "*" in self.file_contents:
            return self.file_contents["*"]
        raise GhError(["api", "contents"], 1, "404 not found")

    def post_comment(self, repo, number, body):
        if self.post_raises:
            raise GhError(["pr", "comment"], 1, "boom")
        self.posted.append((repo, number, body))
        return f"https://github.com/{repo}/pull/{number}#issuecomment-1"


class FakeGitRepoCache:
    """Mirror of GitRepoCache for orchestrator tests. ``files`` keys on path,
    ``grep_hits`` on symbol, both with a ``"*"`` catch-all. ``ensure_ok=False``
    simulates an unavailable checkout; ``raises=True`` makes show/grep raise
    GitError (mid-resolution failure → gh fallback)."""

    def __init__(self, *, files=None, grep_hits=None, ensure_ok=True, raises=False,
                 worktree_dir="/tmp/ghcr-test-wt"):
        self.files = files or {}
        self.grep_hits = grep_hits or {}
        self.ensure_ok = ensure_ok
        self.raises = raises
        # worktree_dir=None simulates `git worktree add` failing (agentic degrade path).
        self.worktree_dir = worktree_dir
        self.ensure_calls = 0
        self.show_calls = 0
        self.grep_calls = 0
        self.worktree_calls = 0
        self.remove_calls = 0

    def ensure(self, repo, pr_number, head_sha):
        self.ensure_calls += 1
        return self.ensure_ok

    def worktree(self, repo, sha):
        self.worktree_calls += 1
        return self.worktree_dir

    def remove_worktree(self, repo, path):
        self.remove_calls += 1

    def show(self, repo, sha, path):
        self.show_calls += 1
        if self.raises:
            raise GitError(["show"], 1, "boom")
        if path in self.files:
            return self.files[path]
        if "*" in self.files:
            return self.files["*"]
        raise GitError(["show"], 128, f"path {path} does not exist")

    def grep_paths(self, repo, symbol, sha, limit=5):
        self.grep_calls += 1
        if self.raises:
            raise GitError(["grep"], 1, "boom")
        hits = self.grep_hits.get(symbol, self.grep_hits.get("*", []))
        return list(hits)[:limit]


class FakeAgenticVerifier:
    """Agentic scoring double — exposes ``verify(system, user, *, cwd)`` with the same
    system-prompt-substring routing as the other fakes, plus a ``prices`` attr (0/0) and
    a ``model`` tag. Records the ``cwd`` of every call so tests can assert the worktree
    was passed through. Lock-guarded (scoring calls it from the thread pool)."""

    def __init__(self, *, content='{"confidence": 90, "reason": "verified in checkout"}',
                 usage=None, raises=False, responses=None, model: str = "glm-4.7"):
        self.content = content
        self.usage = usage or Usage(prompt_tokens=300, completion_tokens=40, total_tokens=340)
        self.raises = raises
        self.responses = responses or {}
        self.prices = Prices(0.0, 0.0)
        self.model = model
        self._lock = threading.Lock()
        self.calls = 0
        self.systems: list[str] = []
        self.users: list[str] = []
        self.cwds: list[str] = []

    def verify(self, system_prompt, user_prompt, *, cwd):
        with self._lock:
            self.calls += 1
            self.systems.append(system_prompt)
            self.users.append(user_prompt)
            self.cwds.append(cwd)
        if self.raises:
            from ghcr.claude_cli import ClaudeCliError

            raise ClaudeCliError("verifier down")
        return ReviewResult(content=self._route(system_prompt, user_prompt), usage=self.usage, model=self.model)

    def _route(self, system_prompt, user_prompt):
        for key, val in self.responses.items():
            if key in system_prompt:
                return val(system_prompt, user_prompt) if callable(val) else val
        return self.content

    def calls_matching(self, substr: str) -> int:
        with self._lock:
            return sum(1 for s in self.systems if substr in s)


class FakeDeepSeekClient:
    """Routes a canned response by matching a substring of the system prompt.

    ``responses`` maps a substring (e.g. "## LENS: security" or "## PASS: scoring")
    to either a string or a ``callable(system, user) -> str`` (so scoring can vary
    by the finding embedded in the user prompt). Unmatched calls fall back to
    ``content`` — keeping single-pass tests unchanged. The call counter and seen
    log are lock-guarded because the multi-pass pipeline calls from a thread pool.
    """

    def __init__(self, *, content="## Summary\nlooks fine", usage=None, raises=False, responses=None,
                 model: str = "deepseek-v4-pro"):
        self.content = content
        self.usage = usage or Usage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500)
        self.raises = raises
        self.responses = responses or {}
        self.model = model
        self._lock = threading.Lock()
        self.calls = 0
        self.systems: list[str] = []
        self.users: list[str] = []
        self.thinkings: list[str | None] = []  # per-call thinking override (None = client default)

    def review(self, system_prompt, user_prompt, *, thinking=None):
        with self._lock:
            self.calls += 1
            self.systems.append(system_prompt)
            self.users.append(user_prompt)
            self.thinkings.append(thinking)
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

    def thinking_for(self, sys_substr: str) -> list[str | None]:
        """Per-call thinking overrides paired with matching system prompts."""
        with self._lock:
            return [t for s, t in zip(self.systems, self.thinkings) if sys_substr in s]


class FakeClaudeCliClient:
    """Advisor double — same system-prompt-substring routing as FakeDeepSeekClient,
    plus a ``prices`` attr (0/0) so the cost split bills it at $0. Lock-guarded
    because scoring calls it from the thread pool."""

    def __init__(self, *, content="## Summary\nadvisor ok", usage=None, raises=False, responses=None,
                 model: str = "opus"):
        self.content = content
        self.usage = usage or Usage(prompt_tokens=200, completion_tokens=100, total_tokens=300)
        self.raises = raises
        self.responses = responses or {}
        self.prices = Prices(0.0, 0.0)
        self.model = model
        self._lock = threading.Lock()
        self.calls = 0
        self.systems: list[str] = []
        self.users: list[str] = []

    def review(self, system_prompt, user_prompt, *, thinking=None):
        with self._lock:
            self.calls += 1
            self.systems.append(system_prompt)
            self.users.append(user_prompt)
        if self.raises:
            from ghcr.claude_cli import ClaudeCliError

            raise ClaudeCliError("claude down")
        return ReviewResult(content=self._route(system_prompt, user_prompt), usage=self.usage, model="opus")

    def _route(self, system_prompt, user_prompt):
        for key, val in self.responses.items():
            if key in system_prompt:
                return val(system_prompt, user_prompt) if callable(val) else val
        return self.content

    def calls_matching(self, substr: str) -> int:
        with self._lock:
            return sum(1 for s in self.systems if substr in s)
