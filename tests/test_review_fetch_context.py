"""Referenced-context fetch (the planner pass + gh resolution) in the multi-pass review.

These set fetch_referenced_context=True explicitly — the test make_config defaults it OFF
so the broader multi-pass suite's call-count assertions stay stable.
"""

from datetime import datetime, timezone

from ghcr.deepseek import DeepSeekError
from ghcr.review import ReviewOrchestrator
from ghcr.state import StateStore
from tests.fakes import FakeDeepSeekClient, FakeGhClient, make_config, make_pr

FIXED = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)

# A diff that closes a connector then reuses it — the classic "use after close" smell
# whose verdict actually depends on BaseConnector (defined elsewhere, not in the diff).
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
SCORE = '{"confidence":95,"reason":"x"}'
PLANNER = '{"requests":[{"symbol":"BaseConnector","module_hint":"db.base","reason":"subclassed"}]}'
BASE_SRC = (
    "import os\n"
    "\n"
    "class BaseConnector:\n"
    "    def close(self):\n"
    "        self._c = None  # lazy: reconnects on next use\n"
    "    def execute(self):\n"
    "        return self._c.run()\n"
)


def _responses(*, corr=CORR, sec=EMPTY, maint=EMPTY, cov=COV, score=SCORE, planner=PLANNER):
    return {
        "## PASS: context": planner,
        "## LENS: correctness": corr,
        "## LENS: security": sec,
        "## LENS: maintainability": maint,
        "## LENS: test_coverage": cov,
        "## PASS: scoring": score,
    }


def _orch(tmp_path, gh, ds, **cfg_kw):
    cfg_kw.setdefault("fetch_referenced_context", True)
    cfg = make_config(db_path=str(tmp_path / "ghcr.db"), review_mode="multi", **cfg_kw)
    store = StateStore(cfg.db_path)
    return ReviewOrchestrator(gh, ds, store, cfg, now=lambda: FIXED), store


def test_referenced_context_reaches_lens_and_scoring_prompts(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"db/base.py": BASE_SRC})
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    assert ds.calls_matching("## PASS: context") == 1  # planner ran once
    lens_users = ds.user_for("## LENS: correctness")
    score_users = ds.user_for("## PASS: scoring")
    assert lens_users and all("REFERENCED DEFINITIONS" in u for u in lens_users)
    assert any("lazy: reconnects" in u for u in lens_users)  # the real def is in the prompt
    assert score_users and all("REFERENCED DEFINITIONS" in u for u in score_users)
    # resolved straight from the module hint (db.base -> db/base.py); no search needed
    assert gh.file_calls == 1 and gh.search_calls == 0


def test_planner_usage_counted_in_cost(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"db/base.py": BASE_SRC})
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds)
    orch.review_pr(make_pr())
    # planner(1) + 4 lenses + 1 score = 6 calls, each 1000/500 tokens
    assert ds.calls == 6
    row = store.recent(1)[0]
    # Default (deepseek) path: advisor IS the worker, so planner+scoring fold into the
    # worker total — prompt_tokens is the FULL review (all 6 DeepSeek calls).
    assert row["prompt_tokens"] == 6000 and row["completion_tokens"] == 3000


def test_search_fallback_when_no_module_hint(tmp_path):
    planner = '{"requests":[{"symbol":"BaseConnector","reason":"subclassed"}]}'  # no hint
    gh = FakeGhClient(
        diff=SRC_DIFF,
        search_results={"BaseConnector": [{"path": "db/base.py"}]},
        file_contents={"db/base.py": BASE_SRC},
    )
    ds = FakeDeepSeekClient(responses=_responses(planner=planner))
    orch, store = _orch(tmp_path, gh, ds)
    orch.review_pr(make_pr())
    assert gh.search_calls == 1 and gh.file_calls == 1
    assert any("class BaseConnector" in u for u in ds.user_for("## LENS: correctness"))


def test_planner_failure_degrades_to_no_block(tmp_path):
    def fail_planner(system, user):
        raise DeepSeekError("planner down")

    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"db/base.py": BASE_SRC})
    ds = FakeDeepSeekClient(responses=_responses(planner=fail_planner))
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "review"  # planner failure must not fail the review
    assert all("REFERENCED DEFINITIONS" not in u for u in ds.user_for("## LENS: correctness"))
    assert gh.file_calls == 0  # nothing parsed -> no resolution attempted


def test_gh_failure_during_resolution_lists_symbol_unresolved(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, code_raises=True)  # search + file both raise GhError
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "review"  # gh failure must not fail the review
    # the failed lookup is surfaced as UNRESOLVED so the scorer's cap-at-25 can fire
    lens_users = ds.user_for("## LENS: correctness")
    assert lens_users and all("UNRESOLVED" in u and "BaseConnector" in u for u in lens_users)


