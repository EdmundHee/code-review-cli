from ghcr.events import (
    AgentEvent,
    ConfigReloaded,
    CycleStarted,
    DeepSeekDone,
    LogLine,
    PrOutcome,
    RepoDone,
    RepoListed,
)
from ghcr.tui import DashboardState, build_layout
from tests.fakes import make_config


def _state(tmp_path):
    cfg = make_config(db_path=str(tmp_path / "ghcr.db"))
    return DashboardState(cfg), cfg


def test_repo_listed_updates_open_pr_count(tmp_path):
    state, _ = _state(tmp_path)
    state.apply(RepoListed(repo="owner/repo", open_prs=3))
    assert state.repos["owner/repo"]["open_prs"] == 3


def test_repo_listed_marks_repo_active(tmp_path):
    state, _ = _state(tmp_path)
    state.apply(RepoListed(repo="owner/repo", open_prs=3))
    assert state.active_repo == "owner/repo"


def test_repo_done_clears_active_and_stamps_polled_at(tmp_path):
    state, _ = _state(tmp_path)
    state.apply(RepoListed(repo="owner/repo", open_prs=3))
    state.apply(RepoDone(repo="owner/repo"))
    assert state.active_repo is None
    assert state.repos["owner/repo"]["polled_at"] is not None


def test_repo_done_for_other_repo_keeps_current_active(tmp_path):
    state, _ = _state(tmp_path)
    state.apply(RepoListed(repo="owner/repo", open_prs=1))
    state.apply(RepoDone(repo="owner/other"))  # a different repo finishing
    assert state.active_repo == "owner/repo"  # ours is still active
    assert state.repos["owner/other"]["polled_at"] is not None


def test_cycle_started_sets_interval_and_logs(tmp_path):
    state, _ = _state(tmp_path)
    state.apply(CycleStarted(repo_count=2, interval_s=180))
    assert state.interval_s == 180
    assert any("cycle" in e for e in state.events)


def test_deepseek_done_becomes_latest(tmp_path):
    state, _ = _state(tmp_path)
    evt = DeepSeekDone(repo="owner/repo", pr_number=42, prompt_tokens=12300,
                       completion_tokens=1100, latency_s=2.1, snippet="found a bug", title="Fix")
    state.apply(evt)
    assert state.latest is evt
    assert any("deepseek" in e for e in state.events)


def test_pr_outcome_review_adds_cost_row_and_spend(tmp_path):
    state, _ = _state(tmp_path)
    state.apply(PrOutcome(repo="owner/repo", pr_number=42, action="review",
                          cost_usd=0.018, head_sha="a1b2c3d4e5f6"))
    assert state.spent_24h == 0.018
    assert state.cost_rows[-1]["pr"] == 42
    # last column shows the action AND the short commit SHA that was reviewed.
    assert state.repos["owner/repo"]["last"] == "#42 review a1b2c3d $0.0180"


def test_pr_outcome_transient_skip_no_cost_row(tmp_path):
    state, _ = _state(tmp_path)
    state.apply(PrOutcome(repo="owner/repo", pr_number=5, action="skip_draft", cost_usd=0.0))
    assert len(state.cost_rows) == 0
    assert state.spent_24h == 0.0


def test_skip_seen_does_not_clobber_recorded_review(tmp_path):
    state, _ = _state(tmp_path)
    state.apply(PrOutcome(repo="owner/repo", pr_number=2, action="review", cost_usd=0.018))
    before = len(state.events)
    # A later cycle re-emits skip_seen for the same already-reviewed PR.
    state.apply(PrOutcome(repo="owner/repo", pr_number=2, action="skip_seen", cost_usd=0.0))
    # The review line and cost survive; skip_seen adds no event noise.
    assert state.repos["owner/repo"]["last"].startswith("#2 review")
    assert state.spent_24h == 0.018
    assert len(state.cost_rows) == 1
    assert len(state.events) == before


