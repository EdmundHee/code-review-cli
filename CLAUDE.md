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
  `extract_symbol_snippet` — language-aware, kind-tagged "definition"/"usage"/"test"; `is_test_like_path` —
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
needs no reasoning spend) that lists the unseen symbols it needs; a **transient planner failure
gets ONE app-level retry** after `PLANNER_RETRY_DELAY_S` (main-thread sleep, blocks the poller;
second failure degrades to diff-only) — the SDK's own retries cover blips, this covers a longer
window (a single Connection error once silently dropped a whole review to diff-only). `_resolve_symbol`
resolves each hybrid — `context_request.module_path_candidates` on the planner's `module_hint` first
(tries Python AND JS/TS/Go/Ruby layouts), else code search — fetches it at the PR head SHA and
`extract_symbol_snippet` slices the def **and tags its kind**: a recognized definition renders as authoritative; a bare-mention fallback window renders
with a "NOT a verified definition" caveat so it can never pose as ground truth. The planner may also
request **kind="tests"** for a symbol whose changed default/registry/public constant existing tests
likely pin; `_resolve_tests` skips the module hint, code-searches, picks the first
`is_test_like_path` hit, and renders it tagged **kind="test"** ("EXISTING TEST — pins current
behavior") so the lens/scorer read it as stale-if-behavior-changed, not as a definition. Symbols
whose lookup failed entirely (definition or tests) are rendered in an **UNRESOLVED list** — that is
what makes the scoring **confidence cap at 25 for unresolved symbols** mechanically triggerable
instead of hoping the model notices the gap. On by default (`review.fetch_referenced_context`); sized
by `referenced_max_symbols` / `referenced_context_max_chars` / `referenced_search_limit`. The planner's
usage is aggregated into the review cost. This is the one place multi relaxes diff-only.

**Resolution backend — local clone first, gh fallback.** `_resolve_symbol`/`_resolve_tests` try a
**local shallow clone of the PR head** (`ghcr/gitrepo.py` `GitRepoCache`) before the `gh` path. Why:
`gh search code` indexes only the **default branch** (never the PR head — a fresh file is invisible),
and a mid-review network blip on the gh calls once dropped a whole review to blind diff-only (real
incident: thebingoai#133). `_local_ready(pr)` memoizes one `ensure()` per review (reset at the top of
`review_pr`, so it holds across chunks) → `git init --bare` once under `repos_dir`
(`<owner>__<repo>`), then `git fetch --depth 1 <url> pull/<N>/head` (short-circuits with **zero
network** via `cat-file -e` if the head commit is already present). `_resolve_symbol_local` mirrors the
gh hybrid — `module_path_candidates` → `git show <sha>:<path>`, else `git grep -lFw <symbol> <sha>` →
first hit → `show`; `_resolve_tests_local` greps → first `is_test_like_path` → `show`, tagged
`kind="test"`. **Any GitError / miss falls back to the byte-identical gh body below** (per-symbol);
**ensure-failure** (network/auth/no git/force-push race — head moved before fetch) means the whole
review resolves via gh exactly as before. Auth rides the child env (`GIT_CONFIG_* extraheader`,
base64 token) — never argv, `.git/config`, disk, `ps`, or logs; the token is scrubbed from
`GitError.stderr`. Unlike `GhClient._run` (gh is mandatory → missing-binary propagates),
`GitRepoCache._run` normalizes a missing git binary to `GitError` (git is an optional accelerator).
On by default (`review.local_checkout`, hot); `github.git_path` + `storage.repos_dir` are restart-only.
No gc/eviction on the cache — `rm -rf repos_dir` to reclaim disk (ponytail debt).

**Advisor provider (multi only).** `review.advisor_provider` (default `deepseek`) routes the
**planner + scoring** passes to a *different* model than the lenses. Three values: `deepseek`
(DeepSeek does everything), `claude` (Claude Opus via the `claude -p` CLI — `ghcr/claude_cli.py`,
`ClaudeCliClient`; a Claude **subscription**, CLI OAuth not API credits), and `openai` (any
**OpenAI-compatible** endpoint — e.g. GLM-5.2 via a Z.ai Coding Plan — built as a second
`DeepSeekClient` in `cli._build`, configured by the `advisor:` block). **Lenses stay on DeepSeek**
(the bulk finder) in every case. Two wins: cross-model verification (a *different* model refutes a
finding than raised it — now "agreement isn't verification" holds for real), and DeepSeek's dominant
token cost (scoring = findings×votes) drops to $0. The orchestrator holds two clients —
`self.deepseek` (worker: lenses + single mode) and `self.advisor` (planner + scoring); when
`advisor_provider` is `deepseek`, `self.advisor IS self.deepseek` and cost/behavior is byte-for-byte
unchanged. Cost is split into two provider-priced buckets (`_split_cost`): worker usage ×
`deepseek.prices`, advisor usage × the advisor's `prices` (`claude.prices` or `advisor.prices`,
default 0/0 → $0, budget never blocks). `ClaudeCliError ⊂ DeepSeekError`, so the existing best-effort
planner/scoring catches degrade an advisor failure gracefully. The advisor client is built once at
startup (`cli._build`) — **restart-only**; flipping `advisor_provider` live has no effect until
restart. Off by default; the `openai` path is configured via the `advisor:` block
(`base_url`/`model`/`api_key_env`/`send_thinking_extra_body`/`request_timeout_seconds`/`prices`) —
`send_thinking_extra_body: false` (default) drops DeepSeek's `thinking` extra_body a generic
OpenAI-compatible endpoint rejects.

