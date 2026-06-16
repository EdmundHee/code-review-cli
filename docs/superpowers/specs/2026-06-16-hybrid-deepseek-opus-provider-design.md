# Hybrid review: DeepSeek worker + Claude Opus advisor

**Date:** 2026-06-16
**Status:** Design approved, pending spec review
**Goal:** Leverage the user's Claude subscription (flat-rate, free at point of use)
to offload the reasoning/verification phases of a `multi` review onto Claude Opus,
while DeepSeek-v4-pro keeps doing the token-heavy bulk finding. Net effect: lower
DeepSeek API spend + cross-model verification (a different model refutes findings
than the one that raised them).

---

## 1. Motivation

The `multi` pipeline runs four model phases:

| Phase | What it does | Token weight |
|---|---|---|
| Planner (context request) | Lists unseen symbols to fetch; one call, thinking disabled | tiny |
| Lenses (find) | N parallel calls, each returns structured findings | heavy |
| Scoring (refute/verify) | `findings × scoring_votes` independent calls | **dominant** |
| Synthesis | Pure Python, no model call | none |

Two problems this design addresses:

1. **Cost.** Scoring is the dominant DeepSeek token cost (each finding re-scored
   `scoring_votes` times). Planner is pure overhead.
2. **Verification quality.** The scorer is told to *refute* a finding first — but
   today it is the **same model** that raised it, so "agreement isn't verification"
   only half-holds. A different model (Opus) refuting is genuine cross-model
   verification. (This is the noted next lever against false positives.)

Both are solved by routing **planner + scoring → Claude Opus** (via the user's
subscription) and keeping **lenses → DeepSeek**.

## 2. Why `claude -p` (CLI subprocess), not an SDK

Subscription auth **only works through the Claude CLI**. The decision:

| Option | Auth | Verdict |
|---|---|---|
| `claude -p` subprocess | Subscription (CLI OAuth) | **chosen** |
| Claude Agent SDK (`claude_agent_sdk`) | Subscription — *wraps the same CLI* | rejected: async, heavier, no gain |
| Anthropic API SDK (`anthropic`) | API credits — **NOT** subscription | rejected: wrong billing |

`claude -p` also matches the existing codebase pattern: `github.py` already wraps
the `gh` CLI. Single-shot JSON generation needs no streaming / tool loops / session
management, so the Agent SDK's value is dead weight here. The orchestrator is
sync + threaded; subprocess is sync-friendly, asyncio bridging is not.

CLI confirmed available: `claude` v2.1.178 at `/Users/edmundhee/.local/bin/claude`,
supporting `--system-prompt` (full override), `--model`, `--output-format json`,
`--disallowedTools`.

## 3. Routing (final)

| Phase | Provider | Model | Orchestrator client |
|---|---|---|---|
| Planner | **Claude** | opus | `self.advisor` |
| Lenses | DeepSeek | deepseek-v4-pro | `self.deepseek` (worker) |
| Scoring | **Claude** | opus | `self.advisor` |
| Synthesis | — | — | Python |
| `single` mode (legacy) | DeepSeek | deepseek-v4-pro | `self.deepseek` |

One config switch flips planner + scoring together. **Default off** — when off,
`self.advisor` *is* `self.deepseek` and behavior is byte-for-byte unchanged.

## 4. Components

### 4.1 New module `ghcr/claude_cli.py` (I/O)

Mirrors `deepseek.py`'s shape: a pure argv builder + a thin subprocess client.

```python
class ClaudeCliError(DeepSeekError):   # subclass so orchestrator's existing
    pass                               # `except DeepSeekError` catches it unchanged

def build_claude_argv(claude_path, model, system_prompt) -> list[str]:
    """Pure, unit-testable (mirrors deepseek.build_request_kwargs).
    User prompt is passed via stdin, NOT argv, to avoid arg-length limits."""
    return [
        claude_path, "-p",
        "--system-prompt", system_prompt,
        "--model", model,
        "--output-format", "json",
        "--disallowedTools", "*",      # pure text generator: no file reads / tool loops
    ]

class ClaudeCliClient:
    def __init__(self, claude_path, model="opus", timeout=600, prices=Prices(0.0, 0.0)):
        self.model = model
        self.prices = prices           # 0/0 → $0; read by orchestrator cost split
        ...
    def review(self, system_prompt, user_prompt, *, thinking=None) -> ReviewResult:
        # thinking is accepted and IGNORED (no CLI equivalent) — keeps the
        # duck-typed interface identical to DeepSeekClient.
        ...
```

