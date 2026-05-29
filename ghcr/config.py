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


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class GithubConfig:
    gh_path: str
    token: str
    bot_login: str
    request_timeout_seconds: int


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
    db_path: str
    log_level: str


# Fields safe to swap into a running loop (read fresh every cycle/PR). The rest
# bind something at startup that a live swap won't touch: github/deepseek/db_path
# construct a client or store, and log_level is applied once via logging.basicConfig
# (nothing re-runs setLevel on reload), so all of them need a restart to take effect.
_RELOADABLE = ("repos", "poll_interval_seconds", "review_policy", "diff", "budgets")
_RESTART_ONLY = ("github", "deepseek", "db_path", "log_level")


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
    poll = raw.get("poll", {}) or {}
    storage = raw.get("storage", {}) or {}
    logging_cfg = raw.get("logging", {}) or {}

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
    diff_cfg = DiffConfig(
        max_diff_bytes=int(diff.get("max_diff_bytes", 400_000)),
        per_run_input_token_cap=int(diff.get("per_run_input_token_cap", 250_000)),
        skip_globs=tuple(str(g) for g in skip_globs),
        oversized_behavior=_behavior(diff.get("oversized_behavior", "notice"), "diff.oversized_behavior"),
    )
    budget_cfg = BudgetConfig(
        daily_usd_budget=float(budgets.get("daily_usd_budget", 5.0)),
        budget_exceeded_behavior=_behavior(
            budgets.get("budget_exceeded_behavior", "notice"), "budgets.budget_exceeded_behavior"
        ),
    )

    return Config(
        github=github_cfg,
        deepseek=deepseek_cfg,
        repos=tuple(repos),
        poll_interval_seconds=int(poll.get("interval_seconds", 120)),
        review_policy=review_policy,
        diff=diff_cfg,
        budgets=budget_cfg,
        db_path=os.path.expanduser(storage.get("db_path", "~/.local/state/ghcr/ghcr.db")),
        log_level=str(logging_cfg.get("level", "INFO")).upper(),
    )
