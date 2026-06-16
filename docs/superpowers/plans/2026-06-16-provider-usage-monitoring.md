# Provider-Aware Usage Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the live TUI provider-aware — label which model ran each sub-agent, and track per-provider (DeepSeek worker vs Claude Opus advisor) token usage, persisted in SQLite so 24h totals survive restart.

**Architecture:** Two new SQLite columns + an idempotent migration hold the advisor token split; `record()` and a new `tokens_spent_since` query read/write them. The orchestrator publishes the split it already computes (`worker_usages`/`advisor_usages`) onto the existing `DeepSeekDone` event and tags `AgentEvent` with the running model. The TUI seeds 24h per-provider tokens from the DB and accumulates the session live. Off-path (DeepSeek-only) renders identically to today.

**Tech Stack:** Python 3, sqlite3, frozen dataclasses, Rich TUI, pytest. Spec: `docs/superpowers/specs/2026-06-16-provider-usage-monitoring-design.md`.

---

## File Structure

- **Modify** `ghcr/models.py` — add `ProviderTokens` frozen dataclass (24h token totals).
- **Modify** `ghcr/state.py` — 2 new `reviews` columns, idempotent migration, `record(advisor_usage=…)`, `tokens_spent_since()`.
- **Modify** `ghcr/events.py` — `DeepSeekDone` advisor token fields; `AgentEvent.model`.
- **Modify** `ghcr/review.py` — `_emit_agent` tags model; `DeepSeekDone` + `record` use the worker/advisor split.
- **Modify** `ghcr/tui.py` — session counters, seed 24h tokens, reduce accumulation, header shows both models + per-provider tokens.
- **Modify** `ghcr/cli.py` — pass `tokens_spent_since` into `state.seed` (only if seed happens in cli; it is in tui — see Task 5).
- **Modify** `CLAUDE.md` — document per-provider token tracking + schema v2.

Run tests bypassing the rtk proxy (it hides tracebacks):
`.venv/bin/python -m pytest -o addopts="" -q < /dev/null`

---

## Task 1: SQLite persistence — columns, migration, record, query

**Files:**
- Modify: `ghcr/models.py` (add `ProviderTokens` near `Usage`, ~line 52)
- Modify: `ghcr/state.py` (`_SCHEMA` 24-37; `__init__` 57-71; `record` 88-123; after `usd_spent_since` 147-)
- Test: `tests/test_state.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_state.py` (read the file first for its existing `StateStore` construction + `Usage` import style):

```python
from datetime import datetime, timedelta, timezone
from ghcr.models import Usage, ProviderTokens
from ghcr.state import StateStore


def _now():
    return datetime.now(timezone.utc)


def test_record_persists_per_provider_tokens(tmp_path):
    s = StateStore(str(tmp_path / "s.db"))
    s.record("o/r", 1, "a" * 40, "reviewed",
             usage=Usage(prompt_tokens=100, completion_tokens=40),
             advisor_usage=Usage(prompt_tokens=10, completion_tokens=5), cost_usd=0.1)
    tok = s.tokens_spent_since(_now() - timedelta(hours=24))
    assert tok == ProviderTokens(worker_prompt=100, worker_completion=40,
                                 advisor_prompt=10, advisor_completion=5)


def test_advisor_usage_defaults_to_zero(tmp_path):
    s = StateStore(str(tmp_path / "s.db"))
    s.record("o/r", 2, "b" * 40, "reviewed", usage=Usage(prompt_tokens=7, completion_tokens=3), cost_usd=0.0)
    tok = s.tokens_spent_since(_now() - timedelta(hours=24))
    assert tok == ProviderTokens(worker_prompt=7, worker_completion=3, advisor_prompt=0, advisor_completion=0)


def test_tokens_spent_since_excludes_old_rows(tmp_path):
    s = StateStore(str(tmp_path / "s.db"))
    old = (_now() - timedelta(days=2)).isoformat()
    s.record("o/r", 3, "c" * 40, "reviewed", usage=Usage(prompt_tokens=1000, completion_tokens=1000),
             cost_usd=0.0, created_at=old)
    tok = s.tokens_spent_since(_now() - timedelta(hours=24))
    assert tok == ProviderTokens(0, 0, 0, 0)


def test_migration_adds_columns_to_old_db(tmp_path):
    # Simulate a pre-feature DB: create reviews WITHOUT the advisor columns, with one row.
    import sqlite3
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT, pr_number INTEGER, "
        "head_sha TEXT, outcome TEXT, comment_url TEXT, prompt_tokens INTEGER DEFAULT 0, "
        "completion_tokens INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0, model TEXT, error TEXT, created_at TEXT);"
        "CREATE TABLE schema_meta (k TEXT PRIMARY KEY, v TEXT);"
    )
    conn.execute("INSERT INTO reviews(repo, pr_number, head_sha, outcome, prompt_tokens, completion_tokens, created_at) "
                 "VALUES('o/r', 9, 'd', 'reviewed', 50, 20, ?)", (_now().isoformat(),))
    conn.commit(); conn.close()
    # Re-open via StateStore → migration must add the columns and keep the old row readable.
    s = StateStore(db)
    cols = {row["name"] for row in s.conn.execute("PRAGMA table_info(reviews)")}
    assert "advisor_prompt_tokens" in cols and "advisor_completion_tokens" in cols
    tok = s.tokens_spent_since(_now() - timedelta(hours=24))
    assert tok.worker_prompt == 50 and tok.worker_completion == 20 and tok.advisor_prompt == 0
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.venv/bin/python -m pytest -o addopts="" tests/test_state.py -k "provider or advisor or migration or tokens_spent" -q < /dev/null`
Expected: FAIL (`ImportError: ProviderTokens` / `record() got an unexpected keyword argument 'advisor_usage'`).

