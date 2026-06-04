from datetime import datetime, timedelta, timezone

from ghcr.models import Usage
from ghcr.state import StateStore


def _db(tmp_path):
    return str(tmp_path / "ghcr.db")


def test_reviewed_is_seen(tmp_path):
    s = StateStore(_db(tmp_path))
    assert not s.already_reviewed("o/r", 1, "a" * 40)
    s.record("o/r", 1, "a" * 40, "reviewed", model="m")
    assert s.already_reviewed("o/r", 1, "a" * 40)
    # different SHA is not seen
    assert not s.already_reviewed("o/r", 1, "b" * 40)


def test_error_does_not_block_retry(tmp_path):
    s = StateStore(_db(tmp_path))
    s.record("o/r", 1, "a" * 40, "error", error="boom")
    assert not s.already_reviewed("o/r", 1, "a" * 40)


def test_oversized_and_empty_are_seen(tmp_path):
    s = StateStore(_db(tmp_path))
    s.record("o/r", 1, "a" * 40, "skip_oversized")
    s.record("o/r", 2, "c" * 40, "skip_empty")
    assert s.already_reviewed("o/r", 1, "a" * 40)
    assert s.already_reviewed("o/r", 2, "c" * 40)


def test_duplicate_reviewed_is_noop(tmp_path):
    s = StateStore(_db(tmp_path))
    s.record("o/r", 1, "a" * 40, "reviewed", cost_usd=0.5, model="m")
    s.record("o/r", 1, "a" * 40, "reviewed", cost_usd=0.5, model="m")
    rows = s.recent(10)
    assert len([r for r in rows if r["outcome"] == "reviewed"]) == 1


def test_24h_window_sum(tmp_path):
    s = StateStore(_db(tmp_path))
    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    old = (now - timedelta(hours=30)).isoformat()
    recent = (now - timedelta(hours=2)).isoformat()
    s.record("o/r", 1, "a" * 40, "reviewed", cost_usd=1.0, created_at=old)
    s.record("o/r", 2, "b" * 40, "reviewed", cost_usd=2.0, created_at=recent)
    cutoff = now - timedelta(hours=24)
    assert s.usd_spent_since(cutoff) == 2.0


def test_baseline_marks_seen(tmp_path):
    from tests.fakes import make_pr

    s = StateStore(_db(tmp_path))
    pr = make_pr(number=5, head_sha="d" * 40)
    s.baseline_seen(pr)
    assert s.already_reviewed("owner/repo", 5, "d" * 40)


def test_survives_reopen(tmp_path):
    path = _db(tmp_path)
    s = StateStore(path)
    s.record("o/r", 1, "a" * 40, "reviewed", model="m")
    s.close()
    s2 = StateStore(path)
    assert s2.already_reviewed("o/r", 1, "a" * 40)
    assert s2.has_any()


# -- @mention re-review: watermark + distinct outcome ------------------------
def test_mention_watermark_roundtrip(tmp_path):
    s = StateStore(_db(tmp_path))
    assert s.last_mention_id("o/r", 1) is None  # unseen PR
    s.set_mention_id("o/r", 1, 42)
    assert s.last_mention_id("o/r", 1) == 42
    s.set_mention_id("o/r", 1, 99)  # upsert advances
    assert s.last_mention_id("o/r", 1) == 99


def test_mention_watermark_survives_reopen(tmp_path):
    path = _db(tmp_path)
    s = StateStore(path)
    s.set_mention_id("o/r", 3, 7)
    s.close()
    assert StateStore(path).last_mention_id("o/r", 3) == 7


def test_rereviewed_coexists_with_reviewed_same_sha(tmp_path):
    s = StateStore(_db(tmp_path))
    s.record("o/r", 1, "a" * 40, "reviewed", cost_usd=1.0, model="m")
    s.record("o/r", 1, "a" * 40, "rereviewed", cost_usd=2.0, model="m")
    outcomes = sorted(r["outcome"] for r in s.recent(10))
    assert outcomes == ["rereviewed", "reviewed"]  # both rows kept (no ON CONFLICT drop)
    assert s.already_reviewed("o/r", 1, "a" * 40)  # 'reviewed' still marks SHA seen


def test_rereviewed_not_in_seen_set(tmp_path):
    s = StateStore(_db(tmp_path))
    s.record("o/r", 2, "b" * 40, "rereviewed", cost_usd=1.0, model="m")
    # a lone rereview must NOT mark the SHA seen (only head-SHA 'reviewed' does)
    assert not s.already_reviewed("o/r", 2, "b" * 40)


def test_rereviewed_counts_toward_spend(tmp_path):
    s = StateStore(_db(tmp_path))
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    s.record("o/r", 1, "a" * 40, "reviewed", cost_usd=1.0, created_at=now.isoformat())
    s.record("o/r", 1, "a" * 40, "rereviewed", cost_usd=2.0, created_at=now.isoformat())
    assert s.usd_spent_since(now - timedelta(hours=24)) == 3.0
