import pytest

from ghcr.poller import baseline_if_first_run, preflight
from ghcr.state import StateStore
from tests.fakes import FakeGhClient, make_config, make_pr


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
