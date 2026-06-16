# Provider-aware usage monitoring in the TUI

**Date:** 2026-06-16
**Status:** Design approved, pending spec review
**Goal:** Make the live TUI provider-aware now that reviews can split across DeepSeek
(worker: lenses) and Claude Opus (advisor: planner + scoring). Three additions:
(1) label which model ran each sub-agent + show both models in the header;
(2) track per-provider token usage from ghcr's own calls; (3) show approximate
rolling 5h/7d Claude token windows parsed from `~/.claude` transcripts.

Builds on the hybrid-advisor feature (`review.advisor_provider`, PR #7).

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
- Real Claude usage data: `~/.claude/projects/**/*.jsonl` transcripts (per-session
  message logs; each assistant message has `message.usage` token counts + a
  top-level `timestamp` + model). This is the live, current source (the
  `stats-cache.json` `dailyModelTokens` is stale — months behind). The **official**
  weighted 5h/7d % is only delivered live to the statusline via stdin and is NOT
  file-readable by a separate daemon — so ghcr can only ever show an **approximate**
  raw-token window, never Anthropic's exact %.

## 2. Feature 1 — label the advisor (Opus) in the TUI

**Event change.** Add `model: str = ""` to `AgentEvent`. The orchestrator's
`_emit_agent` gains the model of the client that ran the call:
- planner (`context`) and scorer (`score:#N`) → `self.advisor.model`
- lenses (`lens:*`) and single-mode → `self.deepseek.model`

(`ClaudeCliClient` and `DeepSeekClient` both expose `.model`; `DeepSeekClient`
already has it, `ClaudeCliClient` already has it.)

**TUI change.**
- Agent board rows append the model when present: `score:#3 · opus  ✓`.
- Header shows both models when advisor differs from worker:
  `worker: deepseek-v4-pro · advisor: opus`. When `advisor_provider == "deepseek"`
  (advisor IS worker), show just the one model as today (no visual change off-path).

`_emit_agent` is the single choke point, so threading off the model is a one-line
change per call site (or pass it once where the agent string is built). No new
publish sites; the bus stays thread-safe (still pure data).

## 3. Feature 2 — per-provider token tracking (ghcr's own calls)

**Event change.** Redefine the existing `DeepSeekDone` token fields by provider and
add the advisor pair (no rename — keep the name to limit churn; it is the
"review done" event):
- `prompt_tokens` / `completion_tokens` → the **worker** (DeepSeek) portion.
- New `advisor_prompt_tokens: int = 0` / `advisor_completion_tokens: int = 0` →
  the **advisor** (Claude) portion.

Default path (`advisor_provider == "deepseek"`): advisor bucket is empty, advisor_*
= 0, and `prompt_tokens`/`completion_tokens` equal the full review as before →
**no behavior change off-path.** The orchestrator publishes from its existing
`worker_usages` / `advisor_usages` buckets: `merge_usages(worker_usages)` →
worker fields, `merge_usages(advisor_usages)` → advisor fields.

**TUI change.** `DashboardState` gains session counters: `worker_prompt`,
`worker_completion`, `advisor_prompt`, `advisor_completion` (ints, summed on each
`DeepSeekDone`). The header/cost region shows a per-provider line:
`deepseek 12.3k↑/4.1k↓ · opus 2.0k↑/0.9k↓` (advisor segment hidden when zero).
The existing per-review event-log line keeps working (now worker-only tokens,
which is the correct DeepSeek figure).

**No DB schema change.** Per-provider totals are session-scoped (accumulated from
the live event stream since TUI start). 24h/historical per-provider persistence is
out of scope (YAGNI) — `cost` history already lives in the DB and the cost split is
already correct there.

## 4. Feature 3 — approximate rolling 5h/7d Claude windows

### 4.1 New pure module `ghcr/usage_window.py`

```python
@dataclass(frozen=True)
class ModelTokens:
    total: int = 0                       # input + output + cache tokens
    by_model: tuple[tuple[str, int], ...] = ()  # [(model, tokens)], desc

@dataclass(frozen=True)
class WindowUsage:
    five_h: ModelTokens
    seven_d: ModelTokens
    five_h_decay_at: float | None = None  # earliest in-window ts + 5h (epoch); the
                                          # natural "reset" analog for a rolling sum
    seven_d_decay_at: float | None = None
    ok: bool = True                       # False = scan failed → TUI shows "n/a"

def scan_claude_usage(claude_dir: str, now: float,
                      five_h_secs: int = 5*3600,
                      seven_d_secs: int = 7*86400) -> WindowUsage:
    """Sum Claude token usage in the rolling 5h and 7d windows from CC transcripts.
    Best-effort: missing dir / unreadable files / malformed lines are skipped;
    any unexpected error returns WindowUsage(ok=False). Never raises."""
```

**Algorithm.**
1. Glob `{claude_dir}/projects/**/*.jsonl`. **Filter to files with mtime within
   the 7d window** — a file last written ≥7d ago cannot hold an in-window message.
   This bounds work to a handful of active-session files, not all ~900.
2. For each kept file, read line by line. Parse each line as JSON (skip on error).
   Keep lines that look like an assistant message with usage: a top-level
   `timestamp` (ISO-8601) and a `message.usage` object with integer token fields.
3. Token total per message = `input_tokens + output_tokens +
   cache_creation_input_tokens + cache_read_input_tokens` (missing → 0). Model =
   `message.model` (fallback `"unknown"`).
4. Bucket by age: `now - ts <= 7d` → seven_d; `<= 5h` → five_h (a 5h message is
   also in 7d). Accumulate totals + per-model sums; track the earliest in-window
   timestamp per window for the decay hint.
5. Return `WindowUsage`. Per-model lists sorted desc, capped to top N (e.g. 4).

Pure and dependency-free (stdlib `json`, `glob`, `os`, `datetime`). Unit-tested
against fixture `.jsonl` files with synthetic timestamps + a fixed `now`.

### 4.2 Periodic scan in the TUI

The TUI render loop already ticks on a timer. Add a throttled refresh: at most once
per `refresh_seconds` (default 60), run `scan_claude_usage` **off the render thread**
(a short-lived worker / the existing daemon worker) and store the result in
`DashboardState.window_usage`. The scan reading the filesystem must never block a
render frame; a stale-but-present `WindowUsage` is shown between refreshes.

### 4.3 TUI panel

A compact line/panel labeled to signal it is approximate and account-wide:
```
Claude (approx, all CC use)  5h 1.2M tok (decays @14:30)  ·  7d 14.8M tok
```
- Tokens human-formatted (k/M). Decay hint from `*_decay_at` (`@HH:MM` for 5h,
  `@Day HH:MM` for 7d); omitted if `None`.
- `ok=False` or absent → render `Claude usage: n/a`.
- No color thresholds / % (no real denominator) — raw totals only, per the chosen
  display option. (The optional per-model breakdown can render in `detail`/tooltip
  space if it fits; otherwise show only the totals.)

## 5. Config

New optional `monitor:` block (all defaulted; absent block = sensible defaults):
```yaml
monitor:
  claude_dir: ~/.claude          # transcript root for the 5h/7d scan
  window_refresh_seconds: 60     # how often to rescan
```
- `MonitorConfig(claude_dir: str, window_refresh_seconds: int)` frozen dataclass;
  `Config.monitor: MonitorConfig` with defaults so existing configs/`make_config`
  keep working.
- Restart-only (the TUI binds the scanner at startup). Document alongside the other
  restart-only fields.
- `claude_dir` is `~`-expanded in `load_config`.

## 6. Error handling / invariants

- **Window scan is best-effort** (mirrors the comment-fetch invariant): missing
  dir, unreadable file, malformed JSON, schema drift → skip / `ok=False`. It must
  never raise into the TUI loop or affect reviews. It is **display-only** — it reads
  no review state and feeds nothing back into the pipeline.
- **Thread safety** unchanged: `AgentEvent`/`DeepSeekDone` stay pure data published
  as today; the window scan runs off the render thread and only writes
  `DashboardState` (TUI-owned). No pool worker touches new shared state.
- **Frozen dataclasses**; `WindowUsage`/`ModelTokens`/`MonitorConfig` are frozen.
- **Off-path no-op:** with `advisor_provider == "deepseek"`, Features 1–2 render
  exactly as today (advisor model == worker, advisor tokens == 0). Feature 3 is
  independent of provider config (always shows account-wide Claude usage if a
  `~/.claude` exists).
- **Schema coupling** (Feature 3) is the known risk: the transcript `.jsonl` shape
  is Claude Code's internal format and may drift. Mitigation: parse defensively,
  degrade to `n/a`, and keep the parser isolated in one pure module so a fix is
  localized + unit-test-pinned.

## 7. Testing

- `tests/test_usage_window.py` (pure): fixture `.jsonl` with messages at known
  offsets from a fixed `now` → assert 5h vs 7d bucketing, per-model sums, cache
  token inclusion, decay timestamps; malformed lines skipped; missing dir →
  `ok=False`; a file with old mtime excluded.
- Orchestrator test: `DeepSeekDone` carries worker tokens in `prompt_tokens`/
  `completion_tokens` and Claude tokens in `advisor_*` when `advisor_provider=claude`;
  default path puts everything in the worker fields with `advisor_*` == 0.
- `AgentEvent.model` populated (advisor model for planner/scorer, worker for lenses).
- Config: `monitor` defaults applied when block absent; `claude_dir` `~`-expanded.
- TUI rendering: light — a state→panel-string test for the per-provider line and the
  window line (incl. the `n/a` path). No live terminal.

## 8. Out of scope (YAGNI)

- Anthropic's official weighted 5h/7d % (not file-accessible; user already has it in
  the statusline).
- Per-provider token **history**/24h persistence in SQLite (session totals suffice).
- Enforcement / self-throttle (pausing Claude calls at a cap) — not requested.
- Shelling out to `ccusage` (in-house parser chosen).
- Configurable caps / colored % bars for the windows (raw totals chosen).

## 9. Open items

None blocking. Defaults locked: in-house parser, raw totals + decay hint, no caps,
`monitor` restart-only, `DeepSeekDone` kept (not renamed), per-provider tokens
session-scoped (no DB migration).
