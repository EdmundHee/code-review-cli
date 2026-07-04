from datetime import datetime, timezone

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

LOCK_ONLY = """\
diff --git a/poetry.lock b/poetry.lock
index a..b 100644
--- a/poetry.lock
+++ b/poetry.lock
@@ -1 +1 @@
-x
+y
"""


def _orch(tmp_path, gh, ds, **cfg_kw):
    cfg = make_config(db_path=str(tmp_path / "ghcr.db"), **cfg_kw)
    store = StateStore(cfg.db_path)
    orch = ReviewOrchestrator(gh, ds, store, cfg, now=lambda: FIXED)
    return orch, store


def test_happy_path_posts_and_records_usage(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF)
    ds = FakeDeepSeekClient()
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    assert ds.calls == 1
    assert len(gh.posted) == 1
    body = gh.posted[0][2]
    assert "a" * 40 in body  # marker SHA present
    row = store.recent(1)[0]
    assert row["outcome"] == "reviewed"
    assert row["prompt_tokens"] == 1000 and row["completion_tokens"] == 500
    assert row["cost_usd"] > 0
    assert store.already_reviewed("owner/repo", 1, "a" * 40)


def test_skip_author_no_call_no_diff_no_row(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF)
    ds = FakeDeepSeekClient()
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr(author="dependabot[bot]"))
    assert out.action == "skip_author"
    assert ds.calls == 0 and gh.diff_calls == 0
    assert not store.has_any()


def test_skip_own_pr(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF)
    ds = FakeDeepSeekClient()
    orch, store = _orch(tmp_path, gh, ds, bot_login="reviewbot")
    out = orch.review_pr(make_pr(author="reviewbot"))
    assert out.action == "skip_author"
    assert ds.calls == 0


def test_skip_draft(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF)
    ds = FakeDeepSeekClient()
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr(is_draft=True))
    assert out.action == "skip_draft"
    assert ds.calls == 0 and not store.has_any()


def test_second_review_same_sha_is_skipped(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF)
    ds = FakeDeepSeekClient()
    orch, store = _orch(tmp_path, gh, ds)
    orch.review_pr(make_pr())
    ds.calls = 0
    gh.diff_calls = 0
    out = orch.review_pr(make_pr())  # same SHA
    assert out.action == "skip_seen"
    assert ds.calls == 0 and gh.diff_calls == 0


def test_empty_after_filter_skips_and_records(tmp_path):
    gh = FakeGhClient(diff=LOCK_ONLY)
    ds = FakeDeepSeekClient()
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "skip_empty"
    assert ds.calls == 0
    assert gh.diff_calls == 1  # it did fetch
    assert store.already_reviewed("owner/repo", 1, "a" * 40)


def test_oversized_byte_cap_posts_notice(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF)
    ds = FakeDeepSeekClient()
    orch, store = _orch(tmp_path, gh, ds, max_diff_bytes=10)
    out = orch.review_pr(make_pr())
    assert out.action == "skip_oversized"
    assert ds.calls == 0
    assert len(gh.posted) == 1 and "too large" in gh.posted[0][2].lower()
    assert store.already_reviewed("owner/repo", 1, "a" * 40)


def test_byte_cap_measured_post_filter_lockfile_junk_ignored(tmp_path):
    # A PR that is mostly lockfile churn plus a little real code must be reviewed:
    # the cap applies to what SURVIVES filtering, not the raw diff.
    lock_lines = "".join(f"+lockjunk {i}\n" for i in range(200))
    big_lock = (
        "diff --git a/poetry.lock b/poetry.lock\n"
        "index a..b 100644\n"
        "--- a/poetry.lock\n"
        "+++ b/poetry.lock\n"
        "@@ -0,0 +200 @@\n"
        f"{lock_lines}"
    )
    gh = FakeGhClient(diff=big_lock + SRC_DIFF)
    ds = FakeDeepSeekClient()
    orch, store = _orch(tmp_path, gh, ds, max_diff_bytes=1_000)  # raw >> cap, kept << cap
    out = orch.review_pr(make_pr())
    assert out.action == "review"
    assert ds.calls == 1
    assert store.recent(1)[0]["outcome"] == "reviewed"


def test_oversized_token_cap(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF)
    ds = FakeDeepSeekClient()
    orch, store = _orch(tmp_path, gh, ds, per_run_input_token_cap=1)
    out = orch.review_pr(make_pr())
    assert out.action == "skip_oversized"
    assert ds.calls == 0


def test_oversized_skip_behavior_no_comment(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF)
    ds = FakeDeepSeekClient()
    orch, store = _orch(tmp_path, gh, ds, max_diff_bytes=10, oversized_behavior="skip")
    out = orch.review_pr(make_pr())
    assert out.action == "skip_oversized"
    assert gh.posted == []
    assert store.already_reviewed("owner/repo", 1, "a" * 40)


def test_budget_exceeded_no_call(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF)
    ds = FakeDeepSeekClient()
    orch, store = _orch(tmp_path, gh, ds, daily_usd_budget=5.0)
    store.record(
        "owner/repo", 99, "z" * 40, "reviewed", cost_usd=10.0,
        created_at=FIXED.isoformat(), model="m",
    )
    out = orch.review_pr(make_pr())
    assert out.action == "skip_budget"
    assert ds.calls == 0
    assert len(gh.posted) == 1  # budget notice


def test_failed_post_records_error_not_reviewed(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF, post_raises=True)
    ds = FakeDeepSeekClient()
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "error"
    assert ds.calls == 1  # we paid for the review
    # critical: a failed post must NOT be recorded as reviewed → retried next cycle
    assert not store.already_reviewed("owner/repo", 1, "a" * 40)
    assert store.recent(1)[0]["outcome"] == "error"


def test_deepseek_error_records_error(tmp_path):
    gh = FakeGhClient(diff=SRC_DIFF)
    ds = FakeDeepSeekClient(raises=True)
    orch, store = _orch(tmp_path, gh, ds)
    out = orch.review_pr(make_pr())
    assert out.action == "error"
    assert gh.posted == []
    assert not store.already_reviewed("owner/repo", 1, "a" * 40)