- [ ] **Step 3: Add `ProviderTokens` to `models.py`**

After the `Usage` dataclass (ghcr/models.py ~line 52):

```python
@dataclass(frozen=True)
class ProviderTokens:
    """Summed token counts split by provider (worker = DeepSeek, advisor = Claude),
    e.g. the 24h totals read back from the store for the TUI."""

    worker_prompt: int = 0
    worker_completion: int = 0
    advisor_prompt: int = 0
    advisor_completion: int = 0
```

- [ ] **Step 4: Add the two columns to `_SCHEMA`**

In `ghcr/state.py` `_SCHEMA`, between `completion_tokens` and `cost_usd` (lines 32-33):

```sql
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  advisor_prompt_tokens INTEGER NOT NULL DEFAULT 0,
  advisor_completion_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
```

- [ ] **Step 5: Add the idempotent migration**

In `StateStore.__init__`, after the `INSERT OR IGNORE ... version '1'` line (state.py:68-70) and before `self.conn.commit()`:

```python
        self._migrate()
```

Add the method (after `__init__`):

```python
    def _migrate(self) -> None:
        """Idempotent, column-guarded migration. Safe to run every startup.
        Adds the per-provider advisor token columns to pre-feature DBs (fresh DBs
        already have them from _SCHEMA, so the guard skips the ALTERs)."""
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(reviews)")}
        if "advisor_prompt_tokens" not in cols:
            self.conn.execute("ALTER TABLE reviews ADD COLUMN advisor_prompt_tokens INTEGER NOT NULL DEFAULT 0")
        if "advisor_completion_tokens" not in cols:
            self.conn.execute("ALTER TABLE reviews ADD COLUMN advisor_completion_tokens INTEGER NOT NULL DEFAULT 0")
        self.conn.execute(
            "INSERT INTO schema_meta(k, v) VALUES('version', '2') "
            "ON CONFLICT(k) DO UPDATE SET v='2'"
        )
```

- [ ] **Step 6: Extend `record()`**

Change the signature (state.py:88-101) to add `advisor_usage`:

```python
    def record(
        self,
        repo: str,
        number: int,
        head_sha: str,
        outcome: str,
        *,
        comment_url: str | None = None,
        usage: Usage | None = None,
        advisor_usage: Usage | None = None,
        cost_usd: float = 0.0,
        model: str | None = None,
        error: str | None = None,
        created_at: str | None = None,
    ) -> None:
        u = usage or Usage()
        au = advisor_usage or Usage()
        ts = created_at or _utcnow().isoformat()
        self.conn.execute(
            "INSERT INTO reviews(repo, pr_number, head_sha, outcome, comment_url, "
            "prompt_tokens, completion_tokens, advisor_prompt_tokens, advisor_completion_tokens, "
            "cost_usd, model, error, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(repo, pr_number, head_sha) WHERE outcome='reviewed' DO NOTHING",
            (
                repo, number, head_sha, outcome, comment_url,
                u.prompt_tokens, u.completion_tokens,
                au.prompt_tokens, au.completion_tokens,
                cost_usd, model, error, ts,
            ),
        )
        self.conn.commit()
```

