# Provider-aware usage monitoring in the TUI

**Date:** 2026-06-16
**Status:** Design approved, pending spec review
**Goal:** Make the live TUI provider-aware now that reviews can split across DeepSeek
(worker: lenses) and Claude Opus (advisor: planner + scoring). Two additions:
(1) label which model ran each sub-agent + show both models in the header;
(2) track **per-provider token usage**, persisted in SQLite so the TUI shows both
session and 24h totals per provider and they survive a restart.

Builds on the hybrid-advisor feature (`review.advisor_provider`, PR #7).

> **Scope note:** an earlier draft included rolling 5h/7d Claude-token windows parsed
> from `~/.claude` transcripts. **Dropped** — redundant with the official weighted %
> the user already sees in the Claude Code statusline, and ghcr could only ever show
> an inferior approximation. Not in this spec.

---

## 1. Background / what exists today

- TUI (`ghcr/tui.py`) renders a header, a per-PR agent board, a cost log, and an
  event log, fed by the in-process `EventBus` (`ghcr/events.py`).
- `AgentEvent(repo, pr_number, agent, status, detail, title)` drives the agent
  board; `agent` is e.g. `"lens:security"`, `"score:#3"`, `"context"` (planner).
  It carries **no model/provider** info.
- The header shows `state.model = cfg.deepseek.model` only — the worker model.
- `DeepSeekDone(repo, pr_number, prompt_tokens, completion_tokens, latency_s,
  snippet, title)` is published **once per review** with **merged** tokens (worker
  + advisor summed). The orchestrator already computes disjoint `worker_usages`
  (lenses) and `advisor_usages` (planner + scoring) buckets for the cost split, so
  the per-provider split is available at publish time but currently discarded.
- `state.py` `reviews` table stores merged `prompt_tokens` / `completion_tokens` /
  `cost_usd` per row; `record(...)` inserts them; `usd_spent_since(cutoff)` sums
  `cost_usd` for the 24h figure. Schema is created via `executescript(_SCHEMA)`
  (`CREATE TABLE IF NOT EXISTS`) with a `schema_meta` `version='1'` row. **No
  migration runner exists yet** — this spec adds a minimal one.

## 2. Feature 1 — label the advisor (Opus) in the TUI

**Event change.** Add `model: str = ""` to `AgentEvent`. The orchestrator's
`_emit_agent` tags each event with the model of the client that ran the call:
- planner (`context`) and scorer (`score:#N`) → `self.advisor.model`
- lenses (`lens:*`) → `self.deepseek.model`

Both `ClaudeCliClient` and `DeepSeekClient` already expose `.model`. `_emit_agent`
is the single choke point; thread the model in there (the caller already knows
whether it is a lens, planner, or scorer). The bus stays thread-safe (pure data).

**TUI change.**
- Agent board rows append the model when present: `score:#3 · opus  ✓`.
- Header shows both models when advisor differs from worker:
  `worker: deepseek-v4-pro · advisor: opus`. When `advisor_provider == "deepseek"`
  (advisor IS worker), show just the one model as today (no visual change off-path).

## 3. Feature 2 — per-provider token tracking, persisted

### 3.1 Event change

Redefine the existing `DeepSeekDone` token fields by provider and add the advisor
pair (keep the event name to limit churn — it is the "review done" event):
- `prompt_tokens` / `completion_tokens` → the **worker** (DeepSeek) portion.
- New `advisor_prompt_tokens: int = 0` / `advisor_completion_tokens: int = 0` →
  the **advisor** (Claude) portion.

Default path (`advisor_provider == "deepseek"`): advisor bucket is empty, advisor_*
= 0, and `prompt_tokens` / `completion_tokens` equal the full review as before →
**no behavior change off-path.** The orchestrator publishes from its existing
buckets: `merge_usages(worker_usages)` → worker fields, `merge_usages(advisor_usages)`
→ advisor fields.

### 3.2 Persistence (schema + migration)

**New columns on `reviews`:**
```sql
advisor_prompt_tokens     INTEGER NOT NULL DEFAULT 0,
advisor_completion_tokens INTEGER NOT NULL DEFAULT 0
```
- Add them to `_SCHEMA` (so fresh DBs get them) AND in a migration for existing DBs.
- The existing `prompt_tokens` / `completion_tokens` columns now hold the **worker**
  portion. Pre-existing rows keep their merged value there — which equals worker for
  every historical row (all prior reviews were DeepSeek-only), so no backfill needed.

**Minimal migration runner.** In `StateStore.__init__`, after `executescript(_SCHEMA)`:
- Read `PRAGMA table_info(reviews)`; if `advisor_prompt_tokens` is absent, run
  `ALTER TABLE reviews ADD COLUMN advisor_prompt_tokens INTEGER NOT NULL DEFAULT 0`
  and the matching `advisor_completion_tokens`. Idempotent (guarded by the column
  check), so it is safe to run every startup. Bump `schema_meta` version to `'2'`.
- `ALTER TABLE ADD COLUMN` with a constant default is O(1) in SQLite (no table
  rewrite), so this is cheap even on large DBs.

### 3.3 `record()` change

`record(...)` gains `advisor_usage: Usage | None = None`. The `usage` param now
carries the **worker** portion; `advisor_usage` the advisor portion. The INSERT adds
the two new columns (defaulting to 0 when `advisor_usage` is None).

**Orchestrator call sites:**
- Multi success + all-lenses-failed: pass `usage=merge_usages(worker_usages)`,
  `advisor_usage=merge_usages(advisor_usages)` (both lists already exist).
- Single mode / skips / post-failure: unchanged call shape — `usage` is the worker
  (or zero) and `advisor_usage` defaults None → 0. (Single mode is DeepSeek-only, so
  its one call is worker.)

### 3.4 24h query

New `StateStore.tokens_spent_since(cutoff: datetime) -> ProviderTokens` returning a
small frozen struct (or 4-tuple) of summed `prompt_tokens`, `completion_tokens`,
`advisor_prompt_tokens`, `advisor_completion_tokens` over rows with
`created_at >= cutoff`. Mirrors `usd_spent_since`.

### 3.5 TUI display

- `DashboardState` gains session counters (`worker_prompt`, `worker_completion`,
  `advisor_prompt`, `advisor_completion`), incremented on each `DeepSeekDone`.
- At startup, **seed the 24h per-provider totals from the DB** via
  `tokens_spent_since(now - 24h)` (the TUI is constructed with the store handle, the
  same way the 24h cost is available), then keep them live during the session.
- Header / cost region shows a per-provider line, e.g.:
  `tokens 24h — deepseek 1.2M↑/410k↓ · opus 180k↑/95k↓` (advisor segment hidden
  when its totals are zero, so the off-path display is unchanged).
- The existing per-review event-log line keeps working — now showing worker-only
  tokens, which is the correct DeepSeek figure for that line.

## 4. Config

**No new config.** Both features are driven by existing state (`advisor_provider`
selects whether the advisor differs; the models come from the constructed clients).

## 5. Error handling / invariants

- **Migration is safe + idempotent:** column-existence-guarded `ALTER`, constant
  default, runs every startup without harm. A fresh DB gets the columns from
  `_SCHEMA`; an old DB gets them from the migration; no backfill needed.
- **Post-then-record ordering unchanged.** Only the columns written by `record`
  change; the call still happens after a successful post on the success path.
- **Off-path no-op:** with `advisor_provider == "deepseek"`, advisor model == worker
  and advisor tokens == 0, so the header collapses to one model and the advisor
  token segment is hidden — identical to today.
- **Thread safety unchanged:** `AgentEvent` / `DeepSeekDone` stay pure data published
  on the main thread (the aggregated `DeepSeekDone` is already published once on the
  main thread, not from pool workers). No pool worker touches the store or new state.
- **Frozen dataclasses:** the new `ProviderTokens` struct (if used) is frozen;
  events stay frozen.
- **Terminal vs transient outcomes unchanged:** the migration adds columns only; the
  unique index and `SEEN_OUTCOMES` logic are untouched.

## 6. Testing

- `tests/test_state.py`:
  - `record(usage=worker, advisor_usage=advisor)` persists all four token columns;
    `tokens_spent_since` sums them across rows within the cutoff and excludes older
    rows.
  - Migration: open a DB created without the advisor columns (simulate by creating
    the old schema, or by deleting the columns), re-open via `StateStore`, assert the
    columns now exist and existing rows are readable (worker tokens intact,
    advisor tokens default 0).
  - Default call (`advisor_usage=None`) → advisor columns 0.
- Orchestrator test (`tests/test_review_hybrid.py` extension): with
  `advisor_provider=claude`, the published `DeepSeekDone` carries worker tokens in
  `prompt_tokens` / `completion_tokens` and Claude tokens in `advisor_*`; and the
  recorded row has the split persisted. Default path → everything in worker fields,
  `advisor_*` == 0.
- `AgentEvent.model` populated correctly (advisor model for planner/scorer, worker
  for lenses) — assert via the event capture already used in TUI/agent tests.
- TUI rendering: light state→panel-string test for the per-provider line, including
  the advisor-hidden (off-path) case and the both-providers case.

## 7. Out of scope (YAGNI)

- Rolling 5h/7d Claude-token windows / `~/.claude` transcript parsing (dropped —
  redundant with the statusline's official %).
- Anthropic's official weighted % (not file-accessible; user has it in the statusline).
- Enforcement / self-throttle (pausing Claude calls at a cap) — not requested.
- Per-finding or per-lens token attribution — only worker-vs-advisor is tracked.

## 8. Open items

None blocking. Defaults locked: `DeepSeekDone` kept (not renamed); worker tokens in
the existing columns + two new advisor columns; idempotent `ALTER`-based migration
bumping schema version to 2; per-provider 24h via `tokens_spent_since`; no new config.