def test_unresolved_symbol_listed_for_lens_and_scorer(tmp_path):
    # planner asks for a symbol that neither hint nor search can locate
    gh = FakeGhClient(diff=SRC_DIFF)  # no file_contents, empty search
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    for pass_key in ("## LENS: correctness", "## PASS: scoring"):
        users = ds.user_for(pass_key)
        assert users and all("UNRESOLVED" in u and "BaseConnector" in u for u in users)


def test_fetch_disabled_skips_planner_and_gh(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"db/base.py": BASE_SRC})
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds, fetch_referenced_context=False)
    orch.review_pr(make_pr())
    assert ds.calls_matching("## PASS: context") == 0
    assert gh.search_calls == 0 and gh.file_calls == 0
    assert all("REFERENCED DEFINITIONS" not in u for u in ds.user_for("## LENS: correctness"))


def _orch_with_bus(tmp_path, gh, ds, **cfg_kw):
    """Like ``_orch`` but wires a real EventBus and returns the recorded
    AgentEvents so the planner's progress signal can be asserted."""
    from ghcr.events import AgentEvent, EventBus

    events: list = []
    bus = EventBus()
    bus.subscribe(lambda e: events.append(e) if isinstance(e, AgentEvent) else None)
    cfg_kw.setdefault("fetch_referenced_context", True)
    cfg = make_config(db_path=str(tmp_path / "ghcr.db"), review_mode="multi", **cfg_kw)
    store = StateStore(cfg.db_path)
    orch = ReviewOrchestrator(gh, ds, store, cfg, now=lambda: FIXED, bus=bus)
    return orch, store, events


def test_planner_emits_context_agent_running_then_done(tmp_path):
    # The planner is the one slow pre-lens step; without an agent event the TUI
    # row sits on bare "polling…" for its whole duration. It must announce itself.
    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"db/base.py": BASE_SRC})
    ds = FakeDeepSeekClient(responses=_responses())
    orch, _store, events = _orch_with_bus(tmp_path, gh, ds)
    orch.review_pr(make_pr())
    ctx = [e for e in events if e.agent == "context"]
    assert [e.status for e in ctx] == ["running", "done"]
    assert all(e.pr_number == make_pr().number for e in ctx)


def test_planner_failure_emits_failed_context_event(tmp_path):
    def fail_planner(system, user):
        raise DeepSeekError("planner down")

    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"db/base.py": BASE_SRC})
    ds = FakeDeepSeekClient(responses=_responses(planner=fail_planner))
    orch, _store, events = _orch_with_bus(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "review"  # still degrades gracefully
    ctx = [e for e in events if e.agent == "context"]
    assert ctx and ctx[-1].status == "failed"


def test_planner_disabled_emits_no_context_event(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"db/base.py": BASE_SRC})
    ds = FakeDeepSeekClient(responses=_responses())
    orch, _store, events = _orch_with_bus(tmp_path, gh, ds, fetch_referenced_context=False)
    orch.review_pr(make_pr())
    assert not [e for e in events if e.agent == "context"]


def test_module_hint_candidates_resolve_js_paths(tmp_path):
    # a JS-style relative hint must resolve by trying language extensions, not just .py
    planner = '{"requests":[{"symbol":"helper","module_hint":"./utils/helpers"}]}'
    gh = FakeGhClient(
        diff=SRC_DIFF,
        file_contents={"utils/helpers.ts": "export function helper(a) {\n  return a;\n}\n"},
    )
    ds = FakeDeepSeekClient(responses=_responses(planner=planner))
    orch, store = _orch(tmp_path, gh, ds)
    orch.review_pr(make_pr())
    assert gh.search_calls == 0  # resolved from the hint, no search fallback
    assert any("export function helper" in u for u in ds.user_for("## LENS: correctness"))


def test_usage_only_snippet_is_marked_unverified(tmp_path):
    # fetched file merely mentions the symbol — the snippet must not pose as a definition
    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"db/base.py": "x = BaseConnector()\n"})
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds)
    orch.review_pr(make_pr())
    assert any("NOT a verified definition" in u for u in ds.user_for("## LENS: correctness"))


def test_planner_runs_with_thinking_disabled(tmp_path):
    # symbol listing needs no deep reasoning; lenses/scoring keep the client default
    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"db/base.py": BASE_SRC})
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds)
    orch.review_pr(make_pr())
    assert ds.thinking_for("## PASS: context") == ["disabled"]
    assert all(t is None for t in ds.thinking_for("## LENS: correctness"))
    assert all(t is None for t in ds.thinking_for("## PASS: scoring"))


def test_max_symbols_caps_resolution(tmp_path):
    planner = (
        '{"requests":['
        '{"symbol":"A","module_hint":"db.a"},'
        '{"symbol":"B","module_hint":"db.b"},'
        '{"symbol":"C","module_hint":"db.c"}]}'
    )
    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"*": "class X:\n    pass\n"})
    ds = FakeDeepSeekClient(responses=_responses(planner=planner))
    orch, store = _orch(tmp_path, gh, ds, referenced_max_symbols=2)
    orch.review_pr(make_pr())
    assert gh.file_calls == 2  # only the first 2 of 3 requests resolved