- [ ] **Step 7: Add `tokens_spent_since`**

After `usd_spent_since` (state.py ~155). **First read `usd_spent_since` (lines 147-153) to copy its exact `cutoff` parameter handling** (it passes the datetime/isoformat a certain way — mirror it precisely):

```python
    def tokens_spent_since(self, cutoff: datetime) -> ProviderTokens:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens),0) AS wp, COALESCE(SUM(completion_tokens),0) AS wc, "
            "COALESCE(SUM(advisor_prompt_tokens),0) AS ap, COALESCE(SUM(advisor_completion_tokens),0) AS ac "
            "FROM reviews WHERE created_at >= ?",
            (cutoff.isoformat(),),  # ← match usd_spent_since's actual cutoff handling
        ).fetchone()
        return ProviderTokens(
            worker_prompt=int(row["wp"]), worker_completion=int(row["wc"]),
            advisor_prompt=int(row["ap"]), advisor_completion=int(row["ac"]),
        )
```

Add `ProviderTokens` to the `from .models import ...` line at the top of state.py.

- [ ] **Step 8: Run tests, verify pass**

Run: `.venv/bin/python -m pytest -o addopts="" tests/test_state.py -q < /dev/null`
Expected: PASS. Then full suite: `.venv/bin/python -m pytest -o addopts="" -q < /dev/null` → all PASS (existing `record(...)` calls unaffected — `advisor_usage` defaults None).

- [ ] **Step 9: Commit**

```bash
git add ghcr/models.py ghcr/state.py tests/test_state.py
git commit -m "feat(state): persist per-provider token split + tokens_spent_since (schema v2)"
```

---

## Task 2: Events + orchestrator producer wiring

**Files:**
- Modify: `ghcr/events.py` (`DeepSeekDone` 40-48; `AgentEvent` 51-64)
- Modify: `ghcr/review.py` (`_emit_agent` 453-459; `DeepSeekDone` publish 402-407; success `record` 426-429; all-lenses-failed `record` 367-374; post-fail `record` 422)
- Test: `tests/test_review_hybrid.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_review_hybrid.py` (reuse its existing `_orch_hybrid` helper + fakes; read the file first). Capture published events via a fake bus that records them — check whether the file/tests already use a bus capture; if not, use this minimal one:

```python
class _CaptureBus:
    def __init__(self):
        self.events = []
    def publish(self, evt):
        self.events.append(evt)


def test_deepseekdone_carries_provider_split(tmp_path):
    from ghcr.events import DeepSeekDone, AgentEvent
    ds = FakeDeepSeekClient(responses={"## LENS:": '{"findings":[{"severity":"WARNING","file":"app.py","issue":"x","fix":"y"}]}'})
    advisor = FakeClaudeCliClient(responses={
        "## PASS: context": '{"requests":[]}',
        "## PASS: scoring": '{"confidence": 90, "reason": "ok"}',
    })
    bus = _CaptureBus()
    # build orchestrator like _orch_hybrid but pass bus=bus; reuse its gh/store/cfg setup.
    orch, gh, cfg = _orch_hybrid(tmp_path, ds, advisor, bus=bus)
    orch.review_pr(make_pr())
    done = [e for e in bus.events if isinstance(e, DeepSeekDone)][0]
    assert done.prompt_tokens == ds.usage.prompt_tokens * ds.calls_matching("## LENS:")
    assert done.advisor_prompt_tokens > 0  # planner + scoring ran on the advisor
    # agent events tagged with the model that ran them:
    agents = [e for e in bus.events if isinstance(e, AgentEvent)]
    assert any(a.agent.startswith("score") and a.model == "opus" for a in agents)
    assert any(a.agent.startswith("lens:") and a.model == "deepseek-v4-pro" for a in agents)


def test_record_persists_split(tmp_path):
    ds = FakeDeepSeekClient(responses={"## LENS:": '{"findings":[{"severity":"WARNING","file":"app.py","issue":"x","fix":"y"}]}'})
    advisor = FakeClaudeCliClient(responses={
        "## PASS: context": '{"requests":[]}',
        "## PASS: scoring": '{"confidence": 90, "reason": "ok"}',
    })
    orch, gh, cfg = _orch_hybrid(tmp_path, ds, advisor)
    orch.review_pr(make_pr())
    from datetime import datetime, timedelta, timezone
    tok = orch.store.tokens_spent_since(datetime.now(timezone.utc) - timedelta(hours=24))
    assert tok.advisor_prompt > 0 and tok.worker_prompt > 0
```

