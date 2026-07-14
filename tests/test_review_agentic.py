"""Agentic scoring pass: findings verified by `claude -p` (fake) inside a PR-head
worktree instead of the prompt-only advisor scorer.

Reuses the diff + canned-response shape from test_review_hybrid.py. The verifier
(FakeAgenticVerifier) and worktree (FakeGitRepoCache) are fakes — no subprocess, no git.
"""

from datetime import datetime, timezone

from ghcr.review import ReviewOrchestrator
from ghcr.state import StateStore
from tests.fakes import (
    FakeAgenticVerifier,
    FakeDeepSeekClient,
    FakeGhClient,
    FakeGitRepoCache,
    make_config,
    make_pr,
)

FIXED = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

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

CORR = '[{"severity":"BLOCKER","file":"src/app.py","area":"f","issue":"use after close","fix":"reconnect"}]'
EMPTY = "[]"
COV = '{"verdict":{"has_tests":false,"detail":"no test"},"findings":[]}'
SCORE = '{"confidence":95,"reason":"advisor"}'
VERIFY = '{"confidence":88,"reason":"read the code, real"}'


def _ds_responses():
    return {
        "## LENS: correctness": CORR,
        "## LENS: security": EMPTY,
        "## LENS: maintainability": EMPTY,
        "## LENS: test_coverage": COV,
    }


def _build(tmp_path, *, agentic=True, verifier=None, gitrepo=None, ds=None, bus=None, lenses=None):
    cfg = make_config(
        db_path=str(tmp_path / "ghcr.db"),
        review_mode="multi",
        scoring_votes=1,
        confidence_threshold=0,
        agentic_scoring=agentic,
        local_checkout=True,
        lenses=lenses or ("correctness", "security", "maintainability", "test_coverage"),
    )
    store = StateStore(cfg.db_path)
    gh = FakeGhClient(diff=SRC_DIFF)
    if ds is None:
        ds = FakeDeepSeekClient(responses=_ds_responses())
    if verifier is None:
        verifier = FakeAgenticVerifier(content=VERIFY)
    if gitrepo is None:
        gitrepo = FakeGitRepoCache(worktree_dir="/tmp/wt-head")
    orch = ReviewOrchestrator(
        gh, ds, store, cfg, now=lambda: FIXED, gitrepo=gitrepo, verifier=verifier, bus=bus,
    )
    return orch, gh, ds, verifier, gitrepo, cfg


def test_findings_scored_by_verifier_in_worktree(tmp_path):
    orch, gh, ds, verifier, gitrepo, _ = _build(tmp_path)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    # The one finding was scored by the verifier (1 finding × 1 vote), not the advisor.
    assert verifier.calls == 1
    assert verifier.calls_matching("## PASS: agentic-scoring") == 1
    assert ds.calls_matching("## PASS: scoring") == 0  # advisor scorer never ran
    # Every verify() ran with the worktree as cwd.
    assert verifier.cwds == ["/tmp/wt-head"]
    # Worktree created once, torn down once.
    assert gitrepo.worktree_calls == 1
    assert gitrepo.remove_calls == 1
    assert gh.posted, "review comment must be posted"


def test_worktree_failure_falls_back_to_advisor(tmp_path):
    # `git worktree add` fails → wt_dir None → prompt-only advisor scoring, review still posts.
    gitrepo = FakeGitRepoCache(worktree_dir=None)
    ds = FakeDeepSeekClient(responses={**_ds_responses(), "## PASS: scoring": SCORE})
    orch, gh, ds, verifier, gitrepo, _ = _build(tmp_path, gitrepo=gitrepo, ds=ds)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    assert verifier.calls == 0                       # never used
    assert ds.calls_matching("## PASS: scoring") == 1  # advisor scorer picked it up
    assert gitrepo.remove_calls == 0                 # nothing to remove
    assert gh.posted


def test_flag_off_never_uses_verifier(tmp_path):
    ds = FakeDeepSeekClient(responses={**_ds_responses(), "## PASS: scoring": SCORE})
    orch, gh, ds, verifier, gitrepo, _ = _build(tmp_path, agentic=False, ds=ds)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    assert verifier.calls == 0
    assert gitrepo.worktree_calls == 0               # not even attempted
    assert ds.calls_matching("## PASS: scoring") == 1


def test_verifier_raises_falls_back_to_advisor_vote(tmp_path):
    # All agentic votes fail → one non-agentic advisor vote as the safety net.
    verifier = FakeAgenticVerifier(raises=True)
    ds = FakeDeepSeekClient(responses={**_ds_responses(), "## PASS: scoring": SCORE})
    orch, gh, ds, verifier, gitrepo, _ = _build(tmp_path, verifier=verifier, ds=ds)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    assert verifier.calls == 1                        # attempted
    assert ds.calls_matching("## PASS: scoring") == 1  # fallback advisor vote fired
    assert gh.posted


def test_consistency_finding_stays_on_advisor(tmp_path):
    # A consistency finding must route to the consistency advisor scorer, never the verifier.
    CONS = ('[{"severity":"WARNING","file":"src/app.py","area":"f",'
            '"issue":"sibling divergence","fix":"align"}]')
    ds = FakeDeepSeekClient(responses={
        "## LENS: correctness": EMPTY,
        "## LENS: security": EMPTY,
        "## LENS: maintainability": EMPTY,
        "## LENS: test_coverage": COV,
        "## LENS: consistency": CONS,
        # consistency scorer key must be inserted before the generic scoring key would
        # matter, but here the generic key is absent — only consistency scoring runs.
        "## PASS: scoring-consistency": '{"confidence":90,"reason":"anchored"}',
    })
    orch, gh, ds, verifier, gitrepo, _ = _build(
        tmp_path, ds=ds,
        lenses=("correctness", "security", "maintainability", "test_coverage", "consistency"),
    )
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    assert verifier.calls == 0                                    # verifier never touched consistency
    assert ds.calls_matching("## PASS: scoring-consistency") == 1  # consistency scorer ran on advisor


def test_verifier_tokens_billed_at_zero(tmp_path):
    orch, gh, ds, verifier, gitrepo, cfg = _build(tmp_path)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    # Verifier prices are 0/0; only the 4 deepseek lens calls cost money.
    from ghcr.cost import estimate_cost_usd
    from ghcr.models import merge_usages
    lens_calls = ds.calls_matching("## LENS:")
    expected = estimate_cost_usd(merge_usages([ds.usage] * lens_calls), cfg.deepseek.prices)
    assert abs(out.cost_usd - expected) < 1e-9


def test_no_local_checkout_no_worktree(tmp_path):
    # ensure() fails → local not ready → no worktree attempt → advisor scoring.
    gitrepo = FakeGitRepoCache(ensure_ok=False)
    ds = FakeDeepSeekClient(responses={**_ds_responses(), "## PASS: scoring": SCORE})
    orch, gh, ds, verifier, gitrepo, _ = _build(tmp_path, gitrepo=gitrepo, ds=ds)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    assert gitrepo.worktree_calls == 0
    assert verifier.calls == 0
    assert ds.calls_matching("## PASS: scoring") == 1
