"""The daemon poll loop: per-cycle control flow with error isolation.

A failure on one repo or one PR is logged and skipped; it never aborts the
cycle. Shutdown is signal-driven via a threading.Event so a SIGINT/SIGTERM wakes
the loop out of its inter-cycle wait immediately.
"""

from __future__ import annotations

import logging
import signal
import threading

import yaml

from .config import Config, ConfigError, load_config, merge_reloadable
from .events import ConfigReloaded, CycleStarted, PrOutcome, RepoDone, RepoListed
from .github import GhError
from .models import ACTION_ERROR, ACTION_REVIEW

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
    def __init__(self, orchestrator, gh, config: Config, bus=None, config_path: str | None = None):
        self.orch = orchestrator
        self.gh = gh
        self.cfg = config
        self.bus = bus
        self._config_path = config_path
        self._stop = threading.Event()
        self._wake = threading.Event()  # breaks the inter-cycle sleep (stop or reload)
        self._cfg_lock = threading.Lock()
        self._pending: Config | None = None

    def install_signals(self) -> None:
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, *_args) -> None:
        log.info("shutdown signal received; finishing current item then exiting")
        self.request_stop()

    def request_stop(self) -> None:
        """Ask the loop to finish the current item and exit. Thread-safe.

        Used by the TUI (running in the main thread) to stop the loop thread,
        where ``signal.signal`` cannot be installed.
        """
        self._stop.set()
        self._wake.set()

    # -- live config reload ----------------------------------------------
    def set_pending_config(self, cfg: Config) -> None:
        """Stage a new config to swap in at the next cycle boundary. Thread-safe."""
        with self._cfg_lock:
            self._pending = cfg
        self._wake.set()  # cut the sleep short so the new repos are picked up now

    def _apply_pending_config(self) -> None:
        """Swap a staged config into the loop + orchestrator. Called between cycles."""
        with self._cfg_lock:
            pending, self._pending = self._pending, None
        if pending is None:
            return
        self.cfg = pending
        self.orch.cfg = pending
        log.info("config reloaded: %d repo(s), interval %ds", len(pending.repos), pending.poll_interval_seconds)
        if self.bus:
            self.bus.publish(ConfigReloaded(
                repos=tuple(pending.repos),
                interval_s=pending.poll_interval_seconds,
                budget=pending.budgets.daily_usd_budget,
            ))

    def _on_config_file_changed(self) -> None:
        """Watcher callback: reload from disk, merge safe fields, stage it.

        Never crashes the loop — a bad edit is logged and the running config is
        kept until the next valid save.
        """
        if not self._config_path:
            return
        try:
            new = load_config(self._config_path, resolve_secrets=False)
        except (ConfigError, yaml.YAMLError, OSError) as e:
            log.warning("config reload skipped (invalid): %s", e)
            return
        merged, restart_only = merge_reloadable(self.cfg, new)
        if restart_only:
            log.warning("config changed in %s — restart required to apply those", ", ".join(restart_only))
        self.set_pending_config(merged)

    def run_once(self) -> None:
        self._apply_pending_config()
        for repo in self.cfg.repos:
            if self._stop.is_set():
                return
            try:
                prs = self.gh.list_open_prs(repo)
            except GhError as e:
                log.error("list failed repo=%s: %s", repo, e)
                if self.bus:  # still mark the repo polled so its freshness updates
                    self.bus.publish(RepoDone(repo=repo))
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
                            title=pr.title, head_sha=pr.head_sha,
                        ))
                    # Second trigger: a new @mention summons a fresh review even on an
                    # unchanged head SHA. Skip firing if we just reviewed this cycle.
                    re_out = self.orch.rereview_if_mentioned(
                        pr, just_reviewed=(outcome.action == ACTION_REVIEW)
                    )
                    if re_out is not None:
                        log.info("repo=%s pr=%d action=rereview cost=$%.4f", repo, pr.number, re_out.cost_usd)
                        if self.bus:
                            self.bus.publish(PrOutcome(
                                repo=repo, pr_number=pr.number, action=re_out.action,
                                cost_usd=re_out.cost_usd, comment_url=re_out.comment_url,
                                title=pr.title, head_sha=pr.head_sha,
                            ))
                except Exception as e:  # isolate: one PR must not kill the loop
                    log.exception("review crashed repo=%s pr=%d", repo, pr.number)
                    try:
                        self.orch.store.record(
                            repo, pr.number, pr.head_sha, ACTION_ERROR, error=str(e)
                        )
                    except Exception:
                        pass
            if self.bus:  # end of this repo's poll (incl. skip_seen PRs)
                self.bus.publish(RepoDone(repo=repo))

    def run_forever(self, install_signals: bool = True) -> None:
        if install_signals:
            self.install_signals()
        log.info("polling %d repo(s) every %ds", len(self.cfg.repos), self.cfg.poll_interval_seconds)
        if self.bus:
            self.bus.publish(CycleStarted(
                repo_count=len(self.cfg.repos), interval_s=self.cfg.poll_interval_seconds,
            ))
        watcher = self._start_watcher()
        try:
            while not self._stop.is_set():
                self._wake.clear()  # cleared before the cycle so a reload during it survives
                self.run_once()
                if self._stop.is_set():
                    break
                self._wake.wait(self.cfg.poll_interval_seconds)
        finally:
            if watcher is not None:
                watcher.stop()
        log.info("stopped")

    def _start_watcher(self):
        """Start watching the config file for live reload. No-op without a path."""
        if not self._config_path:
            return None
        from .watcher import ConfigWatcher

        watcher = ConfigWatcher(self._config_path, self._on_config_file_changed)
        watcher.start()
        return watcher