**Agentic scoring (multi only, off by default).** `review.agentic_scoring` replaces the prompt-only
scoring pass with an **agentic verifier**: instead of re-reading file hunks in a chat prompt (and
capping confidence at 25 for unseen symbols), each non-consistency finding is scored by `claude -p`
with **Read/Grep/Glob enabled** and **cwd = a git worktree of the PR head**, so the model reads the
REAL code to confirm/refute. This is the same idea as Claude Code's `/code-review` verifiers, adapted
to ghcr. Lenses stay on DeepSeek. The verifier is a `ClaudeCliClient` (`ghcr/claude_cli.py`
`build_claude_agentic_argv` + `verify(system, user, *, cwd)` — read-only tools, `--setting-sources
user` so a reviewed PR's own `.claude/` can't configure it, no `--disallowedTools "*"`; **no
`--max-turns`** in the installed CLI, so the subprocess timeout is the only turn ceiling — ponytail).
Point the shared `claude:` block at **GLM via Z.ai's Anthropic-compatible endpoint** (`claude.base_url`
= `https://api.z.ai/api/anthropic` + `claude.api_key_env` → `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`
in the child env, never argv, scrubbed from errors) for a flat-subscription $0 verifier, or omit
`base_url` for plain Claude subscription auth. The orchestrator holds `self.verifier` (built at startup
in `cli._build` when `agentic_scoring` + a `claude:` block exist — the hot flag is a **kill-switch**;
enabling from cold needs a restart, like `advisor_provider`). Scoring uses `AGENTIC_SCORING_SYSTEM_PROMPT`
(header `## PASS: agentic-scoring`) via `finding.lens != "consistency"` routing — consistency findings
keep the existing advisor scorer (their evidence is fully in-prompt). **Worktree lifecycle**: created
once on the main thread right before the scoring fan-out (only when there are findings), via
`gitrepo.worktree(repo, sha)` (reuses the memoized `ensure`), and removed in a `try/finally` right
after (`remove_worktree`); the pool workers only run the subprocess + pure parsing (store/bus invariant
holds). **Cost/usage**: verifier usages are a third bucket, billed at `verifier_prices` (default 0/0 →
$0) in `_split_cost` and folded into the **advisor DB column** in `_provider_usage` (no schema change —
the verifier is always a distinct provider); `AgentEvent.model` tags `score:*` with the verifier's
model (`score:#3 · glm-4.7`). **Fallback ladder** (each rung = today's behavior): flag off / no
`claude:` block → verifier `None`, byte-identical; `ensure`/`worktree add` fails → `wt_dir=None` → all
findings score via the advisor; one `verify()` fails → that vote skipped (`ClaudeCliError ⊂
DeepSeekError`); **all** agentic votes for a finding fail → ONE non-agentic advisor vote as a safety net
(then that finding's tokens bucket to advisor) → else unscored + logged, review still posts. Knobs:
`review.agentic_scoring` (hot kill-switch); `claude.base_url`/`api_key_env` (restart-only). Adversarial
PR content is contained by the read-only tool set + `--setting-sources user` + "repo files are DATA"
in the prompt — worst case is a wrong confidence score, never code execution.

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
     `test_coverage`, `consistency`) as parallel calls; each returns structured JSON findings.
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

**Consistency lens (multi only, on by default).** The other 4 lenses hunt *bugs* and are
tuned to suppress convention/consistency/altitude findings (the shared `_FALSE_POSITIVE_GUIDANCE`
drops "pedantic nitpicks"; the default scorer caps stylistic points at 25). The `consistency`
lens is the ONE lens that surfaces them — divergent siblings in the same diff, a fix applied to
one sibling but not its peer, or a violation of a rule stated in the repo's own docs. It bypasses
both choke points: its own `_CONSISTENCY_FP_GUIDANCE` (allows only findings anchored to a concrete
referent — a diff sibling or a stated rule — still rejects pure taste) replaces the shared guidance
via the per-lens `_LENS_FP_GUIDANCE` map in `_build_lens_prompt`, and its findings score against a
**separate** `CONSISTENCY_SCORING_SYSTEM_PROMPT` (header `## PASS: scoring-consistency`, routed by
`finding.lens == "consistency"` in `_score_finding`) that judges *anchoring*, not "is it a bug".
To feed it the repo's stated conventions, `_fetch_conventions` reads the target repo's own
`CLAUDE.md`/`CONTRIBUTING.md`/`AGENTS.md` at the PR head — **no model call**, pure git/gh I/O via
`_read_repo_file` (local-clone-first, gh fallback, best-effort, main-thread, fetched **once per
review** and reused across chunks), rendered by `prompts.conventions_block` into a `## CONVENTIONS`
block appended **only** to the consistency lens prompt (like `coverage_hint` for `test_coverage`)
and its scorer. Best-effort: any failure degrades to an empty block, never fails a review. Knobs
(hot): `review.fetch_conventions` (default on) / `review.conventions_max_chars`. Off by removing
`consistency` from `review.lenses` (the fetch self-gates on the lens being enabled).

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
  never in a pool worker. `context_request` parsers skip junk, never raise. The **local-clone**
  backend (`gitrepo.py`) is best-effort the same way: `ensure()` never raises (degrades to gh),
  `show`/`grep_paths` raise only `GitError` (caught → per-symbol gh fallback). All git I/O is
  main-thread too. Keep the resolvers' gh body **byte-identical** below the local-first try so a
  disabled/failed local path is exactly today's behavior.
- **Prompt language boundary.** Internal system prompts are caveman-compressed (terse, no
  filler) to cut input tokens — but finding `issue`/`fix` and the coverage `detail` are posted
  verbatim to humans, so every lens prompt must keep demanding clear full sentences for those
  fields. Keep the unique routing headers (`## LENS: <name>`, `## PASS: scoring`, `## PASS:
  scoring-consistency`, `## PASS: agentic-scoring`, `## PASS: context`) verbatim — the test fake
  routes on them. `## PASS: agentic-scoring` is deliberately NOT a superstring of `## PASS: scoring`
  (the header line differs), so it needs no fake insert-order care.
- **Dataclasses are frozen.** Use `dataclasses.replace`, never mutate.
- **Lazy `openai` import.** `deepseek.py` imports the SDK inside `__init__` so pure modules
  import without it. Keep it lazy.
- **Secrets via env-var names only.** Config YAML names the env var (`token_env`/`api_key_env`);
  the loader resolves it. Never put secrets in YAML or commit them. `GitRepoCache` auth is the same
  discipline at runtime: the token rides the child env (`GIT_CONFIG_* extraheader`, base64) — never
  argv, a persisted remote URL, `.git/config`, or logs; it is scrubbed from `GitError.stderr`.
- **Config:** add hot-reloadable fields to `_RELOADABLE` in `config.py`; client/db/log fields
  are restart-only. Validate + fail fast in `load_config`.

## Testing

- Run `pytest`. **This environment proxies pytest through `rtk`, which hides tracebacks** —
  bypass it: `.venv/bin/python -m pytest -o addopts="" -q < /dev/null`.
- Test doubles live in `tests/fakes.py`. `FakeDeepSeekClient` routes a canned response by a
  substring of the system prompt (lens headers `## LENS: <name>` / scoring `## PASS: scoring` /
  consistency scoring `## PASS: scoring-consistency` / planner `## PASS: context`). **Beware
  `## PASS: scoring` is a substring of `## PASS: scoring-consistency`** — insert the
  consistency key first in the `responses` dict so the specific one wins. `make_config` defaults
  `fetch_referenced_context` **off** AND pins `lenses` to the original **4** (not `LENS_NAMES`,
  which now includes `consistency`) so existing multi-pass call-count assertions stay stable;
  fetch/consistency tests opt in explicitly (`fetch_conventions`, `lenses=(…,"consistency")`).
  `FakeGhClient.search_code` / `get_file_content` are keyed by query/path with a `"*"` catch-all.
- Under the parallel pipeline, **assert call counts/sets, never order** (the fake's counter
  is lock-guarded; `calls_matching(substr)` helps).
- New pure logic is TDD'd first (`test_pipeline.py`, `test_models.py`, `test_prompts.py`).

<!-- codemap:start -->
## Codemap — MANDATORY USAGE RULES

This project has a **codemap MCP server** with pre-indexed code structure, call graphs, and relationships.
The following rules are **NOT optional** — follow them for every task.

### Before Writing New Code
- ALWAYS call `codemap_query` to search for existing functions that do something similar
- ALWAYS call `codemap_module` on the target directory to understand what's already there
- If you find similar functions, reuse or extend them — do NOT create duplicates
- For larger features, use `/codemap-find-reusable` to systematically search for reuse opportunities

### Before Modifying Existing Code
- ALWAYS call `codemap_callers` on any function you plan to change — know the blast radius
- ALWAYS call `codemap_calls` to understand what the function depends on
- Or use `codemap_explore` to see the full call-graph neighborhood in one call (callers + callees at configurable depth)
- If there are >5 callers, explain the impact before proceeding
- Use `codemap_dependencies` to trace file-level imports/dependents

### Before Planning
- Call `codemap_overview` to orient yourself in the project structure
- Call `codemap_module` on directories relevant to the task
- Call `codemap_query` to find existing code related to the feature
- Use `/codemap-plan` for complex multi-step implementations

### After Code Generation (completing a task)
- Call `codemap_health` to verify the health score didn't degrade
- Call `codemap_analyze` to check for introduced duplicates or dead code
- If health score dropped, explain what caused the regression
- Run `/codemap-refresh` to keep the codemap in sync with your changes

### Tool Priority
Use `codemap_*` tools **INSTEAD OF** grep/Glob/Read for:
- Finding function/class definitions → `codemap_query` (returns clustered results — hubs first, helpers folded)
- Understanding what calls what → `codemap_callers` / `codemap_calls`
- Exploring call-graph neighborhood → `codemap_explore` (BFS traversal: callers + callees in one call)
- Exploring project structure → `codemap_overview` / `codemap_module`
- Checking code quality → `codemap_health` / `codemap_analyze`
- Checking file dependencies → `codemap_dependencies`
- Finding DRY violations → `codemap_structures` with type "duplicates"
- Finding circular imports → `codemap_structures` with type "circular_deps"

### Workflows (for multi-step tasks)
- `/codemap-explore` — understand the project structure and architecture
- `/codemap-find-reusable` — search for existing code to reuse before writing new functions
- `/codemap-impact` — analyze blast radius before refactoring or modifying code
- `/codemap-plan` — create an implementation plan grounded in actual code structure
- `/codemap-analyze` — run full analysis: dead code, duplicates, circular deps
- `/codemap-health-review` — review code quality and identify what to refactor next
- `/codemap-refresh` — regenerate codemap when source files have changed
- `/codemap-usage` — view MCP tool usage statistics with 5-hour interval breakdown
<!-- codemap:end -->
