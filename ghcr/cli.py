"""CLI entrypoint: run | review-once | check-config | status."""

from __future__ import annotations

import argparse
import fcntl
import logging
import sys
from datetime import datetime, timedelta, timezone

from .config import Config, ConfigError, load_config
from .deepseek import DeepSeekClient
from .github import GhClient, GhError
from .poller import PollLoop, baseline_if_first_run, preflight
from .review import ReviewOrchestrator
from .state import StateStore

log = logging.getLogger("ghcr")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _build(cfg: Config):
    gh = GhClient(cfg.github.gh_path, cfg.github.token, cfg.github.request_timeout_seconds)
    ds = DeepSeekClient(
        api_key=cfg.deepseek.api_key,
        base_url=cfg.deepseek.base_url,
        model=cfg.deepseek.model,
        thinking=cfg.deepseek.thinking,
        reasoning_effort=cfg.deepseek.reasoning_effort,
        timeout=cfg.deepseek.request_timeout_seconds,
    )
    store = StateStore(cfg.db_path)
    orch = ReviewOrchestrator(gh, ds, store, cfg)
    return gh, ds, store, orch


def _acquire_lock(db_path: str):
    """Single-instance guard: exclusive flock on a lockfile beside the DB."""
    lock_path = db_path + ".lock"
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"another ghcr instance holds {lock_path}; exiting", file=sys.stderr)
        sys.exit(1)
    return fh  # keep handle alive for process lifetime


def cmd_run(cfg: Config) -> int:
    lock = _acquire_lock(cfg.db_path)  # noqa: F841 — held until exit
    gh, ds, store, orch = _build(cfg)
    preflight(gh, cfg)
    baseline_if_first_run(gh, store, cfg)
    PollLoop(orch, gh, cfg).run_forever()
    return 0


def cmd_review_once(cfg: Config, repo: str, pr_number: int) -> int:
    gh, ds, store, orch = _build(cfg)
    preflight(gh, cfg)
    pr = gh.get_pr(repo, pr_number)
    outcome = orch.review_pr(pr)
    print(f"{repo}#{pr_number} ({pr.head_sha[:7]}): {outcome.action} cost=${outcome.cost_usd:.4f}")
    if outcome.comment_url:
        print(outcome.comment_url)
    return 0


def cmd_check_config(cfg: Config) -> int:
    print(f"config OK · {len(cfg.repos)} repo(s) · model={cfg.deepseek.model}")
    gh = GhClient(cfg.github.gh_path, cfg.github.token, cfg.github.request_timeout_seconds)
    preflight(gh, cfg)
    print("preflight OK")
    return 0


def cmd_status(cfg: Config) -> int:
    store = StateStore(cfg.db_path)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    spent = store.usd_spent_since(cutoff)
    print(f"spend (last 24h): ${spent:.4f} / ${cfg.budgets.daily_usd_budget:.2f}")
    print("recent:")
    for row in store.recent(20):
        print(
            f"  {row['created_at']}  {row['repo']}#{row['pr_number']} "
            f"{row['head_sha'][:7]}  {row['outcome']}  ${row['cost_usd']:.4f}"
        )
    store.close()
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(prog="ghcr", description="DeepSeek PR review bot")
    p.add_argument("--config", default="config.yaml", help="path to config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the polling daemon")
    s_once = sub.add_parser("review-once", help="review a single PR and exit")
    s_once.add_argument("--repo", required=True)
    s_once.add_argument("--pr", required=True, type=int)
    sub.add_parser("check-config", help="validate config + bot identity")
    sub.add_parser("status", help="show recent reviews and 24h spend")
    args = p.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    _setup_logging(cfg.log_level)

    try:
        if args.cmd == "run":
            return cmd_run(cfg)
        if args.cmd == "review-once":
            return cmd_review_once(cfg, args.repo, args.pr)
        if args.cmd == "check-config":
            return cmd_check_config(cfg)
        if args.cmd == "status":
            return cmd_status(cfg)
    except (GhError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
