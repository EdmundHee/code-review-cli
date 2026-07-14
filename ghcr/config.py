"""Load + validate the YAML config into typed dataclasses.

Secrets never live in the YAML — the file names an env var (``token_env`` /
``api_key_env``) and we resolve it at load time, failing fast if absent.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace

import yaml

from .cost import Prices
from .prompts import LENS_NAMES

_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

_DEFAULT_SKIP_GLOBS = [
    "**/*.lock",
    "**/package-lock.json",
    "**/pnpm-lock.yaml",
    "**/yarn.lock",
    "**/*.min.js",
    "**/*.min.css",
    "**/dist/**",
    "**/build/**",
    "**/vendor/**",
    "**/node_modules/**",
    "**/__generated__/**",
    "**/*.pb.go",
    "**/*.snap",
    "**/*.svg",
    "**/*.png",
    "**/*.jpg",
    "**/*.pdf",
]

# Path globs treated as test files by the test_coverage lens's pre-signal.
_DEFAULT_TEST_GLOBS = [
    "**/test_*.py",
    "**/*_test.py",
    "**/tests/**",
    "**/__tests__/**",
    "**/*.test.*",
    "**/*.spec.*",
    "**/*_spec.rb",
    "**/*Test.java",
    "**/*Tests.cs",
    "**/*_test.go",
]


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class GithubConfig:
    gh_path: str
    token: str
    bot_login: str
    request_timeout_seconds: int
    git_path: str = "git"  # local-clone symbol resolution (restart-only)


@dataclass(frozen=True)
class DeepSeekConfig:
    api_key: str
    base_url: str
    model: str
    thinking: str
    reasoning_effort: str
    request_timeout_seconds: int
    prices: Prices


@dataclass(frozen=True)
class ClaudeConfig:
    claude_path: str
    model: str
    request_timeout_seconds: int
    prices: Prices  # default 0/0 → subscription is flat, report $0
    # Optional Anthropic-compatible endpoint override (e.g. GLM via Z.ai). Empty →
    # the CLI's own subscription auth. Passed to the child env, never argv.
    base_url: str = ""
    api_key: str = ""


@dataclass(frozen=True)
class AdvisorConfig:
    """Generic OpenAI-compatible advisor (e.g. GLM-5.2 via a Z.ai subscription).
    Drives a second ``DeepSeekClient`` pointed at ``base_url``; ``send_thinking_extra_body``
    defaults False so the DeepSeek-specific ``thinking`` extra_body isn't sent."""
    api_key: str
    base_url: str
    model: str
    request_timeout_seconds: int
    prices: Prices  # default 0/0 → subscription is flat, report $0
    send_thinking_extra_body: bool = False


@dataclass(frozen=True)
class ReviewPolicy:
    skip_drafts: bool
    review_backlog_on_start: bool
    ignore_authors: frozenset[str]


@dataclass(frozen=True)
class DiffConfig:
    max_diff_bytes: int
    per_run_input_token_cap: int
    skip_globs: tuple[str, ...]
    oversized_behavior: str  # "notice" | "skip"
    test_globs: tuple[str, ...] = tuple(_DEFAULT_TEST_GLOBS)
    chunk_reviews: bool = True  # multi: split an over-cap filtered diff instead of skipping
    hard_max_diff_bytes: int = 5_000_000  # raw pre-filter sanity ceiling; still skip_oversized
    max_review_chunks: int = 10  # budget guard; overflow chunks listed as unreviewed


@dataclass(frozen=True)
class ReviewModeConfig:
    mode: str  # "single" | "multi"
    lenses: tuple[str, ...]
    confidence_threshold: int  # keep findings scored >= this (0-100)
    scoring_votes: int  # independent score calls per finding; median is taken
    max_parallel: int  # cap on concurrent model calls; 0 = unbounded
    read_prior_comments: bool = True  # feed the PR's existing comments back as context
    prior_comment_max_chars: int = 6000  # budget for the rendered prior-discussion block
    rereview_on_mention: bool = True  # a new comment @mentioning the bot triggers a fresh review
    fetch_referenced_context: bool = True  # multi: fetch defs of symbols the diff references
    referenced_max_symbols: int = 6  # cap on symbols resolved per review
    referenced_context_max_chars: int = 6000  # budget for the rendered referenced-defs block
    referenced_search_limit: int = 5  # code-search results scanned per unresolved symbol
    advisor_provider: str = "deepseek"  # "deepseek" | "claude" (Opus CLI) | "openai" (GLM etc.)
    agentic_scoring: bool = False  # multi: score findings via `claude -p` with Read/Grep/Glob in a PR-head worktree
    local_checkout: bool = True  # resolve symbols from a local shallow clone of the PR head; gh is the fallback
    fetch_conventions: bool = True  # feed the repo's own CLAUDE.md etc. to the consistency lens
    conventions_max_chars: int = 6000  # budget for the rendered ## CONVENTIONS block


