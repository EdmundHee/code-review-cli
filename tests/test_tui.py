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


def _render(state, width=160) -> str:
    import io

    from rich.console import Console

    buf = io.StringIO()
    Console(width=width, file=buf, no_color=True).print(build_layout(state))
    return buf.getvalue()


def test_activity_shows_review_progress_for_active_pr(tmp_path):
    # A repo under active review (agent events flowing for one of its PRs) must
    # show live progress — not a bare "polling…" that's indistinguishable from a
    # hang while a slow multi-pass review grinds for minutes.
    from ghcr.tui import _activity

    state, _ = _state(tmp_path)
    state.apply(RepoListed(repo="owner/repo", open_prs=2))
    state.apply(AgentEvent(repo="owner/repo", pr_number=50, agent="lens:correctness", status="done"))
    state.apply(AgentEvent(repo="owner/repo", pr_number=50, agent="lens:security", status="running"))
    assert _activity(state, "owner/repo") == "reviewing #50 · 1/2"


def test_activity_active_without_agents_is_polling(tmp_path):
    # Active but pre-lens (diff fetch / planner / skip-only repo): no agents yet.
    from ghcr.tui import _activity

    state, _ = _state(tmp_path)
    state.apply(RepoListed(repo="owner/repo", open_prs=2))
    assert _activity(state, "owner/repo") == "polling…"


def test_activity_ignores_agents_from_a_different_repo(tmp_path):
    # active_repo just advanced; agents_pr still holds the PREVIOUS repo's PR
    # (it's reset only when the next PR's first agent event arrives). The new
    # active repo must not borrow the prior repo's progress.
    from ghcr.tui import _activity

    state, _ = _state(tmp_path)
    state.apply(AgentEvent(repo="owner/prev", pr_number=9, agent="lens:security", status="running"))
    state.apply(RepoListed(repo="owner/repo", open_prs=1))
    assert state.agents_pr == ("owner/prev", 9)
    assert _activity(state, "owner/repo") == "polling…"


def test_activity_idle_repo_shows_freshness_not_progress(tmp_path):
    # A repo that finished its poll (RepoDone) is no longer active → it shows
    # time-since-poll, never "reviewing".
    from ghcr.tui import _activity

    state, _ = _state(tmp_path)
    state.apply(RepoListed(repo="owner/repo", open_prs=1))
    state.apply(AgentEvent(repo="owner/repo", pr_number=50, agent="lens:security", status="done"))
    state.apply(RepoDone(repo="owner/repo"))
    assert "reviewing" not in _activity(state, "owner/repo")
    assert _activity(state, "owner/repo").endswith("ago")


def test_monitoring_renders_review_progress(tmp_path):
    state, _ = _state(tmp_path)
    state.apply(RepoListed(repo="owner/repo", open_prs=2))
    state.apply(AgentEvent(repo="owner/repo", pr_number=50, agent="lens:correctness", status="running"))
    assert "reviewing #50" in _render(state)


# -- per-provider token tracking tests ----------------------------------------

from ghcr.models import ProviderTokens


def _pstate(**kw):
    return DashboardState(make_config(db_path=":memory:", **kw))


def test_seed_sets_24h_provider_tokens():
    st = _pstate(advisor_provider="claude")
    st.seed([], spent_24h=0.0, tokens_24h=ProviderTokens(100, 40, 10, 5))
    assert st.tok24_wp == 100 and st.tok24_wc == 40
    assert st.tok24_ap == 10 and st.tok24_ac == 5


def test_deepseekdone_accumulates_provider_tokens():
    st = _pstate()
    st.seed([], spent_24h=0.0, tokens_24h=ProviderTokens(0, 0, 0, 0))
    st.apply(DeepSeekDone(repo="o/r", pr_number=1, prompt_tokens=50, completion_tokens=20,
                          latency_s=1.0, snippet="x", advisor_prompt_tokens=8, advisor_completion_tokens=3))
    assert st.tok24_wp == 50 and st.tok24_wc == 20
    assert st.tok24_ap == 8 and st.tok24_ac == 3
    assert st.sess_wp == 50 and st.sess_ap == 8
    assert st.sess_wc == 20 and st.sess_ac == 3


def test_advisor_model_off_path_equals_worker():
    st = _pstate()  # advisor_provider defaults to deepseek
    assert st.advisor_model == st.model


def test_seed_without_tokens_arg_is_safe():
    st = _pstate()
    st.seed([], spent_24h=1.0)  # tokens_24h omitted → defaults, no crash
    assert st.tok24_wp == 0


def test_header_shows_both_models_and_opus_tokens_when_hybrid():
    from ghcr.tui import _header
    st = _pstate()
    st.advisor_model = "opus"          # force a distinct advisor (make_config can't build cfg.claude)
    st.seed([], spent_24h=0.0, tokens_24h=ProviderTokens(1200, 400, 180, 95))
    panel = _header(st)
    text = panel.renderable.plain if hasattr(panel.renderable, "plain") else str(panel.renderable)
    assert "opus" in text and st.model in text   # both models shown
    assert "ds " in text                          # per-provider token line present
