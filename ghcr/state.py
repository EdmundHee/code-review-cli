"""SQLite-backed dedupe + audit/cost log.

Dedupe rule: one *terminal* outcome per unique (repo, pr_number, head_sha).
Terminal = reviewed / skip_oversized / skip_budget / skip_empty / skip_baseline.
Transient skips (seen/draft/author) are never written here — they are logged by
the caller — so the table cannot grow a row per open PR per cycle.
``error`` rows are written but are NOT in the "seen" set, so a transient failure
retries on the next cycle.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

from .cost import Prices, estimate_cost_usd
from .models import PullRequest, Usage

# Outcomes that mean "this SHA is handled; do not review again".
SEEN_OUTCOMES = ("reviewed", "skip_oversized", "skip_budget", "skip_empty", "skip_baseline")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo TEXT NOT NULL,
  pr_number INTEGER NOT NULL,
  head_sha TEXT NOT NULL,
  outcome TEXT NOT NULL,
  comment_url TEXT,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  model TEXT,
  error TEXT,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_unique
  ON reviews(repo, pr_number, head_sha) WHERE outcome='reviewed';
CREATE INDEX IF NOT EXISTS idx_reviews_lookup ON reviews(repo, pr_number, head_sha);
CREATE INDEX IF NOT EXISTS idx_reviews_created ON reviews(created_at);
CREATE TABLE IF NOT EXISTS comment_triggers (
  repo TEXT NOT NULL,
  pr_number INTEGER NOT NULL,
  last_comment_id INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (repo, pr_number)
);
CREATE TABLE IF NOT EXISTS schema_meta (k TEXT PRIMARY KEY, v TEXT);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StateStore:
    def __init__(self, db_path: str):
        self.db_path = os.path.expanduser(db_path)
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_meta(k, v) VALUES('version', '1')"
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def has_any(self) -> bool:
        return self.conn.execute("SELECT 1 FROM reviews LIMIT 1").fetchone() is not None

    def already_reviewed(self, repo: str, number: int, head_sha: str) -> bool:
        placeholders = ",".join("?" for _ in SEEN_OUTCOMES)
        row = self.conn.execute(
            f"SELECT 1 FROM reviews WHERE repo=? AND pr_number=? AND head_sha=? "
            f"AND outcome IN ({placeholders}) LIMIT 1",
            (repo, number, head_sha, *SEEN_OUTCOMES),
        ).fetchone()
        return row is not None

    def record(
        self,
        repo: str,
        number: int,
        head_sha: str,
        outcome: str,
        *,
        comment_url: str | None = None,
        usage: Usage | None = None,
        cost_usd: float = 0.0,
        model: str | None = None,
        error: str | None = None,
        created_at: str | None = None,
    ) -> None:
        u = usage or Usage()
        ts = created_at or _utcnow().isoformat()
        self.conn.execute(
            "INSERT INTO reviews(repo, pr_number, head_sha, outcome, comment_url, "
            "prompt_tokens, completion_tokens, cost_usd, model, error, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(repo, pr_number, head_sha) WHERE outcome='reviewed' DO NOTHING",
            (
                repo,
                number,
                head_sha,
                outcome,
                comment_url,
                u.prompt_tokens,
                u.completion_tokens,
                cost_usd,
                model,
                error,
                ts,
            ),
        )
        self.conn.commit()

    def baseline_seen(self, pr: PullRequest, created_at: str | None = None) -> None:
        self.record(pr.repo, pr.number, pr.head_sha, "skip_baseline", created_at=created_at)

    # -- @mention re-review watermark ------------------------------------
    def last_mention_id(self, repo: str, number: int) -> int | None:
        """Highest handled @mention comment id for a PR, or None if never scanned."""
        row = self.conn.execute(
            "SELECT last_comment_id FROM comment_triggers WHERE repo=? AND pr_number=?",
            (repo, number),
        ).fetchone()
        return int(row["last_comment_id"]) if row is not None else None

    def set_mention_id(self, repo: str, number: int, comment_id: int, created_at: str | None = None) -> None:
        ts = created_at or _utcnow().isoformat()
        self.conn.execute(
            "INSERT INTO comment_triggers(repo, pr_number, last_comment_id, updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(repo, pr_number) DO UPDATE SET "
            "last_comment_id=excluded.last_comment_id, updated_at=excluded.updated_at",
            (repo, number, int(comment_id), ts),
        )
        self.conn.commit()

    def usd_spent_since(self, cutoff: datetime) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM reviews WHERE created_at >= ?",
            (cutoff.isoformat(),),
        ).fetchone()
        return float(row["s"] or 0.0)

    def recent(self, limit: int = 20):
        return self.conn.execute(
            "SELECT * FROM reviews ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