@dataclass(frozen=True)
class BudgetConfig:
    daily_usd_budget: float
    budget_exceeded_behavior: str  # "notice" | "skip"


@dataclass(frozen=True)
class Config:
    github: GithubConfig
    deepseek: DeepSeekConfig
    repos: tuple[str, ...]
    poll_interval_seconds: int
    review_policy: ReviewPolicy
    diff: DiffConfig
    budgets: BudgetConfig
    review: ReviewModeConfig
    db_path: str
    log_level: str
    repos_dir: str = "~/.local/state/ghcr/repos"  # bare-clone cache for local symbol resolution
    claude: "ClaudeConfig | None" = None
    advisor: "AdvisorConfig | None" = None


# Fields safe to swap into a running loop (read fresh every cycle/PR). The rest
# bind something at startup that a live swap won't touch: github/deepseek/db_path
# construct a client or store, and log_level is applied once via logging.basicConfig
# (nothing re-runs setLevel on reload), so all of them need a restart to take effect.
# NOTE: review.advisor_provider is hot-reloadable as a value, but the advisor
# CLIENT is constructed at startup (cli._build) — flipping deepseek<->claude
# requires a restart to take effect. Same for review.agentic_scoring: the verifier
# client is built at startup, so the flag is a hot KILL-SWITCH (off stops using an
# already-built verifier) but enabling it from cold needs a restart.
_RELOADABLE = ("repos", "poll_interval_seconds", "review_policy", "diff", "budgets", "review")
_RESTART_ONLY = ("github", "deepseek", "db_path", "log_level", "repos_dir", "claude", "advisor")


def merge_reloadable(old: Config, new: Config) -> tuple[Config, list[str]]:
    """Return ``old`` with hot-reloadable fields taken from ``new``.

    Restart-only fields (github/deepseek/db_path) are kept from ``old``; any that
    differ in ``new`` are returned in the second element so the caller can warn a
    restart is needed to apply them.
    """
    merged = replace(old, **{f: getattr(new, f) for f in _RELOADABLE})
    changed = [f for f in _RESTART_ONLY if getattr(old, f) != getattr(new, f)]
    return merged, changed


def _require_env(env, name: str, what: str) -> str:
    val = env.get(name, "")
    if not val:
        raise ConfigError(f"env var {name!r} (for {what}) is unset or empty")
    return val


def _behavior(value, where: str) -> str:
    if value not in ("notice", "skip"):
        raise ConfigError(f"{where} must be 'notice' or 'skip', got {value!r}")
    return value


def _parse_review(review: dict) -> "ReviewModeConfig":
    mode = review.get("mode", "multi")  # accuracy-first default
    if mode not in ("single", "multi"):
        raise ConfigError(f"review.mode must be 'single' or 'multi', got {mode!r}")

    lenses = review.get("lenses") or list(LENS_NAMES)
    for name in lenses:
        if name not in LENS_NAMES:
            raise ConfigError(
                f"unknown review lens {name!r} (valid: {', '.join(LENS_NAMES)})"
            )

    threshold = max(0, min(100, int(review.get("confidence_threshold", 80))))

    votes = int(review.get("scoring_votes", 1))
    if votes < 1:
        raise ConfigError("review.scoring_votes must be >= 1")

    max_parallel = int(review.get("max_parallel", 0))
    if max_parallel < 0:
        raise ConfigError("review.max_parallel must be >= 0 (0 = unbounded)")

    advisor_provider = review.get("advisor_provider", "deepseek")
    if advisor_provider not in ("deepseek", "claude", "openai"):
        raise ConfigError(
            f"review.advisor_provider must be 'deepseek', 'claude' or 'openai', got {advisor_provider!r}"
        )

    return ReviewModeConfig(
        mode=mode,
        lenses=tuple(lenses),
        confidence_threshold=threshold,
        scoring_votes=votes,
        max_parallel=max_parallel,
        read_prior_comments=bool(review.get("read_prior_comments", True)),
        prior_comment_max_chars=max(0, int(review.get("prior_comment_max_chars", 6000))),
        rereview_on_mention=bool(review.get("rereview_on_mention", True)),
        fetch_referenced_context=bool(review.get("fetch_referenced_context", True)),
        referenced_max_symbols=max(0, int(review.get("referenced_max_symbols", 6))),
        referenced_context_max_chars=max(0, int(review.get("referenced_context_max_chars", 6000))),
        referenced_search_limit=max(1, int(review.get("referenced_search_limit", 5))),
        advisor_provider=advisor_provider,
        agentic_scoring=bool(review.get("agentic_scoring", False)),
        local_checkout=bool(review.get("local_checkout", True)),
        fetch_conventions=bool(review.get("fetch_conventions", True)),
        conventions_max_chars=max(0, int(review.get("conventions_max_chars", 6000))),
    )


