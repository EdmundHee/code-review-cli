"""Tests for hybrid-provider routing: planner+scoring → advisor, lenses → deepseek.

All three tests use the same diff + canned-response setup from test_review_fetch_context.py.
The advisor (FakeClaudeCliClient) has prices=Prices(0,0) so its tokens cost $0.
"""

from datetime import datetime, timezone

from ghcr.review import ReviewOrchestrator
from ghcr.state import StateStore
from ghcr.cost import estimate_cost_usd
from ghcr.models import merge_usages
from tests.fakes import FakeDeepSeekClient, FakeClaudeCliClient, FakeGhClient, make_config, make_pr

FIXED = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)

# Minimal diff with a real source file so lenses see something.
SRC_DIFF = """\
diff --git a/src/app.py b/src/app.py
index 1..2 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 from db.base import BaseConnector
 def f(c):
-    return c.execute()
+    c.close()
+    return c.execute()
"""

# Canned lens / score / planner responses that parse cleanly.
# Empty planner: zero symbols → no resolution needed → trivial path.
PLANNER_EMPTY = '{"requests":[]}'
CORR = '[{"severity":"BLOCKER","file":"src/app.py","area":"f","issue":"use after close","fix":"reconnect"}]'
EMPTY = "[]"
COV = '{"verdict":{"has_tests":false,"detail":"no test"},"findings":[]}'
SCORE = '{"confidence":95,"reason":"x"}'


def _ds_responses():
    """DeepSeek (worker) handles lenses only."""
    return {
        "## LENS: correctness": CORR,
        "## LENS: security": EMPTY,
        "## LENS: maintainability": EMPTY,
        "## LENS: test_coverage": COV,
    }


def _advisor_responses():
    """Advisor (Claude) handles planner + scoring."""
    return {
        "## PASS: context": PLANNER_EMPTY,
        "## PASS: scoring": SCORE,
    }


def _orch_hybrid(tmp_path):
    """Build an orchestrator with a real advisor separate from the worker."""
    cfg = make_config(
        db_path=str(tmp_path / "ghcr.db"),
        review_mode="multi",
        scoring_votes=1,
        confidence_threshold=0,
        fetch_referenced_context=True,
        advisor_provider="claude",
    )
    store = StateStore(cfg.db_path)
    gh = FakeGhClient(diff=SRC_DIFF)
    ds = FakeDeepSeekClient(responses=_ds_responses())
    advisor = FakeClaudeCliClient(responses=_advisor_responses())
    orch = ReviewOrchestrator(gh, ds, store, cfg, now=lambda: FIXED, advisor=advisor)
    return orch, store, gh, ds, advisor, cfg


# ---------------------------------------------------------------------------
# Test 1: routing — planner+scoring go to advisor; lenses stay on deepseek.
# ---------------------------------------------------------------------------
def test_planner_and_scoring_route_to_advisor(tmp_path):
    orch, store, gh, ds, advisor, _cfg = _orch_hybrid(tmp_path)
    out = orch.review_pr(make_pr())

    assert out.action == "review", f"Expected review, got {out.action}"

    # Advisor handled planner and scoring.
    assert advisor.calls_matching("## PASS: context") == 1, "planner must go to advisor"
    assert advisor.calls_matching("## PASS: scoring") >= 1, "scoring must go to advisor"

    # DeepSeek handled lenses.
    assert ds.calls_matching("## LENS:") >= 1, "lenses must stay on deepseek"

    # Advisor must NOT have run any lenses.
    assert advisor.calls_matching("## LENS:") == 0, "advisor must not run lenses"

    # DeepSeek must NOT have run scoring.
    assert ds.calls_matching("## PASS: scoring") == 0, "deepseek must not run scoring"

    # DeepSeek must NOT have run the planner.
    assert ds.calls_matching("## PASS: context") == 0, "deepseek must not run planner"