> Adjust `_orch_hybrid` to accept and thread a `bus=` kwarg and to `return orch, gh, cfg` (it already returns cfg per the prior task). If it doesn't accept `bus`, add the parameter and pass it to `ReviewOrchestrator(..., bus=bus)`.

- [ ] **Step 2: Run tests, verify they fail**

Run: `.venv/bin/python -m pytest -o addopts="" tests/test_review_hybrid.py -k "provider_split or persists_split" -q < /dev/null`
Expected: FAIL (`AttributeError: 'DeepSeekDone' object has no attribute 'advisor_prompt_tokens'` / `AgentEvent ... 'model'`).

- [ ] **Step 3: Add event fields**

In `ghcr/events.py`, `DeepSeekDone` (after `completion_tokens`, line 45):

```python
@dataclass(frozen=True)
class DeepSeekDone:
    repo: str
    pr_number: int
    prompt_tokens: int                 # worker (DeepSeek) portion
    completion_tokens: int
    latency_s: float
    snippet: str
    title: str = ""
    advisor_prompt_tokens: int = 0     # advisor (Claude) portion; 0 when advisor==worker
    advisor_completion_tokens: int = 0
```

`AgentEvent` (add field, line 64):

```python
    title: str = ""
    model: str = ""  # the model that ran this sub-agent (worker or advisor)
```

- [ ] **Step 4: Tag the model in `_emit_agent`**

Replace `_emit_agent` (review.py:453-459):

```python
    def _emit_agent(self, pr, agent: str, status: str, detail: str = "") -> None:
        """Publish a sub-agent state change for the TUI. Safe from pool threads —
        the bus is thread-safe; no store access here. The model is derived from the
        agent kind: scorers (``score:*``) and the planner (``context``) run on the
        advisor; lenses run on the worker."""
        if self.bus:
            model = self.advisor.model if (agent.startswith("score") or agent == "context") else self.deepseek.model
            self.bus.publish(AgentEvent(
                repo=pr.repo, pr_number=pr.number, agent=agent, status=status,
                detail=detail, title=pr.title, model=model,
            ))
```

(Both `DeepSeekClient` and `ClaudeCliClient` expose `.model`.)

- [ ] **Step 5: Publish + record the split**

In the multi success path, locate where `usage = merge_usages(worker_usages + advisor_usages)` and `cost = self._split_cost(...)` are computed (review.py ~395-396). Replace the single `usage` with two merges:

```python
        worker_u = merge_usages(worker_usages)
        advisor_u = merge_usages(advisor_usages)
        cost = self._split_cost(worker_usages, advisor_usages)
```

Update the `DeepSeekDone` publish (review.py:402-407):

```python
        if self.bus:
            self.bus.publish(DeepSeekDone(
                repo=pr.repo, pr_number=pr.number,
                prompt_tokens=worker_u.prompt_tokens, completion_tokens=worker_u.completion_tokens,
                advisor_prompt_tokens=advisor_u.prompt_tokens, advisor_completion_tokens=advisor_u.completion_tokens,
                latency_s=latency_s, snippet=content[:280], title=pr.title,
            ))
```

Update the success `record` (review.py:426-429) and the post-fail `record` (review.py:422) to pass the split:

```python
        self.store.record(
            pr.repo, pr.number, pr.head_sha, record_outcome,
            comment_url=url, usage=worker_u, advisor_usage=advisor_u, cost_usd=cost, model=model,
        )
```
```python
            self.store.record(pr.repo, pr.number, pr.head_sha, ACTION_ERROR, model=model,
                              usage=worker_u, advisor_usage=advisor_u, cost_usd=cost, error=str(e))
```

In the all-lenses-failed branch (review.py:367-374), replace its `usage = merge_usages(worker_usages + advisor_usages)` with the split and pass it:

```python
        if len(lens_errors) == len(lenses):
            worker_u = merge_usages(worker_usages)
            advisor_u = merge_usages(advisor_usages)
            cost = self._split_cost(worker_usages, advisor_usages)
            if not dry_run:
                self.store.record(
                    pr.repo, pr.number, pr.head_sha, ACTION_ERROR,
                    model=model, usage=worker_u, advisor_usage=advisor_u,
                    cost_usd=cost, error="all review lenses failed",
                )
            log.error("all lenses failed repo=%s pr=%s", pr.repo, pr.number)
            return ReviewOutcome(ACTION_ERROR, cost_usd=cost)
```