`review()` behavior:
- Run `build_claude_argv(...)`, feed `user_prompt` on **stdin** (`subprocess.run`,
  `capture_output=True`, `text=True`, `timeout=self.timeout`).
- Parse stdout as JSON. Extract:
  - `result` → `content` (the assistant text).
  - `usage.input_tokens` / `usage.output_tokens` → `Usage` (for token **display**
    only; cost is computed from `self.prices`, which is 0/0).
  - `total_cost_usd` is **ignored** — the subscription is flat; we report $0.
- Raise `ClaudeCliError` (⊂ `DeepSeekError`) on: nonzero exit, timeout,
  non-JSON stdout, missing/empty `result`. Message includes stderr tail.

The interface contract (`review(system, user, *, thinking) -> ReviewResult`) is
identical to `DeepSeekClient`, so it drops into any orchestrator client slot.

### 4.2 Config

New frozen dataclass + a switch.

```python
@dataclass(frozen=True)
class ClaudeConfig:
    claude_path: str            # default "claude" (resolved on PATH)
    model: str                  # default "opus"
    request_timeout_seconds: int
    prices: Prices              # default Prices(0.0, 0.0) → $0

# ReviewModeConfig gains:
    advisor_provider: str = "deepseek"   # "deepseek" | "claude"
```

- `Config` gains an optional `claude: ClaudeConfig | None` field.
- `load_config` parses an optional `claude:` YAML block; builds `ClaudeConfig`
  only if `advisor_provider == "claude"` (fail fast if claude selected but block
  malformed / CLI unresolvable is left to runtime, not load — keep load cheap).
