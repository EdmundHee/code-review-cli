"""Consistency lens: convention fetch + its dedicated (non-cap-at-25) scorer.

These opt into the consistency lens explicitly — the test make_config pins the
original 4 lenses and defaults fetch_conventions OFF so the broader multi-pass
suite's call-count assertions stay stable.
"""

from datetime import datetime, timezone

from ghcr.review import ReviewOrchestrator
from ghcr.state import StateStore
from tests.fakes import FakeDeepSeekClient, FakeGhClient, FakeGitRepoCache, make_config, make_pr

FIXED = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)

SRC_DIFF = """\
diff --git a/src/app.py b/src/app.py
index 1..2 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 def f(c):
-    return c.execute()
+    return c.run()
"""

CORR = '[{"severity":"WARNING","file":"src/app.py","area":"f","issue":"a correctness bug here","fix":"fix it"}]'
CONS = '[{"severity":"WARNING","file":"src/app.py","area":"btn","issue":"two siblings styled differently","fix":"use the shared component"}]'
COV = '{"verdict":{"has_tests":true,"detail":"n/a"},"findings":[]}'
DEF_SCORE = '{"confidence":95,"reason":"real bug"}'
CONS_SCORE = '{"confidence":90,"reason":"anchored inconsistency"}'

CONV_LENSES = ("correctness", "test_coverage", "consistency")


def _responses(*, corr=CORR, cons=CONS, cov=COV, def_score=DEF_SCORE, cons_score=CONS_SCORE):
    # Insertion order matters: "## PASS: scoring-consistency" must precede
    # "## PASS: scoring" because the latter is a substring of the former and the
    # fake routes on the first key found in the system prompt.
    return {
        "## LENS: correctness": corr,
        "## LENS: consistency": cons,
        "## LENS: test_coverage": cov,
        "## PASS: scoring-consistency": cons_score,
        "## PASS: scoring": def_score,
    }


def _orch(tmp_path, gh, ds, gitrepo=None, **cfg_kw):
    cfg_kw.setdefault("lenses", CONV_LENSES)
    cfg_kw.setdefault("fetch_conventions", True)
    if gitrepo is not None:
        cfg_kw.setdefault("local_checkout", True)
    cfg = make_config(db_path=str(tmp_path / "ghcr.db"), review_mode="multi", **cfg_kw)
    store = StateStore(cfg.db_path)
    return ReviewOrchestrator(gh, ds, store, cfg, now=lambda: FIXED, gitrepo=gitrepo), store


# -- _fetch_conventions unit behavior ---------------------------------------
def test_fetch_conventions_reads_claude_md(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"CLAUDE.md": "ENTERPRISE-ONLY-RULE"})
    orch, _ = _orch(tmp_path, gh, FakeDeepSeekClient())
    block = orch._fetch_conventions(make_pr())
    assert "### CLAUDE.md" in block and "ENTERPRISE-ONLY-RULE" in block


def test_fetch_conventions_local_first(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF)  # no gh file_contents
    gitrepo = FakeGitRepoCache(files={"CLAUDE.md": "LOCAL-RULE"})
    orch, _ = _orch(tmp_path, gh, FakeDeepSeekClient(), gitrepo=gitrepo, local_checkout=True)
    block = orch._fetch_conventions(make_pr())
    assert "LOCAL-RULE" in block and gitrepo.show_calls >= 1


def test_fetch_conventions_gated_off_when_disabled(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"CLAUDE.md": "RULE"})
    orch, _ = _orch(tmp_path, gh, FakeDeepSeekClient(), fetch_conventions=False)
    assert orch._fetch_conventions(make_pr()) == ""
    assert gh.file_calls == 0  # no I/O when disabled


def test_fetch_conventions_gated_off_when_lens_absent(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"CLAUDE.md": "RULE"})
    orch, _ = _orch(tmp_path, gh, FakeDeepSeekClient(), lenses=("correctness", "test_coverage"))
    assert orch._fetch_conventions(make_pr()) == ""
    assert gh.file_calls == 0


def test_fetch_conventions_degrades_on_error(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, code_raises=True)  # every get_file_content raises
    orch, _ = _orch(tmp_path, gh, FakeDeepSeekClient())
    assert orch._fetch_conventions(make_pr()) == ""


def test_fetch_conventions_caps_length(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"CLAUDE.md": "x" * 50_000})
    orch, _ = _orch(tmp_path, gh, FakeDeepSeekClient(), conventions_max_chars=100)
    assert len(orch._fetch_conventions(make_pr())) <= 100


# -- end-to-end: block reaches the right prompts, scorer routes by lens ------
def test_conventions_reach_consistency_lens_and_its_scorer(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"CLAUDE.md": "ENTERPRISE-ONLY-RULE"})
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "review"

    # The convention block rides ONLY the consistency lens prompt, not the others.
    cons_lens_users = ds.user_for("## LENS: consistency")
    corr_lens_users = ds.user_for("## LENS: correctness")
    assert cons_lens_users and all("## CONVENTIONS" in u and "ENTERPRISE-ONLY-RULE" in u for u in cons_lens_users)
    assert corr_lens_users and all("## CONVENTIONS" not in u for u in corr_lens_users)


def test_consistency_finding_routed_to_its_own_scorer(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"CLAUDE.md": "ENTERPRISE-ONLY-RULE"})
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds)
    orch.review_pr(make_pr())

    # Consistency finding → the dedicated scorer, with the conventions block.
    cons_scores = ds.calls_matching("## PASS: scoring-consistency")
    assert cons_scores == 1
    assert all("## CONVENTIONS" in u for u in ds.user_for("## PASS: scoring-consistency"))

    # Correctness finding → the DEFAULT scorer. ("## PASS: scoring" is a substring of
    # the consistency header, so subtract to get default-only.)
    default_scores = ds.calls_matching("## PASS: scoring") - cons_scores
    assert default_scores == 1

    # Only the consistency scorer ever carries the conventions block.
    scored_with_conv = [u for u in ds.user_for("## PASS: scoring") if "## CONVENTIONS" in u]
    assert len(scored_with_conv) == cons_scores


def test_both_findings_survive_and_post(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"CLAUDE.md": "RULE"})
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds)
    orch.review_pr(make_pr())
    assert gh.posted, "a review comment must be posted"
    body = gh.posted[0][2]
    assert "correctness bug" in body and "two siblings styled differently" in body