def _parse_advisor(advisor_raw: dict, env, resolve_secrets: bool) -> "AdvisorConfig":
    """Generic OpenAI-compatible advisor block (used when advisor_provider == 'openai').
    Key resolved from ``api_key_env`` — never a literal secret in YAML."""
    aprices_raw = advisor_raw.get("prices", {}) or {}
    api_key = (
        _require_env(env, advisor_raw.get("api_key_env", "ADVISOR_API_KEY"), "advisor api key")
        if resolve_secrets
        else ""
    )
    return AdvisorConfig(
        api_key=api_key,
        base_url=advisor_raw.get("base_url", "https://api.z.ai/api/paas/v4"),
        model=advisor_raw.get("model", "glm-5.2"),
        request_timeout_seconds=int(advisor_raw.get("request_timeout_seconds", 600)),
        prices=Prices(
            input_per_1m=float(aprices_raw.get("input_per_1m", 0.0)),
            output_per_1m=float(aprices_raw.get("output_per_1m", 0.0)),
        ),
        send_thinking_extra_body=bool(advisor_raw.get("send_thinking_extra_body", False)),
    )


def load_config(path: str, env=None, resolve_secrets: bool = True) -> Config:
    env = os.environ if env is None else env
    full = os.path.expanduser(path)
    try:
        with open(full) as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {full}") from e
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")

    gh = raw.get("github", {}) or {}
    ds = raw.get("deepseek", {}) or {}
    rp = raw.get("review_policy", {}) or {}
    diff = raw.get("diff", {}) or {}
    budgets = raw.get("budgets", {}) or {}
    review = raw.get("review", {}) or {}
    poll = raw.get("poll", {}) or {}
    storage = raw.get("storage", {}) or {}
    logging_cfg = raw.get("logging", {}) or {}

    claude_raw = raw.get("claude", {}) or {}
    advisor_raw = raw.get("advisor", {}) or {}

    repos = raw.get("repos") or []
    if not repos:
        raise ConfigError("config 'repos' must list at least one owner/repo")
    for r in repos:
        if not isinstance(r, str) or not _REPO_RE.match(r):
            raise ConfigError(f"invalid repo entry: {r!r} (expected 'owner/repo')")

    bot_login = (gh.get("bot_login") or "").strip()
    if not bot_login:
        raise ConfigError("github.bot_login is required")

    token = _require_env(env, gh.get("token_env", "GH_TOKEN"), "github token") if resolve_secrets else ""
    api_key = (
        _require_env(env, ds.get("api_key_env", "DEEPSEEK_API_KEY"), "deepseek api key")
        if resolve_secrets
        else ""
    )

    prices_raw = ds.get("prices", {}) or {}
    prices = Prices(
        input_per_1m=float(prices_raw.get("input_per_1m", 0.28)),
        output_per_1m=float(prices_raw.get("output_per_1m", 3.48)),
    )

    github_cfg = GithubConfig(
        gh_path=gh.get("gh_path", "/opt/homebrew/bin/gh"),
        token=token,
        bot_login=bot_login,
        request_timeout_seconds=int(gh.get("request_timeout_seconds", 60)),
        git_path=gh.get("git_path", "git"),
    )
    deepseek_cfg = DeepSeekConfig(
        api_key=api_key,
        base_url=ds.get("base_url", "https://api.deepseek.com"),
        model=ds.get("model", "deepseek-v4-pro"),
        thinking=ds.get("thinking", "enabled"),
        reasoning_effort=ds.get("reasoning_effort", "high"),
        request_timeout_seconds=int(ds.get("request_timeout_seconds", 600)),
        prices=prices,
    )
    if deepseek_cfg.thinking not in ("enabled", "disabled"):
        raise ConfigError("deepseek.thinking must be 'enabled' or 'disabled'")

    ignore = {a.strip() for a in (rp.get("ignore_authors") or []) if a and a.strip()}
    review_policy = ReviewPolicy(
        skip_drafts=bool(rp.get("skip_drafts", True)),
        review_backlog_on_start=bool(rp.get("review_backlog_on_start", False)),
        ignore_authors=frozenset(ignore),
    )

    skip_globs = diff.get("skip_globs") or _DEFAULT_SKIP_GLOBS
    test_globs = diff.get("test_globs") or _DEFAULT_TEST_GLOBS
    diff_cfg = DiffConfig(
        max_diff_bytes=int(diff.get("max_diff_bytes", 400_000)),
        per_run_input_token_cap=int(diff.get("per_run_input_token_cap", 250_000)),
        skip_globs=tuple(str(g) for g in skip_globs),
        oversized_behavior=_behavior(diff.get("oversized_behavior", "notice"), "diff.oversized_behavior"),
        test_globs=tuple(str(g) for g in test_globs),
        chunk_reviews=bool(diff.get("chunk_reviews", True)),
        hard_max_diff_bytes=int(diff.get("hard_max_diff_bytes", 5_000_000)),
        max_review_chunks=int(diff.get("max_review_chunks", 10)),
    )
    if diff_cfg.hard_max_diff_bytes < diff_cfg.max_diff_bytes:
        raise ConfigError("diff.hard_max_diff_bytes must be >= diff.max_diff_bytes")
    if diff_cfg.max_review_chunks < 1:
        raise ConfigError("diff.max_review_chunks must be >= 1")
    budget_cfg = BudgetConfig(
        daily_usd_budget=float(budgets.get("daily_usd_budget", 5.0)),
        budget_exceeded_behavior=_behavior(
            budgets.get("budget_exceeded_behavior", "notice"), "budgets.budget_exceeded_behavior"
        ),
    )

    review_cfg = _parse_review(review)

    # The `claude:` block is shared by the advisor (advisor_provider == "claude") and
    # the agentic verifier (review.agentic_scoring) — build it if either is on.
    claude_cfg = None
    if review_cfg.advisor_provider == "claude" or review_cfg.agentic_scoring:
        cprices_raw = claude_raw.get("prices", {}) or {}
        claude_base_url = claude_raw.get("base_url", "")
        # A base_url override needs a resolvable token; without an override the CLI's
        # own subscription auth is used and no env var is required.
        claude_api_key = (
            _require_env(env, claude_raw.get("api_key_env", "ANTHROPIC_AUTH_TOKEN"), "claude api key")
            if (claude_base_url and resolve_secrets)
            else ""
        )
        claude_cfg = ClaudeConfig(
            claude_path=claude_raw.get("claude_path", "claude"),
            model=claude_raw.get("model", "opus"),
            request_timeout_seconds=int(claude_raw.get("request_timeout_seconds", 600)),
            prices=Prices(
                input_per_1m=float(cprices_raw.get("input_per_1m", 0.0)),
                output_per_1m=float(cprices_raw.get("output_per_1m", 0.0)),
            ),
            base_url=claude_base_url,
            api_key=claude_api_key,
        )

    advisor_cfg = None
    if review_cfg.advisor_provider == "openai":
        advisor_cfg = _parse_advisor(advisor_raw, env, resolve_secrets)

    return Config(
        github=github_cfg,
        deepseek=deepseek_cfg,
        repos=tuple(repos),
        poll_interval_seconds=int(poll.get("interval_seconds", 120)),
        review_policy=review_policy,
        diff=diff_cfg,
        budgets=budget_cfg,
        review=review_cfg,
        db_path=os.path.expanduser(storage.get("db_path", "~/.local/state/ghcr/ghcr.db")),
        log_level=str(logging_cfg.get("level", "INFO")).upper(),
        repos_dir=os.path.expanduser(storage.get("repos_dir", "~/.local/state/ghcr/repos")),
        claude=claude_cfg,
        advisor=advisor_cfg,
    )
