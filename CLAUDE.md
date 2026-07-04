# CLAUDE.md — ghcr

Local daemon: polls GitHub repos, sends each new PR commit's filtered diff to the
DeepSeek API, posts an AI review as a bot account. See `README.md` for user-facing setup.

## Architecture

The package is split into **pure modules** (no I/O, no SDK, trivially testable) and
**I/O modules**. The only place they compose is `ReviewOrchestrator`.

- **Pure:** `models.py` (frozen dataclasses + `merge_usages`), `prompts.py` (prompt strings),
  `pipeline.py` (JSON parsing, dedup, test-file classification, markdown synthesis),
  `prior_context.py` (parse PR comments, render context block, `find_mention_triggers`),
  `context_request.py` (parse the planner pass, `module_path_candidates`,
  `extract_symbol_snippet` — language-aware, kind-tagged "definition"/"usage" —
  render the referenced-definitions block incl. the UNRESOLVED list),
  `diff_filter.py` (incl. `file_hunks`), `cost.py`, `comment.py`.
- **I/O:** `review.py` (`ReviewOrchestrator` — the composition root), `deepseek.py`
  (OpenAI-compatible client), `github.py` (`gh` CLI wrapper), `state.py` (SQLite),
  `poller.py`/`watcher.py`/`tui.py`/`cli.py`/`wizard.py`.

Keep new pure logic in a pure module so it stays unit-testable without network or SDK.

## Review pipeline

`review.mode` (config) selects the path; both share the prefix
`decide() → fetch diff → hard raw ceiling (diff.hard_max_diff_bytes, OOM guard) → filter_diff →
empty check → post-filter size cap → fetch prior PR comments`. The size cap measures
`FilteredDiff.kept_bytes` (what survives filtering), NOT raw bytes — lockfile/generated junk the
filter strips must never disqualify a PR. In single mode (or `diff.chunk_reviews: false`) an
over-cap filtered diff still skips (`skip_oversized`); in multi it is **chunked** (below).

**Chunked review (multi only).** A filtered diff over the per-chunk budget is split by
`diff_filter.chunk_filtered_diff` into chunks of whole files (grouped by top-level dir; a single
file over the budget is truncated at a hunk boundary with an explicit marker + comment-footer note)
and the lens fan-out runs per chunk — sequentially on the main thread, only the fan-out pooled.
Findings accumulate across chunks into ONE dedup → scoring → synthesize → post → record →
`DeepSeekDone` tail; coverage verdicts merge via `pipeline.merge_coverage` (any-False wins);
outcome is plain `reviewed`. **Chunk sizing must satisfy the token cap, not just the byte cap**
(`cost.per_chunk_diff_budget` = min of both; the token sum-gate binds first at defaults — chunking
at `max_diff_bytes` alone would still skip) and all guards (`budget <= 0`, budget gate) run
PRE-spend: `_handle_oversized` writes a terminal row and must never fire mid-loop after chunk 1
spent money. Each finding is scored against ITS chunk's text/ref-ctx so the scoring full-diff
fallback stays bounded by one chunk. Overflow past `diff.max_review_chunks` reviews the first N
chunks and lists the unreviewed files in the comment (never a silent gap). Knobs (all
hot-reloadable): `diff.chunk_reviews` (default on) / `hard_max_diff_bytes` / `max_review_chunks`.

**Two triggers.** (1) **Head SHA** — the poller reviews each PR once per unique head SHA
(`decide()` → `already_reviewed`). (2) **@mention** — after `review_pr`, the poller calls
`orch.rereview_if_mentioned(pr, just_reviewed=...)`: a new comment containing `@{bot_login}`
(non-bot author, id past the `comment_triggers` watermark) fires a full re-review via
`review_pr(trigger="mention")`. First sight of a PR baselines the watermark silently (no fire on
historical @mentions). A mention re-review records outcome **`rereviewed`** — distinct from
`reviewed` so it sidesteps the `WHERE outcome='reviewed'` unique index and is NOT in `SEEN_OUTCOMES`
(head-SHA logic untouched), but still counts toward the budget. Off via `review.rereview_on_mention`.

**Prior-comment context.** After the empty check, `_fetch_pr_comments` pulls the PR's
existing issue-timeline + inline review comments (best-effort) and `prior_context.build_prior_context`
renders one capped, newest-first block. It is injected into every lens user prompt (discussion
context) and every scoring user prompt — where the scorer returns confidence 0 for a finding
**already raised** by the bot on an earlier commit or by a human (no extra model calls; suppression
rides the existing scoring pass). Off via `review.read_prior_comments`; sized by `prior_comment_max_chars`.

