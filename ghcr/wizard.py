"""Interactive ``ghcr init`` wizard: build/edit config.yaml without hand-editing.

Secrets never go in the YAML — the wizard records the *names* of the env vars
that hold them (matching the loader's model) and reminds the user to export them.
Writes atomically (temp + rename) so a running daemon's watcher sees one event.
"""

from __future__ import annotations

import os
import shutil

import yaml
from rich.console import Console
from rich.prompt import Confirm, FloatPrompt, IntPrompt, Prompt

from .config import _DEFAULT_SKIP_GLOBS, _REPO_RE, ConfigError, load_config

_DEFAULT_IGNORE = ["dependabot[bot]", "renovate[bot]", "github-actions[bot]"]


class _Asker:
    """Thin wrapper over rich prompts. Reads from stdin (tests monkeypatch it)."""

    def __init__(self, console: Console):
        self.console = console

    def text(self, prompt, default=None):
        return Prompt.ask(prompt, default=default, console=self.console)

    def confirm(self, prompt, default=True):
        return Confirm.ask(prompt, default=default, console=self.console)

    def integer(self, prompt, default=None):
        return IntPrompt.ask(prompt, default=default, console=self.console)

    def number(self, prompt, default=None):
        return FloatPrompt.ask(prompt, default=default, console=self.console)

    def choice(self, prompt, choices, default=None):
        return Prompt.ask(prompt, choices=choices, default=default, console=self.console)


def _g(raw: dict, *keys, default=None):
    """Nested ``raw['a']['b']`` lookup that tolerates missing/None sections."""
    cur = raw
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return default if cur is None else cur


def _load_existing(full: str) -> dict:
    try:
        with open(full) as f:
            raw = yaml.safe_load(f) or {}
        return raw if isinstance(raw, dict) else {}
    except (FileNotFoundError, yaml.YAMLError):
        return {}


def _ask_required(ask: _Asker, prompt: str, default=None) -> str:
    while True:
        val = (ask.text(prompt, default=default) or "").strip()
        if val:
            return val


def _ask_repos(ask: _Asker, out: Console, current) -> list[str]:
    repos: list[str] = []
    if current:
        out.print(f"[dim]current repos: {', '.join(current)} — re-enter the ones to keep[/dim]")
    while True:
        entry = (ask.text("add repo owner/repo (blank to finish)", default="") or "").strip()
        if not entry:
            if repos:
                return repos
            out.print("[red]at least one repo is required[/red]")
            continue
        if not _REPO_RE.match(entry):
            out.print(f"[red]invalid: {entry!r} (expected owner/repo)[/red]")
            continue
        if entry not in repos:
            repos.append(entry)


def _ask_csv(ask: _Asker, prompt: str, default: list[str]) -> list[str]:
    raw = ask.text(prompt, default=", ".join(default)) or ""
    return [s.strip() for s in raw.split(",") if s.strip()]


