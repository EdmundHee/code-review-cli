from datetime import datetime, timezone

from ghcr.deepseek import DeepSeekError
from ghcr.models import PriorComment
from ghcr.review import ReviewOrchestrator
from ghcr.state import StateStore
from tests.fakes import FakeDeepSeekClient, FakeGhClient, make_config, make_pr

FIXED = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)

SRC_DIFF = """\
diff --git a/src/app.py b/src/app.py
index 1..2 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
 def f():
-    return 1
+    return 2
"""

CORR = '[{"severity":"BLOCKER","file":"src/app.py","area":"f","issue":"returns wrong value","fix":"return 1"}]'
EMPTY = "[]"
COV_NO = '{"verdict":{"has_tests":false,"detail":"no test for f"},"findings":[]}'
COV_YES = '{"verdict":{"has_tests":true,"detail":"tests added for f"},"findings":[]}'
SCORE_HIGH = '{"confidence":95,"reason":"clear bug"}'
SCORE_LOW = '{"confidence":50,"reason":"maybe"}'
SCORE_ZERO = '{"confidence":0,"reason":"already raised in prior discussion"}'


def _responses(*, corr=CORR, sec=EMPTY, maint=EMPTY, cov=COV_NO, score=SCORE_HIGH):
    return {
        "## LENS: correctness": corr,
        "## LENS: security": sec,
        "## LENS: maintainability": maint,
        "## LENS: test_coverage": cov,
        "## PASS: scoring": score,
    }


def _orch(tmp_path, gh, ds, **cfg_kw):
    cfg = make_config(db_path=str(tmp_path / "ghcr.db"), review_mode="multi", **cfg_kw)
    store = StateStore(cfg.db_path)
    return ReviewOrchestrator(gh, ds, store, cfg, now=lambda: FIXED), store


def test_happy_path_fans_out_scores_and_posts(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF)
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    assert ds.calls == 5  # 4 lenses + 1 finding * 1 vote
    assert ds.calls_matching("## PASS: scoring") == 1
    body = gh.posted[0][2]
    assert "## Test coverage" in body and "⚠️" in body
    assert "[BLOCKER]" in body and "confidence 95" in body
    row = store.recent(1)[0]
    assert row["outcome"] == "reviewed"
    # Default (deepseek) path: advisor IS the worker, so scoring folds into the worker
    # total — prompt_tokens is the FULL review (4 lenses + 1 score).
    assert row["prompt_tokens"] == 5000 and row["completion_tokens"] == 2500
    assert store.already_reviewed("owner/repo", 1, "a" * 40)


def test_default_path_reports_no_phantom_advisor_tokens(tmp_path):
    """Default config (advisor_provider=deepseek) has no DISTINCT advisor: planner +
    scoring tokens are DeepSeek's own. The split must fold them into the worker total
    and publish ZERO advisor tokens — no phantom Opus usage when no Opus ran."""
    from ghcr.events import DeepSeekDone

    class _CaptureBus:
        def __init__(self):
            self.events = []

        def publish(self, evt):
            self.events.append(evt)

    bus = _CaptureBus()
    gh = FakeGhClient(diff=SRC_DIFF)
    ds = FakeDeepSeekClient(responses=_responses())
    cfg = make_config(db_path=str(tmp_path / "ghcr.db"), review_mode="multi")
    store = StateStore(cfg.db_path)
    orch = ReviewOrchestrator(gh, ds, store, cfg, now=lambda: FIXED, bus=bus)
    out = orch.review_pr(make_pr())
    assert out.action == "review"

    done = [e for e in bus.events if isinstance(e, DeepSeekDone)][0]
    # All 5 DeepSeek calls (4 lenses + 1 score) fold into the worker total.
    assert done.prompt_tokens == ds.usage.prompt_tokens * ds.calls
    assert done.completion_tokens == ds.usage.completion_tokens * ds.calls
    # No distinct advisor ran → zero advisor tokens.
    assert done.advisor_prompt_tokens == 0
    assert done.advisor_completion_tokens == 0


def test_below_threshold_findings_filtered_but_still_posts(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF)
    ds = FakeDeepSeekClient(responses=_responses(score=SCORE_LOW))
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    body = gh.posted[0][2]
    # The lone finding scored 50 (< threshold 80) → dropped, but the comment now
    # surfaces that instead of reading as a bare "nothing found".
    assert "No high-confidence issues" not in body
    assert "1 scored lower" in body and "≥80" in body
    assert "[BLOCKER]" not in body
    assert store.already_reviewed("owner/repo", 1, "a" * 40)


def test_tests_present_shows_check_mark(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF)
    ds = FakeDeepSeekClient(responses=_responses(corr=EMPTY, cov=COV_YES))
    orch, store = _orch(tmp_path, gh, ds)
    orch.review_pr(make_pr())
    body = gh.posted[0][2]
    assert "✅" in body and "tests added for f" in body


