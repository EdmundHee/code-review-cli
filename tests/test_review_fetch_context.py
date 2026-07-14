"""Referenced-context fetch (the planner pass + gh resolution) in the multi-pass review.

These set fetch_referenced_context=True explicitly — the test make_config defaults it OFF
so the broader multi-pass suite's call-count assertions stay stable.
"""

from datetime import datetime, timezone

from ghcr.deepseek import DeepSeekError
from ghcr.review import ReviewOrchestrator
from ghcr.state import StateStore
from tests.fakes import FakeDeepSeekClient, FakeGhClient, FakeGitRepoCache, make_config, make_pr

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


def _orch(tmp_path, gh, ds, gitrepo=None, **cfg_kw):
    cfg_kw.setdefault("fetch_referenced_context", True)
    if gitrepo is not None:
        cfg_kw.setdefault("local_checkout", True)
    cfg = make_config(db_path=str(tmp_path / "ghcr.db"), review_mode="multi", **cfg_kw)
    store = StateStore(cfg.db_path)
    return ReviewOrchestrator(gh, ds, store, cfg, now=lambda: FIXED, gitrepo=gitrepo), store


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


def test_planner_failure_degrades_to_no_block(tmp_path, monkeypatch):
    monkeypatch.setattr("ghcr.review.PLANNER_RETRY_DELAY_S", 0)

    def fail_planner(system, user):
        raise DeepSeekError("planner down")

    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"db/base.py": BASE_SRC})
    ds = FakeDeepSeekClient(responses=_responses(planner=fail_planner))
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "review"  # planner failure must not fail the review
    assert ds.calls_matching("## PASS: context") == 2  # one retry before giving up
    assert all("REFERENCED DEFINITIONS" not in u for u in ds.user_for("## LENS: correctness"))
    assert gh.file_calls == 0  # nothing parsed -> no resolution attempted


def test_planner_transient_failure_retries_once_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr("ghcr.review.PLANNER_RETRY_DELAY_S", 0)
    state = {"n": 0}

    def flaky_planner(system, user):
        state["n"] += 1
        if state["n"] == 1:
            raise DeepSeekError("transient")
        return PLANNER

    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"db/base.py": BASE_SRC})
    ds = FakeDeepSeekClient(responses=_responses(planner=flaky_planner))
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    assert ds.calls_matching("## PASS: context") == 2  # failed once, retried, succeeded
    lens_users = ds.user_for("## LENS: correctness")
    assert lens_users and all("REFERENCED DEFINITIONS" in u for u in lens_users)


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


TESTS_PLANNER = '{"requests":[{"symbol":"BaseConnector","module_hint":"db.base","kind":"tests","reason":"default changed"}]}'
TEST_SRC = "def test_close_reconnects():\n    assert BaseConnector().close() is None\n"


def test_kind_tests_resolved_via_search_prefers_test_path(tmp_path):
    gh = FakeGhClient(
        diff=SRC_DIFF,
        search_results={"BaseConnector": [{"path": "src/base.py"}, {"path": "tests/test_base.py"}]},
        file_contents={"tests/test_base.py": TEST_SRC},
    )
    ds = FakeDeepSeekClient(responses=_responses(planner=TESTS_PLANNER))
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    # tests kind skips the module hint entirely: search once, fetch only the test file
    assert gh.search_calls == 1 and gh.file_calls == 1
    lens_users = ds.user_for("## LENS: correctness")
    assert lens_users and all("EXISTING TEST" in u for u in lens_users)
    assert any("test_close_reconnects" in u for u in lens_users)


def test_kind_tests_no_test_like_hit_is_unresolved(tmp_path):
    gh = FakeGhClient(
        diff=SRC_DIFF,
        search_results={"BaseConnector": [{"path": "src/base.py"}]},  # no test-like path
        file_contents={"src/base.py": BASE_SRC},
    )
    ds = FakeDeepSeekClient(responses=_responses(planner=TESTS_PLANNER))
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    assert gh.file_calls == 0  # no test-like hit -> never fetched
    for pass_key in ("## LENS: correctness", "## PASS: scoring"):
        users = ds.user_for(pass_key)
        assert users and all("UNRESOLVED" in u and "BaseConnector" in u for u in users)


# -- local-clone resolution (gitrepo), with gh fallback ----------------------