**Referenced-context fetch (multi only).** The diff-only constraint makes the model guess about
unseen code (a called method, a base class, a field's meaning) and fire confident false BLOCKERs.
To fix the root cause, multi optionally fetches the real source: after `classify_test_signal` and
**after the budget gate** (it is spend), `_fetch_referenced_context` runs a **planner** call
(`CONTEXT_REQUEST_PROMPT`, header `## PASS: context`, **thinking disabled** — listing symbols
needs no reasoning spend) that lists the unseen symbols it needs; `_resolve_symbol` resolves each
hybrid — `context_request.module_path_candidates` on the planner's `module_hint` first (tries
Python AND JS/TS/Go/Ruby layouts), else `gh search_code` — fetches it at the PR head SHA
(`gh get_file_content`, raw blob), and `extract_symbol_snippet` slices the def **and tags its
kind**: a recognized definition renders as authoritative; a bare-mention fallback window renders
with a "NOT a verified definition" caveat so it can never pose as ground truth. Symbols whose
lookup failed entirely are rendered in an **UNRESOLVED list** — that is what makes the scoring
**confidence cap at 25 for unresolved symbols** mechanically triggerable instead of hoping the
model notices the gap. On by default (`review.fetch_referenced_context`); sized by
`referenced_max_symbols` / `referenced_context_max_chars` / `referenced_search_limit`. The planner's
usage is aggregated into the review cost. This is the one place multi relaxes diff-only.

**Advisor provider (multi only).** `review.advisor_provider` (default `deepseek`) routes the
**planner + scoring** passes to Claude Opus via the `claude -p` CLI (`ghcr/claude_cli.py`,
`ClaudeCliClient`) — leveraging a Claude **subscription** (CLI OAuth, not API credits) instead of
DeepSeek tokens. **Lenses stay on DeepSeek** (the bulk finder). Two wins: cross-model verification
(a *different* model refutes a finding than raised it — now "agreement isn't verification" holds for
real), and DeepSeek's dominant token cost (scoring = findings×votes) drops to $0. The orchestrator
holds two clients — `self.deepseek` (worker: lenses + single mode) and `self.advisor` (planner +
scoring); when `advisor_provider` is `deepseek`, `self.advisor IS self.deepseek` and cost/behavior is
byte-for-byte unchanged. Cost is split into two provider-priced buckets (`_split_cost`): worker usage
× `deepseek.prices`, advisor usage × `claude.prices` (default 0/0 → $0, budget never blocks).
`ClaudeCliError ⊂ DeepSeekError`, so the existing best-effort planner/scoring catches degrade a CLI
failure gracefully. The advisor client is built once at startup (`cli._build`) — **restart-only**;
flipping `advisor_provider` live has no effect until restart. Off by default; configured via the
`claude:` block (`claude_path`/`model`/`request_timeout_seconds`/`prices`).

**Per-provider usage in the TUI.** Tokens are tracked split by **real provider** — worker (DeepSeek,
lenses) vs advisor (Claude, planner+scoring). `_provider_usage` attributes by the *actual* provider,
not the cost bucket: when `self.advisor IS self.deepseek` (off-path), planner+scoring tokens fold into
the worker total and advisor tokens are 0 — so a DeepSeek-only review shows no phantom Opus usage.
`DeepSeekDone` carries both (`prompt_tokens`/`completion_tokens` = worker; `advisor_*` = advisor),
`AgentEvent.model` tags which model ran each sub-agent (`score:#3 · opus`), and the split is
**persisted** on the `reviews` row (`advisor_prompt_tokens`/`advisor_completion_tokens`; schema v2 via
an idempotent, column-guarded `ALTER` migration in `StateStore._migrate`). The TUI seeds for first
paint, then the **main render thread re-queries the DB once/sec** (`tui.refresh_from_db` — its own
main-thread `StateStore`, never shared with the worker): rolling-24h spend + per-provider tokens
(`usd_spent_since`/`tokens_spent_since`, → `ProviderTokens`) so the header **ages out exactly like the
budget gate** (no monotonic drift), plus a **calendar-day `today $X`** figure (`usd_spent_since`
from `_local_midnight`, resets at 00:00 local). The DB is the single source of truth — `reduce()` no
longer bumps `spent_24h`/`tok24_*` on events (so no cross-thread write race); only the cost/agent/
history panels and `sess_*` session totals stay event-driven. Header shows both models +
`24h $…/$… · today $… · tok ds …↑/…↓ · opus …↑/…↓`. Off-path the advisor columns/fields are 0 and the
display collapses to a single model — unchanged.

- **single** — one DeepSeek call with `SYSTEM_PROMPT`. The legacy path; tests pin to it. Stays
  pure diff-only (no planner/fetch).
