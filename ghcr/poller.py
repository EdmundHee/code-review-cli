"""The daemon poll loop: per-cycle control flow with error isolation.

A failure on one repo or one PR is logged and skipped; it never aborts the
cycle. Shutdown is signal-driven via a threading.Event so a SIGINT/SIGTERM wakes
the loop out of its inter-cycle wait immediately.
"""

from __future__ import annotations

import logging
import signal
import threading

from .config import Config
from .events import CycleStarted, PrOutcome, RepoListed
from .github import GhError
from .models import ACTION_ERROR

log = logging.getLogger("ghcr.poller")


def preflight(gh, cfg: Config) -> None:
    """Verify the daemon will act as the bot, not the user. Hard-fail otherwise."""
    who = gh.whoami()
    if who.lower() != cfg.github.bot_login.lower():
        raise RuntimeError(
            f"gh identity is {who!r} but config bot_login is {cfg.github.bot_login!r}. "
            f"Refusing to run so reviews are not posted as the wrong account. "
            f"Check GH_TOKEN points to the bot PAT."
        )
    log.info("preflight ok: posting as %s", who)


def baseline_if_first_run(gh, store, cfg: Config) -> int:
    """On an empty DB, mark current open-PR head SHAs as seen (no review).

    Prevents a cost bomb on first start against repos with existing open PRs,
    and cheaply re-baselines after a DB loss. No-op if backlog review is opted
    in, or if the DB already has rows.
    """
    if cfg.review_policy.review_backlog_on_start or store.has_any():
        return 0
    n = 0
    for repo in cfg.repos:
        try:
            prs = gh.list_open_prs(repo)
        except GhError as e:
            log.error("baseline list failed repo=%s: %s", repo, e)
            continue
        for pr in prs:
            store.baseline_seen(pr)
            n += 1
    log.info("baselined %d existing open PR(s) as seen", n)
    return n


class PollLoop:
    def __init__(self, orchestrator, gh, config: Config, bus=None):
        self.orch = orchestrator
        self.gh = gh
        self.cfg = config
        self.bus = bus
        self._stop = threading.Event()

    def install_signals(self) -> None:
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, *_args) -> None:
        log.info("shutdown signal received; finishing current item then exiting")
        self._stop.set()

    def request_stop(self) -> None:
        """Ask the loop to finish the current item and exit. Thread-safe.

        Used by the TUI (running in the main thread) to stop the loop thread,
        where ``signal.signal`` cannot be installed.
        """
        self._stop.set()

    def run_once(self) -> None:
        for repo in self.cfg.repos:
            if self._stop.is_set():
                return
            try:
                prs = self.gh.list_open_prs(repo)
            except GhError as e:
                log.error("list failed repo=%s: %s", repo, e)
                continue
            if self.bus:
                self.bus.publish(RepoListed(repo=repo, open_prs=len(prs)))
            for pr in prs:
                if self._stop.is_set():
                    return
                try:
                    outcome = self.orch.review_pr(pr)
                    log.info(
                        "repo=%s pr=%d action=%s cost=$%.4f",
                        repo, pr.number, outcome.action, outcome.cost_usd,
                    )
                    if self.bus:
                        self.bus.publish(PrOutcome(
                            repo=repo, pr_number=pr.number, action=outcome.action,
                            cost_usd=outcome.cost_usd, comment_url=outcome.comment_url,
                            title=pr.title,
                        ))
                except Exception as e:  # isolate: one PR must not kill the loop
                    log.exception("review crashed repo=%s pr=%d", repo, pr.number)
                    try:
                        self.orch.store.record(
                            repo, pr.number, pr.head_sha, ACTION_ERROR, error=str(e)
                        )
                    except Exception:
                        pass

    def run_forever(self, install_signals: bool = True) -> None:
        if install_signals:
            self.install_signals()
        log.info("polling %d repo(s) every %ds", len(self.cfg.repos), self.cfg.poll_interval_seconds)
        if self.bus:
            self.bus.publish(CycleStarted(
                repo_count=len(self.cfg.repos), interval_s=self.cfg.poll_interval_seconds,
            ))
        while not self._stop.is_set():
            self.run_once()
            if self._stop.is_set():
                break
            self._stop.wait(self.cfg.poll_interval_seconds)
        log.info("stopped")