def test_one_lens_raises_review_completes_from_survivors(tmp_path):
    def boom(system, user):
        raise DeepSeekError("lens down")

    gh = FakeGhClient(diff=SRC_DIFF)
    ds = FakeDeepSeekClient(responses=_responses(sec=boom))
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    body = gh.posted[0][2]
    assert "## Notes" in body and "security" in body
    assert "[BLOCKER]" in body  # correctness finding still made it through


def test_one_lens_malformed_json_is_dropped(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF)
    ds = FakeDeepSeekClient(responses=_responses(maint="the model rambled and emitted no json"))
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    assert "maintainability" in gh.posted[0][2]  # noted as a failed lens


def test_multi_vote_takes_median(tmp_path):
    cycle = {"i": 0}

    def cycler(system, user):
        vals = [10, 90, 95]
        v = vals[cycle["i"] % len(vals)]
        cycle["i"] += 1
        return '{"confidence":%d,"reason":"v"}' % v

    gh = FakeGhClient(diff=SRC_DIFF)
    ds = FakeDeepSeekClient(responses=_responses(score=cycler))
    orch, store = _orch(tmp_path, gh, ds, scoring_votes=3)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    assert ds.calls_matching("## PASS: scoring") == 3
    assert "confidence 90" in gh.posted[0][2]  # median(10,90,95)


def test_all_lenses_fail_records_error_no_post(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF)
    ds = FakeDeepSeekClient(raises=True)  # every call raises
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "error"
    assert gh.posted == []
    assert not store.already_reviewed("owner/repo", 1, "a" * 40)
    assert ds.calls == 4  # 4 lenses, scoring never runs


def test_budget_gate_runs_no_model_calls(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF)
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds, daily_usd_budget=5.0)
    store.record("owner/repo", 99, "z" * 40, "reviewed", cost_usd=10.0, created_at=FIXED.isoformat(), model="m")
    out = orch.review_pr(make_pr())
    assert out.action == "skip_budget"
    assert ds.calls == 0
    assert len(gh.posted) == 1  # budget notice


def test_failed_post_records_error_not_reviewed(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, post_raises=True)
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "error"
    assert ds.calls == 5  # we paid for the full pipeline
    assert not store.already_reviewed("owner/repo", 1, "a" * 40)
    row = store.recent(1)[0]
    assert row["outcome"] == "error" and row["cost_usd"] > 0


# -- prior PR comments as context -------------------------------------------
def test_prior_comments_injected_into_lens_and_scoring_prompts(tmp_path):
    issue = [PriorComment(author="alice", body="please check error handling", created_at="2026-05-01T00:00:00Z")]
    review = [PriorComment(author="bob", body="inline note", created_at="2026-05-02T00:00:00Z",
                           kind="review", path="src/app.py", line=2)]
    gh = FakeGhClient(diff=SRC_DIFF, issue_comments=issue, review_comments=review)
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds)
    orch.review_pr(make_pr())
    lens_users = ds.user_for("## LENS: correctness")
    score_users = ds.user_for("## PASS: scoring")
    assert lens_users and all("PRIOR PR DISCUSSION" in u for u in lens_users)
    assert score_users and all("PRIOR PR DISCUSSION" in u for u in score_users)
    assert any("src/app.py:2" in u for u in lens_users)  # inline stream rendered too
    assert gh.comment_calls == 2  # both streams fetched once each


def test_prior_comment_suppresses_already_raised_finding(tmp_path):
    # The bot's own earlier review (an issue comment) already raised this finding.
    issue = [PriorComment(author="reviewbot", body="ALREADY-RAISED returns wrong value",
                          created_at="2026-05-01T00:00:00Z")]

    def scorer(system, user):
        # The injected prior block carries the sentinel into the scoring prompt.
        return SCORE_ZERO if "ALREADY-RAISED" in user else SCORE_HIGH

    gh = FakeGhClient(diff=SRC_DIFF, issue_comments=issue)
    ds = FakeDeepSeekClient(responses=_responses(score=scorer))
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    body = gh.posted[0][2]
    assert "[BLOCKER]" not in body  # duplicate finding scored 0 → dropped
    assert store.already_reviewed("owner/repo", 1, "a" * 40)


def test_comment_fetch_failure_degrades_to_no_context(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, comments_raise=True)
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "review"  # comment-fetch failure must not fail the review
    assert "PRIOR PR DISCUSSION" not in gh.posted[0][2]
    assert all("PRIOR PR DISCUSSION" not in u for u in ds.user_for("## LENS: correctness"))