# ---------------------------------------------------------------------------
# Test 2: cost — advisor tokens (prices=0,0) are excluded from cost_usd.
# ---------------------------------------------------------------------------
def test_advisor_tokens_billed_at_zero(tmp_path):
    orch, store, gh, ds, advisor, cfg = _orch_hybrid(tmp_path)
    out = orch.review_pr(make_pr())

    assert out.action == "review"

    # Advisor prices are 0/0 so its tokens contribute $0.
    # Worker (deepseek) runs 4 lenses; each call uses FakeDeepSeekClient.usage = 1000/500 tokens.
    lens_calls = ds.calls_matching("## LENS:")
    assert lens_calls >= 1

    # Compute expected cost: only lens calls at deepseek prices. Derive the prices
    # from the same config the orchestrator used so the test can't drift from the default.
    ds_usage = ds.usage  # Usage(prompt_tokens=1000, completion_tokens=500)
    ds_prices = cfg.deepseek.prices
    # Each lens call produces one usage; merge_usages accumulates them.
    worker_usages = [ds_usage] * lens_calls
    expected_cost = estimate_cost_usd(merge_usages(worker_usages), ds_prices)

    # The advisor's planner + scoring calls are at price $0, adding nothing.
    assert abs(out.cost_usd - expected_cost) < 1e-9, (
        f"Expected cost {expected_cost} (lens-only), got {out.cost_usd}"
    )


# ---------------------------------------------------------------------------
# Test 3: default path (no advisor kwarg) — behavior unchanged, single-price.
# ---------------------------------------------------------------------------
def test_default_advisor_is_worker_unchanged(tmp_path):
    """When no advisor is passed, self.advisor is self.deepseek; behavior is identical
    to the pre-hybrid path — all calls on ds, cost at deepseek prices."""
    all_responses = {
        "## PASS: context": PLANNER_EMPTY,
        "## LENS: correctness": CORR,
        "## LENS: security": EMPTY,
        "## LENS: maintainability": EMPTY,
        "## LENS: test_coverage": COV,
        "## PASS: scoring": SCORE,
    }
    cfg = make_config(
        db_path=str(tmp_path / "ghcr.db"),
        review_mode="multi",
        scoring_votes=1,
        confidence_threshold=0,
        fetch_referenced_context=True,
    )
    store = StateStore(cfg.db_path)
    gh = FakeGhClient(diff=SRC_DIFF)
    ds = FakeDeepSeekClient(responses=all_responses)
    # NO advisor kwarg — self.advisor defaults to self.deepseek (proves the default
    # via construction, not config).
    orch = ReviewOrchestrator(gh, ds, store, cfg, now=lambda: FIXED)
    out = orch.review_pr(make_pr())

    assert out.action == "review", f"Expected review, got {out.action}"

    # All routing hits deepseek: planner + lenses + scoring.
    assert ds.calls_matching("## PASS: context") == 1
    assert ds.calls_matching("## LENS:") >= 1
    assert ds.calls_matching("## PASS: scoring") >= 1

    # Cost is positive (deepseek prices > 0).
    assert out.cost_usd > 0


# ---------------------------------------------------------------------------
# Test 4: best-effort — a failing advisor (planner + all scoring votes raise
# ClaudeCliError) must NOT fail the review; it degrades to diff-only / dropped
# findings and STILL posts. Mirrors an expired claude subscription auth.
# ---------------------------------------------------------------------------
def test_advisor_failure_still_posts_review(tmp_path):
    from ghcr.models import ACTION_REVIEW

    cfg = make_config(
        db_path=str(tmp_path / "ghcr.db"),
        review_mode="multi",
        scoring_votes=1,
        confidence_threshold=0,
        fetch_referenced_context=True,
        advisor_provider="claude",
    )
    store = StateStore(cfg.db_path)
    gh = FakeGhClient(diff=SRC_DIFF)
    # Lenses run normally and produce at least one finding so the body is non-empty.
    ds = FakeDeepSeekClient(responses=_ds_responses())
    # The advisor raises on EVERY call: the planner and every scoring vote both fail.
    advisor = FakeClaudeCliClient(raises=True)
    orch = ReviewOrchestrator(gh, ds, store, cfg, now=lambda: FIXED, advisor=advisor)

    out = orch.review_pr(make_pr())

    # Best-effort: the claude failure degrades, it never blocks or fails the review.
    assert out.action == ACTION_REVIEW, f"Expected review despite advisor failure, got {out.action}"
    assert gh.posted, "a comment must still be posted when the advisor fails"
    # The advisor was actually exercised (planner + scoring attempts), all raising.
    assert advisor.calls >= 1
