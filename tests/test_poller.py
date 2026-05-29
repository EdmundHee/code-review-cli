import dataclasses

import pytest

from ghcr.events import ConfigReloaded, EventBus
from ghcr.poller import PollLoop, baseline_if_first_run, preflight
from ghcr.state import StateStore
from tests.fakes import FakeGhClient, make_config, make_pr


class _Orch:
    """Minimal orchestrator stub: the reload path only touches ``.cfg``."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.store = None


def test_preflight_ok_case_insensitive(tmp_path):
    cfg = make_config(db_path=str(tmp_path / "db"), bot_login="ReviewBot")
    gh = FakeGhClient(who="reviewbot")
    preflight(gh, cfg)  # no raise


def test_preflight_rejects_wrong_identity(tmp_path):
    cfg = make_config(db_path=str(tmp_path / "db"), bot_login="reviewbot")
    gh = FakeGhClient(who="EdmundHee")
    with pytest.raises(RuntimeError):
        preflight(gh, cfg)


def test_baseline_marks_existing_prs_seen(tmp_path):
    cfg = make_config(db_path=str(tmp_path / "ghcr.db"), review_backlog_on_start=False)
    store = StateStore(cfg.db_path)
    gh = FakeGhClient(prs=[make_pr(number=1, head_sha="a" * 40), make_pr(number=2, head_sha="b" * 40)])
    n = baseline_if_first_run(gh, store, cfg)
    assert n == 2
    assert store.already_reviewed("owner/repo", 1, "a" * 40)
    assert store.already_reviewed("owner/repo", 2, "b" * 40)


def test_baseline_noop_when_db_not_empty(tmp_path):
    cfg = make_config(db_path=str(tmp_path / "ghcr.db"))
    store = StateStore(cfg.db_path)
    store.record("owner/repo", 9, "c" * 40, "reviewed", model="m")
    gh = FakeGhClient(prs=[make_pr(number=1, head_sha="a" * 40)])
    assert baseline_if_first_run(gh, store, cfg) == 0
    assert not store.already_reviewed("owner/repo", 1, "a" * 40)


def test_baseline_noop_when_backlog_opted_in(tmp_path):
    cfg = make_config(db_path=str(tmp_path / "ghcr.db"), review_backlog_on_start=True)
    store = StateStore(cfg.db_path)
    gh = FakeGhClient(prs=[make_pr(number=1, head_sha="a" * 40)])
    assert baseline_if_first_run(gh, store, cfg) == 0


# -- live config reload ------------------------------------------------------

def test_apply_pending_swaps_cfg_on_loop_and_orch(tmp_path):
    old = make_config(db_path=str(tmp_path / "db"))
    orch = _Orch(old)
    loop = PollLoop(orch, FakeGhClient(), old)
    new = dataclasses.replace(old, repos=("owner/added",))
    loop.set_pending_config(new)
    loop._apply_pending_config()
    assert loop.cfg.repos == ("owner/added",)
    assert orch.cfg.repos == ("owner/added",)


def test_set_pending_wakes_sleep(tmp_path):
    old = make_config(db_path=str(tmp_path / "db"))
    loop = PollLoop(_Orch(old), FakeGhClient(), old)
    assert not loop._wake.is_set()
    loop.set_pending_config(dataclasses.replace(old, repos=("owner/x",)))
    assert loop._wake.is_set()


def test_apply_with_no_pending_is_noop(tmp_path):
    old = make_config(db_path=str(tmp_path / "db"))
    loop = PollLoop(_Orch(old), FakeGhClient(), old)
    loop._apply_pending_config()
    assert loop.cfg is old


def test_apply_pending_publishes_reloaded_event(tmp_path):
    old = make_config(db_path=str(tmp_path / "db"))
    orch = _Orch(old)
    bus = EventBus()
    seen: list = []
    bus.subscribe(seen.append)
    loop = PollLoop(orch, FakeGhClient(), old, bus=bus)
    new = dataclasses.replace(
        old,
        repos=("owner/a", "owner/b"),
        poll_interval_seconds=300,
        budgets=dataclasses.replace(old.budgets, daily_usd_budget=9.0),
    )
    loop.set_pending_config(new)
    loop._apply_pending_config()
    evts = [e for e in seen if isinstance(e, ConfigReloaded)]
    assert len(evts) == 1
    assert evts[0].repos == ("owner/a", "owner/b")
    assert evts[0].interval_s == 300
    assert evts[0].budget == 9.0


def test_reload_ignores_invalid_yaml(tmp_path):
    p = tmp_path / "config.yaml"
    cfg = make_config(db_path=str(tmp_path / "db"))
    loop = PollLoop(_Orch(cfg), FakeGhClient(), cfg, config_path=str(p))
    p.write_text("this: : not valid yaml ::")
    loop._on_config_file_changed()  # must not raise
    assert loop._pending is None  # nothing staged; running config kept


def test_run_once_applies_pending_before_iterating(tmp_path):
    old = make_config(db_path=str(tmp_path / "db"))
    listed: list[str] = []

    class GH(FakeGhClient):
        def list_open_prs(self, repo, limit=100):
            listed.append(repo)
            return []

    loop = PollLoop(_Orch(old), GH(), old)
    loop.set_pending_config(dataclasses.replace(old, repos=("owner/new1", "owner/new2")))
    loop.run_once()
    assert listed == ["owner/new1", "owner/new2"]