- **multi** (default) — accuracy-first, modeled on Claude Code's `/code-review`, adapted to
  ghcr's diff-only constraint (no repo checkout, except the optional referenced-context fetch above):
  1. Fan out N **lenses** (`correctness`, `security`, `maintainability`,
     `test_coverage`) as parallel calls; each returns structured JSON findings.
  2. **Dedup** findings (`pipeline.dedup_findings`, Jaccard on issue text per file).
  3. **Score** every finding with `scoring_votes` independent calls (median); keep
     `confidence >= confidence_threshold`. The scorer is told to try to REFUTE the finding
     first (it is the same model that raised it — agreement isn't verification). Each scoring
     call sends only the finding's **file hunks** (`diff_filter.file_hunks`), not the whole
     diff — the full diff re-sent per finding×vote was the dominant token cost; falls back to
     the full diff when the finding's path isn't in it.
  4. **Synthesize** markdown in Python (not a model call) with an explicit **Test coverage**
     verdict from the `test_coverage` lens.
  Cost scales ~(1 planner + lenses + findings×votes)× a single review — that trade is intentional.

## Invariants — do not break

- **Post the comment FIRST, then `store.record`.** A failed post must never be recorded as
  `reviewed` (else the PR is silently never retried). See the post/record tail in `review.py`.
- **Terminal vs transient outcomes.** Terminal (`reviewed`/`skip_*`) write one row per
  `(repo, pr, head_sha)` and block re-review; transient skips (seen/draft/author) and `error`
  rows are NOT in the seen-set, so they retry next cycle. `state.SEEN_OUTCOMES` is the source of truth.
- **Thread safety.** Multi-pass model calls run in a `ThreadPoolExecutor`. Workers may ONLY
  call `deepseek.review` + pure parsing — never touch the single SQLite connection or the
  `EventBus`. Aggregate usage, record, and publish `DeepSeekDone` (once, aggregated) on the
  main thread only.
- **Robust JSON parsing.** The thinking model wraps JSON in fences and prose. `pipeline`
  parsers strip fences, fall back to the first balanced block, validate per-element, and
  return `None` only on total failure — a bad lens degrades to empty findings, never raises.
- **Comment fetch is best-effort.** `_fetch_pr_comments` wraps the `gh` calls in
  `try/except GhError → None` (**`None` = fetch failed, distinct from `[]` = genuinely no
  comments**); a fetch failure must never block or fail the review. `review_pr` coerces `None → []`
  (empty context block). `rereview_if_mentioned` treats `None` as "skip this cycle, retry next" so
  a failed **first-sight** fetch never baselines the watermark at 0 (which would replay every
  historical @mention once the fetch recovers). `prior_context` parsers skip junk, never raise.
- **Referenced-context fetch is best-effort.** `_fetch_referenced_context` / `_resolve_symbol`
  wrap the planner call (`try/except DeepSeekError`) and each `gh` call (`try/except GhError`);
  any failure degrades to an empty block and the review proceeds diff-only — a fetch must never
  block or fail a review. All planner + `gh` I/O runs on the **main thread** (before lens fan-out),
  never in a pool worker. `context_request` parsers skip junk, never raise.
- **Prompt language boundary.** Internal system prompts are caveman-compressed (terse, no
  filler) to cut input tokens — but finding `issue`/`fix` and the coverage `detail` are posted
  verbatim to humans, so every lens prompt must keep demanding clear full sentences for those
  fields. Keep the unique routing headers (`## LENS: <name>`, `## PASS: scoring`, `## PASS:
  context`) verbatim — the test fake routes on them.
- **Dataclasses are frozen.** Use `dataclasses.replace`, never mutate.
- **Lazy `openai` import.** `deepseek.py` imports the SDK inside `__init__` so pure modules
  import without it. Keep it lazy.
- **Secrets via env-var names only.** Config YAML names the env var (`token_env`/`api_key_env`);
  the loader resolves it. Never put secrets in YAML or commit them.
- **Config:** add hot-reloadable fields to `_RELOADABLE` in `config.py`; client/db/log fields
  are restart-only. Validate + fail fast in `load_config`.

## Testing

- Run `pytest`. **This environment proxies pytest through `rtk`, which hides tracebacks** —
  bypass it: `.venv/bin/python -m pytest -o addopts="" -q < /dev/null`.
- Test doubles live in `tests/fakes.py`. `FakeDeepSeekClient` routes a canned response by a
  substring of the system prompt (lens headers `## LENS: <name>` / scoring `## PASS: scoring` /
  planner `## PASS: context`). `make_config` defaults `fetch_referenced_context` **off** so the
  existing multi-pass call-count assertions stay stable; fetch tests opt in explicitly.
  `FakeGhClient.search_code` / `get_file_content` are keyed by query/path with a `"*"` catch-all.
- Under the parallel pipeline, **assert call counts/sets, never order** (the fake's counter
  is lock-guarded; `calls_matching(substr)` helps).
- New pure logic is TDD'd first (`test_pipeline.py`, `test_models.py`, `test_prompts.py`).
