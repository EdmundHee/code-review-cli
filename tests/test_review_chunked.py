"""Chunked multi-mode review: an over-cap FILTERED diff is split into whole-file
chunks and fully reviewed instead of skipped. Assert call counts/sets, never order
(the lens fan-out runs in a thread pool)."""

from datetime import datetime, timezone

from ghcr.deepseek import DeepSeekError
from ghcr.events import DeepSeekDone
from ghcr.review import ReviewOrchestrator
from ghcr.state import StateStore
from tests.fakes import FakeDeepSeekClient, FakeGhClient, make_config, make_pr

FIXED = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)

SCORE_HIGH = '{"confidence":95,"reason":"clear bug"}'
COV_YES = '{"verdict":{"has_tests":true,"detail":"tests added"},"findings":[]}'
COV_NO = '{"verdict":{"has_tests":false,"detail":"beta function untested"},"findings":[]}'
EMPTY = "[]"

FINDING_A = '[{"severity":"BLOCKER","file":"src/a.py","area":"a","issue":"alpha function returns wrong value","fix":"fix alpha"}]'
FINDING_B = '[{"severity":"WARNING","file":"src/b.py","area":"b","issue":"beta handler drops errors silently","fix":"fix beta"}]'


def _file_diff(path: str, lines: int, tag: str) -> str:
    body = "".join(f"+{tag} line {i}\n" for i in range(lines))
    return (
        f"diff --git a/{path} b/{path}\n"
        f"index 1..2 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +{lines} @@\n"
        f"{body}"
    )


DIFF_A = _file_diff("src/a.py", 20, "alpha")
DIFF_B = _file_diff("src/b.py", 20, "beta")
PER_FILE = len(DIFF_A.encode())


def _corr_router(system, user):
    return FINDING_A if "alpha" in user else FINDING_B


def _responses(*, corr=_corr_router, cov=COV_YES, sec=EMPTY, maint=EMPTY, score=SCORE_HIGH):
    return {
        "## LENS: correctness": corr,
        "## LENS: security": sec,
        "## LENS: maintainability": maint,
        "## LENS: test_coverage": cov,
        "## PASS: scoring": score,
    }


def _orch(tmp_path, gh, ds, **cfg_kw):
    cfg_kw.setdefault("review_mode", "multi")
    cfg_kw.setdefault("per_run_input_token_cap", 10_000_000)  # byte cap is the binding min
    cfg = make_config(db_path=str(tmp_path / "ghcr.db"), **cfg_kw)
    store = StateStore(cfg.db_path)
    return ReviewOrchestrator(gh, ds, store, cfg, now=lambda: FIXED), store


# -- the headline fix: junk-inflated PRs review normally ----------------------
def test_lockfile_inflated_pr_reviewed_not_skipped(tmp_path):
    lock = _file_diff("poetry.lock", 800, "lockjunk")  # stripped by **/*.lock
    src = _file_diff("src/a.py", 5, "alpha")
    assert len((lock + src).encode()) > 5_000  # raw diff over the cap...
    gh = FakeGhClient(diff=lock + src)
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds, max_diff_bytes=5_000)
    out = orch.review_pr(make_pr())
    assert out.action == "review"  # ...but kept_bytes is tiny → normal full review
    assert ds.calls == 5  # 4 lenses + 1 finding * 1 vote — unchunked
    assert "chunks" not in gh.posted[0][2]
    assert store.recent(1)[0]["outcome"] == "reviewed"


# -- chunked full review -------------------------------------------------------
def test_over_cap_diff_reviewed_in_two_chunks(tmp_path):
    gh = FakeGhClient(diff=DIFF_A + DIFF_B)
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds, max_diff_bytes=PER_FILE + 50)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    assert ds.calls_matching("## LENS:") == 4 * 2  # lenses × chunks
    assert ds.calls_matching("## PASS: scoring") == 2  # one finding per chunk
    assert len(gh.posted) == 1
    body = gh.posted[0][2]
    assert "reviewed in 2 chunks" in body
    assert "alpha function returns wrong value" in body  # finding from chunk 1
    assert "beta handler drops errors silently" in body  # finding from chunk 2
    row = store.recent(1)[0]
    assert row["outcome"] == "reviewed"
    assert store.already_reviewed("owner/repo", 1, "a" * 40)