def test_local_hint_hit_resolves_without_gh(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"db/base.py": BASE_SRC})  # gh present but must go unused
    git = FakeGitRepoCache(files={"db/base.py": BASE_SRC})
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds, gitrepo=git)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    lens_users = ds.user_for("## LENS: correctness")
    assert lens_users and any("lazy: reconnects" in u for u in lens_users)
    assert gh.file_calls == 0 and gh.search_calls == 0  # local resolved it
    assert git.ensure_calls == 1 and git.show_calls == 1


def test_local_grep_hit_when_no_hint(tmp_path):
    planner = '{"requests":[{"symbol":"BaseConnector","reason":"subclassed"}]}'  # no hint
    gh = FakeGhClient(diff=SRC_DIFF)
    git = FakeGitRepoCache(files={"db/base.py": BASE_SRC}, grep_hits={"BaseConnector": ["db/base.py"]})
    ds = FakeDeepSeekClient(responses=_responses(planner=planner))
    orch, store = _orch(tmp_path, gh, ds, gitrepo=git)
    orch.review_pr(make_pr())
    assert git.grep_calls == 1 and git.show_calls == 1
    assert gh.file_calls == 0 and gh.search_calls == 0
    assert any("class BaseConnector" in u for u in ds.user_for("## LENS: correctness"))


def test_local_miss_falls_back_to_gh(tmp_path):
    # local clone ready but the symbol isn't found there -> gh path resolves it
    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"db/base.py": BASE_SRC})
    git = FakeGitRepoCache(files={}, grep_hits={})  # empty checkout
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds, gitrepo=git)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    assert gh.file_calls == 1  # gh fallback resolved it
    assert any("lazy: reconnects" in u for u in ds.user_for("## LENS: correctness"))


def test_ensure_failure_uses_gh_and_ensures_once(tmp_path):
    # the incident regression: checkout unavailable -> every symbol via gh, ONE ensure
    planner = (
        '{"requests":['
        '{"symbol":"A","module_hint":"db.a"},'
        '{"symbol":"B","module_hint":"db.b"},'
        '{"symbol":"C","module_hint":"db.c"}]}'
    )
    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"*": "class X:\n    pass\n"})
    git = FakeGitRepoCache(ensure_ok=False)
    ds = FakeDeepSeekClient(responses=_responses(planner=planner))
    orch, store = _orch(tmp_path, gh, ds, gitrepo=git)
    orch.review_pr(make_pr())
    assert git.ensure_calls == 1  # memoized despite 3 symbols
    assert git.show_calls == 0 and git.grep_calls == 0  # never read when unavailable
    assert gh.file_calls == 3  # all three resolved via gh


def test_local_checkout_disabled_skips_ensure(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"db/base.py": BASE_SRC})
    git = FakeGitRepoCache(files={"db/base.py": BASE_SRC})
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds, gitrepo=git, local_checkout=False)
    orch.review_pr(make_pr())
    assert git.ensure_calls == 0  # local path never touched
    assert gh.file_calls == 1  # gh resolved it


def test_giterror_mid_resolution_falls_back_to_gh(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, file_contents={"db/base.py": BASE_SRC})
    git = FakeGitRepoCache(files={"db/base.py": BASE_SRC}, raises=True)  # show/grep raise GitError
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds, gitrepo=git)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    assert gh.file_calls == 1  # gh fallback carried it
    assert any("lazy: reconnects" in u for u in ds.user_for("## LENS: correctness"))


def test_kind_tests_resolved_locally(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF)
    git = FakeGitRepoCache(
        files={"tests/test_base.py": TEST_SRC},
        grep_hits={"BaseConnector": ["src/base.py", "tests/test_base.py"]},
    )
    ds = FakeDeepSeekClient(responses=_responses(planner=TESTS_PLANNER))
    orch, store = _orch(tmp_path, gh, ds, gitrepo=git)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    assert gh.search_calls == 0 and gh.file_calls == 0  # local grep + show
    lens_users = ds.user_for("## LENS: correctness")
    assert lens_users and all("EXISTING TEST" in u for u in lens_users)
    assert any("test_close_reconnects" in u for u in lens_users)


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


def test_planner_failure_emits_failed_context_event(tmp_path, monkeypatch):
    monkeypatch.setattr("ghcr.review.PLANNER_RETRY_DELAY_S", 0)

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