def run_wizard(path: str, env=None, *, console: Console | None = None) -> int:
    env = os.environ if env is None else env
    out = console or Console()
    ask = _Asker(out)
    full = os.path.expanduser(path)
    cur = _load_existing(full)

    out.print("[bold cyan]ghcr config wizard[/bold cyan] — press Enter to accept the [default].\n")

    # -- github -----------------------------------------------------------
    out.print("[bold]GitHub[/bold]")
    gh_default = _g(cur, "github", "gh_path") or shutil.which("gh") or "/opt/homebrew/bin/gh"
    gh_path = ask.text("gh binary path", default=gh_default)
    token_env = ask.text("env var holding the bot GH token", default=_g(cur, "github", "token_env", default="GH_TOKEN"))
    bot_login = _ask_required(ask, "bot GitHub login (bot_login)", default=_g(cur, "github", "bot_login"))
    gh_timeout = ask.integer("github request timeout (s)", default=int(_g(cur, "github", "request_timeout_seconds", default=60)))

    # -- deepseek ---------------------------------------------------------
    out.print("\n[bold]DeepSeek[/bold]")
    api_key_env = ask.text("env var holding the DeepSeek API key", default=_g(cur, "deepseek", "api_key_env", default="DEEPSEEK_API_KEY"))
    base_url = ask.text("DeepSeek base url", default=_g(cur, "deepseek", "base_url", default="https://api.deepseek.com"))
    model = ask.text("model", default=_g(cur, "deepseek", "model", default="deepseek-v4-pro"))
    thinking = ask.choice("thinking", ["enabled", "disabled"], default=_g(cur, "deepseek", "thinking", default="enabled"))
    reasoning = ask.choice("reasoning_effort", ["high", "max"], default=_g(cur, "deepseek", "reasoning_effort", default="high"))
    ds_timeout = ask.integer("deepseek request timeout (s)", default=int(_g(cur, "deepseek", "request_timeout_seconds", default=600)))
    in_price = ask.number("price per 1M input tokens (USD)", default=float(_g(cur, "deepseek", "prices", "input_per_1m", default=0.28)))
    out_price = ask.number("price per 1M output tokens (USD)", default=float(_g(cur, "deepseek", "prices", "output_per_1m", default=3.48)))

    # -- repos ------------------------------------------------------------
    out.print("\n[bold]Repos to watch[/bold]")
    repos = _ask_repos(ask, out, _g(cur, "repos", default=[]))

    # -- poll / policy / diff / budgets ----------------------------------
    out.print("\n[bold]Polling & review policy[/bold]")
    interval = ask.integer("poll interval (s)", default=int(_g(cur, "poll", "interval_seconds", default=180)))
    skip_drafts = ask.confirm("skip draft PRs?", default=bool(_g(cur, "review_policy", "skip_drafts", default=True)))
    backlog = ask.confirm("review existing open PRs on first run?", default=bool(_g(cur, "review_policy", "review_backlog_on_start", default=False)))
    ignore = _ask_csv(ask, "ignore authors (comma-separated)", _g(cur, "review_policy", "ignore_authors", default=_DEFAULT_IGNORE))

    out.print("\n[bold]Diff & budget limits[/bold]")
    max_diff = ask.integer("max raw diff bytes", default=int(_g(cur, "diff", "max_diff_bytes", default=400_000)))
    token_cap = ask.integer("per-run input token cap", default=int(_g(cur, "diff", "per_run_input_token_cap", default=250_000)))
    oversized = ask.choice("oversized behavior", ["notice", "skip"], default=_g(cur, "diff", "oversized_behavior", default="notice"))
    budget = ask.number("daily USD budget", default=float(_g(cur, "budgets", "daily_usd_budget", default=5.0)))
    budget_behavior = ask.choice("budget exceeded behavior", ["notice", "skip"], default=_g(cur, "budgets", "budget_exceeded_behavior", default="notice"))

    # -- storage / logging -----------------------------------------------
    db_path = ask.text("state db path", default=_g(cur, "storage", "db_path", default="~/.local/state/ghcr/ghcr.db"))
    log_level = ask.choice("log level", ["DEBUG", "INFO", "WARNING", "ERROR"], default=str(_g(cur, "logging", "level", default="INFO")).upper())

    skip_globs = list(_g(cur, "diff", "skip_globs", default=list(_DEFAULT_SKIP_GLOBS)))

    data = {
        "github": {
            "gh_path": gh_path,
            "token_env": token_env,
            "bot_login": bot_login,
            "request_timeout_seconds": gh_timeout,
        },
        "deepseek": {
            "api_key_env": api_key_env,
            "base_url": base_url,
            "model": model,
            "thinking": thinking,
            "reasoning_effort": reasoning,
            "request_timeout_seconds": ds_timeout,
            "prices": {"input_per_1m": in_price, "output_per_1m": out_price},
        },
        "repos": repos,
        "poll": {"interval_seconds": interval},
        "review_policy": {
            "skip_drafts": skip_drafts,
            "review_backlog_on_start": backlog,
            "ignore_authors": ignore,
        },
        "diff": {
            "max_diff_bytes": max_diff,
            "per_run_input_token_cap": token_cap,
            "oversized_behavior": oversized,
            "skip_globs": skip_globs,
        },
        "budgets": {"daily_usd_budget": budget, "budget_exceeded_behavior": budget_behavior},
        "storage": {"db_path": db_path},
        "logging": {"level": log_level},
    }

    _write_config(full, data)

    try:
        cfg = load_config(full, env=env, resolve_secrets=False)
    except ConfigError as e:
        out.print(f"[red]written config failed validation: {e}[/red]")
        return 1

    out.print(f"\n[green]✓ wrote {full}[/green] · {len(cfg.repos)} repo(s) · model={cfg.deepseek.model}")
    out.print(
        f"[yellow]secrets are env-based:[/yellow] export [bold]{token_env}[/bold] (bot PAT) and "
        f"[bold]{api_key_env}[/bold] (DeepSeek key) before running."
    )
    out.print("next: [bold]ghcr check-config[/bold] then [bold]ghcr tui[/bold]")
    return 0


def _write_config(full: str, data: dict) -> None:
    if os.path.exists(full):
        shutil.copy2(full, full + ".bak")
    tmp = full + ".tmp"
    with open(tmp, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)
    os.replace(tmp, full)  # atomic: the watcher sees one clean event