def test_chunked_lens_prompts_carry_part_note_and_only_their_chunk(tmp_path):
    gh = FakeGhClient(diff=DIFF_A + DIFF_B)
    ds = FakeDeepSeekClient(responses=_responses())
    orch, _ = _orch(tmp_path, gh, ds, max_diff_bytes=PER_FILE + 50)
    orch.review_pr(make_pr())
    users = ds.user_for("## LENS: correctness")
    assert len(users) == 2
    assert sorted("part 1/2" in u for u in users) == [False, True]
    assert sorted("part 2/2" in u for u in users) == [False, True]
    for u in users:  # each lens call sees exactly one chunk's files
        assert ("alpha" in u) != ("beta" in u)


def test_chunked_scoring_bounded_to_findings_chunk(tmp_path):
    gh = FakeGhClient(diff=DIFF_A + DIFF_B)
    ds = FakeDeepSeekClient(responses=_responses())
    orch, _ = _orch(tmp_path, gh, ds, max_diff_bytes=PER_FILE + 50)
    orch.review_pr(make_pr())
    for u in ds.user_for("## PASS: scoring"):
        assert ("alpha" in u) != ("beta" in u)  # never the whole multi-chunk diff


def test_chunked_planner_runs_per_chunk(tmp_path):
    gh = FakeGhClient(diff=DIFF_A + DIFF_B)
    responses = _responses()
    responses["## PASS: context"] = '{"requests": []}'
    ds = FakeDeepSeekClient(responses=responses)
    orch, _ = _orch(tmp_path, gh, ds, max_diff_bytes=PER_FILE + 50,
                    fetch_referenced_context=True)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    assert ds.calls_matching("## PASS: context") == 2  # one planner per chunk


def test_chunked_coverage_any_failing_chunk_wins(tmp_path):
    def cov_router(system, user):
        return COV_YES if "part 1/2" in user else COV_NO

    gh = FakeGhClient(diff=DIFF_A + DIFF_B)
    ds = FakeDeepSeekClient(responses=_responses(cov=cov_router))
    orch, _ = _orch(tmp_path, gh, ds, max_diff_bytes=PER_FILE + 50)
    orch.review_pr(make_pr())
    body = gh.posted[0][2]
    assert "⚠️" in body and "beta function untested" in body


def test_max_review_chunks_overflow_lists_unreviewed_files(tmp_path):
    diff_c = _file_diff("src/c.py", 20, "gamma")
    gh = FakeGhClient(diff=DIFF_A + DIFF_B + diff_c)
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds, max_diff_bytes=PER_FILE + 50, max_review_chunks=2)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    assert ds.calls_matching("## LENS:") == 4 * 2  # only the first 2 chunks reviewed
    body = gh.posted[0][2]
    assert "reviewed in 2 chunks" in body
    assert "Not reviewed" in body and "src/c.py" in body and "max_review_chunks" in body


def test_single_file_over_budget_truncated_with_marker(tmp_path):
    huge = _file_diff("src/huge.py", 400, "omega")
    gh = FakeGhClient(diff=huge)
    ds = FakeDeepSeekClient(responses=_responses(corr=EMPTY))
    orch, _ = _orch(tmp_path, gh, ds, max_diff_bytes=1_500)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    users = ds.user_for("## LENS: correctness")
    assert len(users) == 1 and "omitted" in users[0]  # explicit truncation marker
    assert "truncated to fit: src/huge.py" in gh.posted[0][2]