Grep the multi branch for any remaining bare `usage` name and remove/replace it. Single-mode + skip `record` calls keep their existing `usage=` (worker; advisor defaults to 0).

- [ ] **Step 6: Run tests, verify pass**

Run: `.venv/bin/python -m pytest -o addopts="" tests/test_review_hybrid.py -q < /dev/null` → PASS.
Run: `.venv/bin/python -m pytest -o addopts="" -q < /dev/null` → all PASS (default path: advisor_usages empty → advisor_u zero → DeepSeekDone advisor fields 0, record advisor cols 0; existing DeepSeekDone token assertions still hold because worker_u == full review when no advisor).

> If any existing test asserted `DeepSeekDone.prompt_tokens` equals the *merged* total on a path that now has advisor usage, update it to worker-only (that is the intended new meaning). Default-path tests are unaffected.

- [ ] **Step 7: Commit**

```bash
git add ghcr/events.py ghcr/review.py tests/test_review_hybrid.py
git commit -m "feat(review): publish+persist worker/advisor token split, tag AgentEvent model"
```

---

## Task 3: TUI — session counters, 24h seed, reduce, render

**Files:**
- Modify: `ghcr/tui.py` (`DashboardState.__init__` 91-108; `seed` 111-122; `reduce` DeepSeekDone 162-167; `_header` 192-203; seed call 378)
- Test: `tests/test_tui.py` (or wherever TUI reduce/seed tests live — check; if none, create `tests/test_tui.py`)

- [ ] **Step 1: Write failing tests**

Read existing TUI tests first for the construction pattern. Then add (create `tests/test_tui.py` if absent):

```python
from datetime import datetime, timezone
from ghcr.tui import DashboardState
from ghcr.events import DeepSeekDone
from ghcr.models import ProviderTokens
from tests.fakes import make_config


def _state(**kw):
    return DashboardState(make_config(db_path=":memory:", **kw))


def test_seed_sets_24h_provider_tokens():
    st = _state(advisor_provider="claude")
    st.seed([], spent_24h=0.0, tokens_24h=ProviderTokens(100, 40, 10, 5))
    assert st.tok24_wp == 100 and st.tok24_ap == 10


def test_deepseekdone_accumulates_provider_tokens():
    st = _state(advisor_provider="claude")
    st.seed([], spent_24h=0.0, tokens_24h=ProviderTokens(0, 0, 0, 0))
    st.apply(DeepSeekDone(repo="o/r", pr_number=1, prompt_tokens=50, completion_tokens=20,
                          latency_s=1.0, snippet="x", advisor_prompt_tokens=8, advisor_completion_tokens=3))
    assert st.tok24_wp == 50 and st.tok24_wc == 20
    assert st.tok24_ap == 8 and st.tok24_ac == 3
    assert st.sess_wp == 50 and st.sess_ap == 8


def test_advisor_model_off_path_equals_worker():
    st = _state()  # advisor_provider defaults to deepseek
    assert st.advisor_model == st.model
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.venv/bin/python -m pytest -o addopts="" tests/test_tui.py -q < /dev/null`
Expected: FAIL (`seed() got an unexpected keyword argument 'tokens_24h'` / `AttributeError: tok24_wp`).

- [ ] **Step 3: Add state fields**

In `DashboardState.__init__` (after `self.spent_24h = 0.0`, line 105):

```python
        # Per-provider token tracking. 24h totals (seeded from DB, kept live) and
        # session totals (since TUI start). wp/wc = worker (DeepSeek), ap/ac = advisor (Claude).
        self.tok24_wp = self.tok24_wc = self.tok24_ap = self.tok24_ac = 0
        self.sess_wp = self.sess_wc = self.sess_ap = self.sess_ac = 0
        self.advisor_model = (
            cfg.claude.model
            if cfg.review.advisor_provider == "claude" and cfg.claude is not None
            else cfg.deepseek.model
        )
```

- [ ] **Step 4: Seed 24h tokens**

Change `seed` (tui.py:111):

```python
    def seed(self, rows, spent_24h: float, tokens_24h=None) -> None:
        self.spent_24h = spent_24h
        if tokens_24h is not None:
            self.tok24_wp, self.tok24_wc = tokens_24h.worker_prompt, tokens_24h.worker_completion
            self.tok24_ap, self.tok24_ac = tokens_24h.advisor_prompt, tokens_24h.advisor_completion
        for row in reversed(list(rows)):  # unchanged below
```

(keep the rest of `seed` as-is.)