def test_read_prior_comments_disabled_skips_fetch(tmp_path):
    issue = [PriorComment(author="a", body="x", created_at="2026-01-01T00:00:00Z")]
    gh = FakeGhClient(diff=SRC_DIFF, issue_comments=issue)
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds, read_prior_comments=False)
    orch.review_pr(make_pr())
    assert gh.comment_calls == 0
    assert all("PRIOR PR DISCUSSION" not in u for u in ds.user_for("## LENS: correctness"))


# -- @mention-triggered re-review -------------------------------------------
def _mention(cid, author="alice"):
    return PriorComment(author=author, body="@reviewbot please take another look",
                        created_at="2026-06-01T00:00:00Z", comment_id=cid)


def test_mention_first_sight_baselines_without_firing(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, issue_comments=[_mention(10)])
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.rereview_if_mentioned(make_pr(), just_reviewed=False)
    assert out is None              # first sight only baselines
    assert gh.posted == []
    assert store.last_mention_id("owner/repo", 1) == 10


def test_mention_fires_rereview_past_watermark(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, issue_comments=[_mention(10)])
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds)
    store.set_mention_id("owner/repo", 1, 0)  # not first sight
    out = orch.rereview_if_mentioned(make_pr(), just_reviewed=False)
    assert out is not None and out.action == "review"
    body = gh.posted[0][2]
    assert "Re-review" in body and "@alice" in body
    assert store.recent(1)[0]["outcome"] == "rereviewed"
    assert store.last_mention_id("owner/repo", 1) == 10


def test_mention_not_refired_next_cycle(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, issue_comments=[_mention(10)])
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds)
    store.set_mention_id("owner/repo", 1, 0)
    orch.rereview_if_mentioned(make_pr(), just_reviewed=False)  # fires, wm→10
    n = len(gh.posted)
    out2 = orch.rereview_if_mentioned(make_pr(), just_reviewed=False)  # same comment
    assert out2 is None and len(gh.posted) == n


def test_bot_own_mention_does_not_fire(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, issue_comments=[_mention(10, author="reviewbot")])
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds)
    store.set_mention_id("owner/repo", 1, 0)
    out = orch.rereview_if_mentioned(make_pr(), just_reviewed=False)
    assert out is None and gh.posted == []
    assert store.last_mention_id("owner/repo", 1) == 10  # watermark still advances


def test_just_reviewed_advances_watermark_without_firing(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, issue_comments=[_mention(10)])
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds)
    store.set_mention_id("owner/repo", 1, 0)
    out = orch.rereview_if_mentioned(make_pr(), just_reviewed=True)
    assert out is None and gh.posted == []
    assert store.last_mention_id("owner/repo", 1) == 10


def test_rereview_disabled_skips_fetch_and_fire(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, issue_comments=[_mention(10)])
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds, rereview_on_mention=False)
    out = orch.rereview_if_mentioned(make_pr(), just_reviewed=False)
    assert out is None and gh.comment_calls == 0 and gh.posted == []


def test_mention_first_sight_fetch_failure_skips_baseline(tmp_path):
    # First-sight comment fetch fails: must NOT baseline (else the watermark sticks
    # at 0 and every historical @mention fires next cycle). Stay unscanned, retry later.
    gh = FakeGhClient(diff=SRC_DIFF, issue_comments=[_mention(10)], comments_raise=True)
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.rereview_if_mentioned(make_pr(), just_reviewed=False)
    assert out is None and gh.posted == []
    assert store.last_mention_id("owner/repo", 1) is None  # not baselined at 0


def test_mention_no_storm_when_fetch_recovers_after_failed_first_sight(tmp_path):
    # The deploy-storm guard: a failed first-sight fetch followed by a successful one
    # must baseline silently on the real first sight — never replay historical @mentions.
    gh = FakeGhClient(diff=SRC_DIFF, issue_comments=[_mention(10)], comments_raise=True)
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds)
    orch.rereview_if_mentioned(make_pr(), just_reviewed=False)  # fetch fails, no baseline
    gh.comments_raise = False                                   # next poll: fetch recovers
    out = orch.rereview_if_mentioned(make_pr(), just_reviewed=False)
    assert out is None and gh.posted == []                      # first real sight: baseline only
    assert store.last_mention_id("owner/repo", 1) == 10


def test_mention_fetch_failure_after_baseline_is_noop(tmp_path):
    # A transient fetch failure on a later cycle is harmless: no fire, no crash,
    # watermark unchanged — retried next cycle.
    gh = FakeGhClient(diff=SRC_DIFF, issue_comments=[_mention(10)], comments_raise=True)
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds)
    store.set_mention_id("owner/repo", 1, 5)  # already scanned
    out = orch.rereview_if_mentioned(make_pr(), just_reviewed=False)
    assert out is None and gh.posted == []
    assert store.last_mention_id("owner/repo", 1) == 5