# -- failure / guardrail paths --------------------------------------------------
def test_all_calls_fail_across_chunks_records_error_no_post(tmp_path):
    gh = FakeGhClient(diff=DIFF_A + DIFF_B)
    ds = FakeDeepSeekClient(raises=True)
    orch, store = _orch(tmp_path, gh, ds, max_diff_bytes=PER_FILE + 50)
    out = orch.review_pr(make_pr())
    assert out.action == "error"
    assert gh.posted == []
    assert ds.calls == 8  # 4 lenses × 2 chunks, scoring never runs
    assert not store.already_reviewed("owner/repo", 1, "a" * 40)


def test_one_lens_fails_in_one_chunk_notes_the_part(tmp_path):
    def sec(system, user):
        if "part 2/2" in user:
            raise DeepSeekError("lens down")
        return EMPTY

    gh = FakeGhClient(diff=DIFF_A + DIFF_B)
    ds = FakeDeepSeekClient(responses=_responses(sec=sec))
    orch, _ = _orch(tmp_path, gh, ds, max_diff_bytes=PER_FILE + 50)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    assert "security (part 2/2)" in gh.posted[0][2]


def test_hard_ceiling_still_skips_pathological_raw_diff(tmp_path):
    gh = FakeGhClient(diff="x" * 3_000)
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds, max_diff_bytes=1_000, hard_max_diff_bytes=2_000)
    out = orch.review_pr(make_pr())
    assert out.action == "skip_oversized"
    assert ds.calls == 0
    assert "hard ceiling" in gh.posted[0][2]


def test_chunk_reviews_off_over_cap_skips_like_before(tmp_path):
    gh = FakeGhClient(diff=DIFF_A + DIFF_B)
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds, max_diff_bytes=PER_FILE + 50, chunk_reviews=False)
    out = orch.review_pr(make_pr())
    assert out.action == "skip_oversized"
    assert ds.calls == 0
    assert store.already_reviewed("owner/repo", 1, "a" * 40)


def test_pathological_token_cap_skips_pre_spend(tmp_path):
    gh = FakeGhClient(diff=DIFF_A + DIFF_B)
    ds = FakeDeepSeekClient(responses=_responses())
    orch, _ = _orch(tmp_path, gh, ds, per_run_input_token_cap=1)
    out = orch.review_pr(make_pr())
    assert out.action == "skip_oversized"  # chunk budget <= 0, before any spend
    assert ds.calls == 0


def test_budget_gate_fires_before_any_chunk_spend(tmp_path):
    gh = FakeGhClient(diff=DIFF_A + DIFF_B)
    ds = FakeDeepSeekClient(responses=_responses())
    orch, store = _orch(tmp_path, gh, ds, max_diff_bytes=PER_FILE + 50, daily_usd_budget=5.0)
    store.record("owner/repo", 99, "z" * 40, "reviewed", cost_usd=10.0,
                 created_at=FIXED.isoformat(), model="m")
    out = orch.review_pr(make_pr())
    assert out.action == "skip_budget"
    assert ds.calls == 0


def test_chunked_review_publishes_one_aggregated_event(tmp_path):
    class _CaptureBus:
        def __init__(self):
            self.events = []

        def publish(self, evt):
            self.events.append(evt)

    bus = _CaptureBus()
    gh = FakeGhClient(diff=DIFF_A + DIFF_B)
    ds = FakeDeepSeekClient(responses=_responses())
    cfg = make_config(db_path=str(tmp_path / "ghcr.db"), review_mode="multi",
                      per_run_input_token_cap=10_000_000, max_diff_bytes=PER_FILE + 50)
    store = StateStore(cfg.db_path)
    orch = ReviewOrchestrator(gh, ds, store, cfg, now=lambda: FIXED, bus=bus)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    done = [e for e in bus.events if isinstance(e, DeepSeekDone)]
    assert len(done) == 1  # one aggregated event for the whole chunked review
    assert done[0].prompt_tokens == ds.usage.prompt_tokens * ds.calls
    assert done[0].advisor_prompt_tokens == 0  # advisor IS the worker here
