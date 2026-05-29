from datetime import datetime, timezone

from ghcr.deepseek import DeepSeekError
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
    assert row["prompt_tokens"] == 5000 and row["completion_tokens"] == 2500
    assert store.already_reviewed("owner/repo", 1, "a" * 40)


def test_below_threshold_findings_filtered_but_still_posts(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF)
    ds = FakeDeepSeekClient(responses=_responses(score=SCORE_LOW))
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    body = gh.posted[0][2]
    assert "No high-confidence issues" in body
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