- [ ] **Step 5: Accumulate in `reduce` on `DeepSeekDone`**

Replace the `DeepSeekDone` branch (tui.py:162-167):

```python
    elif isinstance(evt, DeepSeekDone):
        state.latest = evt
        state.tok24_wp += evt.prompt_tokens
        state.tok24_wc += evt.completion_tokens
        state.tok24_ap += evt.advisor_prompt_tokens
        state.tok24_ac += evt.advisor_completion_tokens
        state.sess_wp += evt.prompt_tokens
        state.sess_wc += evt.completion_tokens
        state.sess_ap += evt.advisor_prompt_tokens
        state.sess_ac += evt.advisor_completion_tokens
        adv = f" · opus {evt.advisor_prompt_tokens}→{evt.advisor_completion_tokens}" if evt.advisor_prompt_tokens else ""
        state.events.append(
            f"{_hhmmss(_utcnow())} deepseek {evt.repo}#{evt.pr_number} "
            f"{evt.prompt_tokens}→{evt.completion_tokens} tok{adv} ({evt.latency_s:.1f}s)"
        )
```

- [ ] **Step 6: Add a token formatter + render both models + per-provider tokens in `_header`**

Add a helper near the other `_fmt_*` functions in tui.py:

```python
def _fmt_tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)
```

Replace `_header` (tui.py:192-203):

```python
def _header(state: DashboardState) -> Panel:
    up = _fmt_uptime((_utcnow() - state.started_at).total_seconds())
    spent = state.spent_24h
    color = "red" if spent >= state.budget else "green"
    hybrid = state.advisor_model != state.model
    models = (f"{state.model} + {state.advisor_model}" if hybrid else state.model)
    tok = f"ds {_fmt_tok(state.tok24_wp)}↑/{_fmt_tok(state.tok24_wc)}↓"
    if hybrid:
        tok += f" · opus {_fmt_tok(state.tok24_ap)}↑/{_fmt_tok(state.tok24_ac)}↓"
    text = Text.assemble(
        ("ghcr", "bold cyan"), " · ",
        (state.bot_login, "bold"), " · ",
        (models, "magenta"), " · ",
        f"up {up}", " · ",
        ("24h ", "dim"), (f"${spent:.3f}/${state.budget:.2f}", color), " · ",
        ("tok ", "dim"), (tok, "cyan"),
    )
    return Panel(text, border_style="cyan")
```

- [ ] **Step 7: Pass `tokens_spent_since` into the seed call**

At the `state.seed(...)` call (tui.py:378):

```python
    state.seed(seed_store.recent(20), seed_store.usd_spent_since(cutoff), seed_store.tokens_spent_since(cutoff))
```

- [ ] **Step 8: Run tests, verify pass**

Run: `.venv/bin/python -m pytest -o addopts="" tests/test_tui.py -q < /dev/null` → PASS.
Run: `.venv/bin/python -m pytest -o addopts="" -q < /dev/null` → all PASS.

- [ ] **Step 9: Commit**

```bash
git add ghcr/tui.py tests/test_tui.py
git commit -m "feat(tui): per-provider token totals + both models in header"
```

---

## Task 4: Docs

**Files:**
- Modify: `CLAUDE.md` (Review pipeline / advisor section)

- [ ] **Step 1: Add a note to `CLAUDE.md`**

In the advisor-provider paragraph (added in the prior feature), append:

```markdown
**Per-provider usage in the TUI.** Token usage is tracked split by provider — worker
(DeepSeek, lenses) vs advisor (Claude, planner+scoring). `DeepSeekDone` carries both
(`prompt_tokens`/`completion_tokens` = worker; `advisor_*` = advisor), `AgentEvent.model`
tags which model ran each sub-agent, and the split is **persisted** on the `reviews`
row (`advisor_prompt_tokens`/`advisor_completion_tokens`, schema v2 via an idempotent
`ALTER`-based migration). The TUI seeds 24h per-provider totals from `tokens_spent_since`
and shows both models + `ds …↑/…↓ · opus …↑/…↓`. Off-path (DeepSeek-only) the advisor
columns/fields are 0 and the display collapses to a single model — unchanged.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document per-provider token tracking + schema v2"
```

---

## Final verification

- [ ] Full suite green: `.venv/bin/python -m pytest -o addopts="" -q < /dev/null`
- [ ] Manual (optional): delete a throwaway copy of an old DB schema, run `ghcr ... status`/`tui`, confirm the migration adds columns without error and the header shows the token line.