def test_agent_events_tracked_and_reset_per_pr(tmp_path):
    state, _ = _state(tmp_path)
    state.apply(AgentEvent(repo="owner/repo", pr_number=7, agent="lens:security", status="running"))
    state.apply(AgentEvent(repo="owner/repo", pr_number=7, agent="lens:security", status="done", detail="0 found"))
    state.apply(AgentEvent(repo="owner/repo", pr_number=7, agent="score:#0", status="running"))
    assert state.agents_pr == ("owner/repo", 7)
    assert state.agents["lens:security"]["status"] == "done"
    assert state.agents["score:#0"]["status"] == "running"
    # A different PR resets the agent board.
    state.apply(AgentEvent(repo="owner/repo", pr_number=8, agent="lens:correctness", status="running"))
    assert state.agents_pr == ("owner/repo", 8)
    assert set(state.agents) == {"lens:correctness"}


def test_agent_failure_logged_to_history(tmp_path):
    state, _ = _state(tmp_path)
    state.apply(AgentEvent(repo="owner/repo", pr_number=7, agent="lens:security", status="failed", detail="api error"))
    assert any("lens:security failed" in e for e in state.events)


def test_config_reloaded_adds_new_and_prunes_removed_repos(tmp_path):
    state, _ = _state(tmp_path)  # starts with "owner/repo"
    state.apply(ConfigReloaded(repos=("owner/repo", "owner/added"), interval_s=300, budget=9.0))
    assert "owner/added" in state.repos
    assert "owner/repo" in state.repos
    assert state.interval_s == 300
    assert state.budget == 9.0
    assert any("reload" in e.lower() for e in state.events)


def test_config_reloaded_removes_dropped_repo(tmp_path):
    state, _ = _state(tmp_path)  # starts with "owner/repo"
    state.apply(ConfigReloaded(repos=("owner/added",), interval_s=120, budget=5.0))
    assert "owner/repo" not in state.repos
    assert "owner/added" in state.repos


def test_log_line_appended_to_history(tmp_path):
    state, _ = _state(tmp_path)
    state.apply(LogLine(ts="11:28:10", level="INFO", name="httpx", message="HTTP Request: POST ... 200 OK"))
    assert any("httpx" in e and "200 OK" in e for e in state.events)


def test_seed_populates_from_db_rows(tmp_path):
    state, _ = _state(tmp_path)
    rows = [
        {"repo": "owner/repo", "pr_number": 41, "outcome": "reviewed", "head_sha": "deadbeef1234",
         "cost_usd": 0.012, "created_at": "2026-05-29T11:00:00+00:00"},
    ]
    state.seed(rows, spent_24h=0.012)
    assert state.spent_24h == 0.012
    assert state.cost_rows[-1]["pr"] == 41
    assert state.repos["owner/repo"]["last"] == "#41 reviewed deadbee $0.0120"


def test_build_layout_renders_without_error(tmp_path):
    state, _ = _state(tmp_path)
    state.apply(RepoListed(repo="owner/repo", open_prs=2))
    state.apply(PrOutcome(repo="owner/repo", pr_number=42, action="review", cost_usd=0.018))
    state.apply(DeepSeekDone(repo="owner/repo", pr_number=42, prompt_tokens=100,
                             completion_tokens=10, latency_s=1.0, snippet="x", title="t"))
    state.apply(AgentEvent(repo="owner/repo", pr_number=42, agent="lens:correctness", status="running"))
    state.apply(AgentEvent(repo="owner/repo", pr_number=42, agent="lens:security", status="done", detail="1 found"))
    layout = build_layout(state)
    # Render to a string region to ensure no exceptions in the Rich tree.
    from rich.console import Console
    Console(width=120, height=40, file=open("/dev/null", "w")).print(layout)


def test_build_layout_renders_active_repo_row(tmp_path):
    # RepoListed with no following RepoDone leaves the repo active → the
    # highlighted "polling…" row path must render cleanly.
    state, _ = _state(tmp_path)
    state.apply(RepoListed(repo="owner/repo", open_prs=2))
    assert state.active_repo == "owner/repo"
    from rich.console import Console
    Console(width=120, height=40, file=open("/dev/null", "w")).print(build_layout(state))
