# CLAUDE.md — ghcr

Local daemon: polls GitHub repos, sends each new PR commit's filtered diff to the
DeepSeek API, posts an AI review as a bot account. See `README.md` for user-facing setup.

## Architecture

The package is split into **pure modules** (no I/O, no SDK, trivially testable) and
**I/O modules**. The only place they compose is `ReviewOrchestrator`.

- **Pure:** `models.py` (frozen dataclasses + `merge_usages`), `prompts.py` (prompt strings),
  `pipeline.py` (JSON parsing, dedup, test-file classification, markdown synthesis),
  `diff_filter.py`, `cost.py`, `comment.py`.
- **I/O:** `review.py` (`ReviewOrchestrator` — the composition root), `deepseek.py`
  (OpenAI-compatible client), `github.py` (`gh` CLI wrapper), `state.py` (SQLite),
  `poller.py`/`watcher.py`/`tui.py`/`cli.py`/`wizard.py`.

Keep new pure logic in a pure module so it stays unit-testable without network or SDK.

## Review pipeline

`review.mode` (config) selects the path; both share the prefix
`decide() → fetch diff → byte cap → filter_diff → empty check`.

- **single** — one DeepSeek call with `SYSTEM_PROMPT`. The legacy path; tests pin to it.
- **multi** (default) — accuracy-first, modeled on Claude Code's `/code-review`, adapted to
  ghcr's diff-only constraint (no repo checkout):
  1. Fan out N diff-only **lenses** (`correctness`, `security`, `maintainability`,
     `test_coverage`) as parallel calls; each returns structured JSON findings.
  2. **Dedup** findings (`pipeline.dedup_findings`, Jaccard on issue text per file).
  3. **Score** every finding with `scoring_votes` independent calls (median); keep
     `confidence >= confidence_threshold`.
  4. **Synthesize** markdown in Python (not a model call) with an explicit **Test coverage**
     verdict from the `test_coverage` lens.
  Cost scales ~(lenses + findings×votes)× a single review — that trade is intentional.

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
  substring of the system prompt (lens headers `## LENS: <name>` / scoring `## PASS: scoring`).
- Under the parallel pipeline, **assert call counts/sets, never order** (the fake's counter
  is lock-guarded; `calls_matching(substr)` helps).
- New pure logic is TDD'd first (`test_pipeline.py`, `test_models.py`, `test_prompts.py`).