- Validate `advisor_provider in {"deepseek", "claude"}`, else `ConfigError`.
- `advisor_provider` is **reloadable** (lives in `review:` block → already in
  `_RELOADABLE`). `claude:` (constructs a client) is **restart-only** → add to
  `_RESTART_ONLY`. Note: a live swap of `advisor_provider` deepseek→claude only
  takes effect if a claude client was built at startup; document that flipping it
  on requires a restart (the client is restart-bound). Simpler: treat
  `advisor_provider` as restart-only too, to avoid the half-swap footgun.
  **Decision: `advisor_provider` is restart-only** (moved out of the `review`
  hot-reload set for this field's effect, documented in config comments).

### 4.3 Orchestrator wiring (`review.py`)

`ReviewOrchestrator.__init__` gains an `advisor` param:

```python
def __init__(self, gh, deepseek, store, config, now=None, bus=None, advisor=None):
    ...
    self.deepseek = deepseek                  # worker: lenses + single mode
    self.advisor  = advisor or deepseek        # planner + scoring (defaults to worker)
    self.worker_prices  = config.deepseek.prices
    self.advisor_prices = getattr(self.advisor, "prices", config.deepseek.prices)
```

`advisor or deepseek` is the **default-off guarantee**: existing callers/tests that
construct `ReviewOrchestrator(gh, ds, store, cfg)` get `advisor is deepseek`, and
`advisor_prices == deepseek.prices` → cost math identical to today.

Three call-site reroutes:
- `review.py:238` (planner) — `self.deepseek.review(...)` → `self.advisor.review(...)`.
- `review.py:484` (scoring) — `self.deepseek.review(...)` → `self.advisor.review(...)`.
- `review.py:455` (lens) — **stays** `self.deepseek`.
- `review.py:163` (single mode) — **stays** `self.deepseek`.

### 4.4 Cost split (correctness)

Today usages are pooled into one list and billed at `deepseek.prices`
(`review.py:361, 386, 395-396`). Mixing Opus tokens into that pool and billing at
the DeepSeek rate would be **wrong**. Split into two buckets:

```python
worker_usages:  list[Usage] = [lr.usage for lr in lens_results]      # lenses
advisor_usages: list[Usage] = list(ref_usages)                       # planner
# ... in scoring loop: advisor_usages.extend(score_usages)

def _split_cost(worker_usages, advisor_usages):
    return (estimate_cost_usd(merge_usages(worker_usages),  self.worker_prices)
          + estimate_cost_usd(merge_usages(advisor_usages), self.advisor_prices))
```

- Displayed `usage` (for `DeepSeekDone` event + `store.record`) stays
  `merge_usages(worker + advisor)` — full token totals, unchanged.
- `cost` uses `_split_cost`. When advisor=deepseek (default), both prices equal
  `deepseek.prices` → result == today's single `estimate_cost_usd`. When
  advisor=claude, advisor bucket × 0/0 = $0 → cost = lenses-only.
- Apply the same split in the **all-lenses-failed** branch (`review.py:367-376`),
  where the only usages are planner (advisor) + (no lenses).

`single` mode (`review.py:~178`) is untouched: one DeepSeek call, billed at
`deepseek.prices`.

### 4.5 CLI build (`cli.py::_build`)

```python
advisor = ds  # default: advisor is the DeepSeek worker
if cfg.review.advisor_provider == "claude":
    advisor = ClaudeCliClient(
        claude_path=cfg.claude.claude_path,
        model=cfg.claude.model,
        timeout=cfg.claude.request_timeout_seconds,
        prices=cfg.claude.prices,
    )
orch = ReviewOrchestrator(gh, ds, store, cfg, bus=bus, advisor=advisor)
```

## 5. Invariants preserved

- **Best-effort planner.** Planner already wraps `DeepSeekError`
  (`review.py:241`). `ClaudeCliError ⊂ DeepSeekError`, so a failed Opus planner
  degrades to diff-only exactly as before. Same for scoring (`review.py:485` —
  a failed vote is skipped).
- **Thread safety.** Scoring runs in pool workers (`_map_parallel`). A subprocess
  call from a worker thread is fine — `ClaudeCliClient.review` touches no SQLite /
  EventBus, only `subprocess.run` + pure parsing. Planner runs on the **main
  thread** (before lens fan-out), unchanged.
- **Frozen dataclasses.** `ClaudeConfig` is frozen; use `replace`.
- **Lazy heavy imports.** `claude_cli.py` imports only stdlib (`subprocess`,
  `json`) — no SDK. Pure modules still import without `openai`.
- **Secrets via env only.** No new secrets — subscription auth lives in the CLI's
  own credential store, not in our config or env.
- **Prompt language boundary / routing headers.** Unchanged. Opus receives the
  exact same `CONTEXT_REQUEST_PROMPT` / `SCORING_SYSTEM_PROMPT` as system prompt.

## 6. Testing

- `tests/fakes.py`: `FakeClaudeCliClient` — same canned-response routing by system
  prompt substring (`## PASS: scoring`, `## PASS: context`) as `FakeDeepSeekClient`,
  plus `prices` attr and a lock-guarded call counter. `make_config` keeps
  `advisor_provider` defaulting to `"deepseek"` so existing multi-pass call-count
  assertions are stable; hybrid tests opt in explicitly.
- New `tests/test_claude_cli.py` (pure, no subprocess):
  - `build_claude_argv` produces expected flags (system prompt, model, json,
    disallowedTools).
  - JSON parse: `result` → content, `usage` → tokens.
  - Error normalization: nonzero exit / bad JSON / empty result → `ClaudeCliError`,
    and `isinstance(err, DeepSeekError)` holds.
- New `tests/test_review_hybrid.py`:
  - With `advisor_provider="claude"`: planner + scoring calls land on the fake
    Claude client; lens calls land on the fake DeepSeek client (assert call sets,
    not order).
  - Cost excludes advisor (Opus) tokens: cost == lenses-only at deepseek prices.
  - Default (`deepseek`): advisor IS the worker; cost identical to current.
- Existing `multi` tests unchanged (default keeps DeepSeek scoring).

## 7. Out of scope (YAGNI)

- Per-PR / runtime provider toggle (config-only for now).
- Routing lenses to Opus (lenses stay DeepSeek — that's the bulk worker by design).
- Final "overview" advisor pass (considered, rejected — does not reduce DeepSeek
  tokens; scoring-on-Opus already delivers cross-model verification).
- Reporting Opus's notional `total_cost_usd` (subscription is flat → $0).

## 8. Open items

None blocking. Defaults locked: advisor model `opus`, `advisor_provider` default
`deepseek` (opt-in), `ClaudeCliError ⊂ DeepSeekError`, user prompt via stdin.
