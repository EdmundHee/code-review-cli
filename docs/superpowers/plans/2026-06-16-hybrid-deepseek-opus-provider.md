# Hybrid DeepSeek + Claude Opus Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route the `multi` review's planner + scoring phases to Claude Opus via `claude -p` (subscription auth) while lenses stay on DeepSeek — cutting DeepSeek token cost and adding cross-model verification. Default off.

**Architecture:** New duck-typed `ClaudeCliClient` (subprocess wrapper, same `.review()` interface as `DeepSeekClient`) is injected into `ReviewOrchestrator` as a second `advisor` client. One config switch (`review.advisor_provider`) builds it; planner + scoring call sites reroute from `self.deepseek` to `self.advisor`. Cost is split into two provider-priced buckets so Opus tokens bill at $0, not the DeepSeek rate.

**Tech Stack:** Python 3, stdlib `subprocess`/`json`, frozen dataclasses, pytest. Spec: `docs/superpowers/specs/2026-06-16-hybrid-deepseek-opus-provider-design.md`.

---

## File Structure

- **Create** `ghcr/claude_cli.py` — `build_claude_argv` (pure), `ClaudeCliError`, `ClaudeCliClient`. One responsibility: shell `claude -p` and return a `ReviewResult`.
- **Create** `tests/test_claude_cli.py` — pure tests (monkeypatched `subprocess.run`).
- **Create** `tests/test_review_hybrid.py` — routing + cost-split tests.
- **Modify** `ghcr/config.py` — `ClaudeConfig` dataclass, `Config.claude` field, `advisor_provider` on `ReviewModeConfig`, parse + validate.
- **Modify** `ghcr/review.py` — `ReviewOrchestrator.__init__` gains `advisor`; reroute planner (`:238`) + scoring (`:484`); `_split_cost`.
- **Modify** `ghcr/cli.py` — `_build` constructs `ClaudeCliClient` when selected.
- **Modify** `tests/fakes.py` — `FakeClaudeCliClient`, `make_config(advisor_provider=...)`.
- **Modify** `CLAUDE.md` + `README.md` — document the hybrid path + config keys.

Run tests with (rtk hides tracebacks — bypass it):
`.venv/bin/python -m pytest -o addopts="" -q < /dev/null`

---

## Task 1: ClaudeConfig + advisor_provider config

**Files:**
- Modify: `ghcr/config.py` (dataclasses near `DeepSeekConfig:67-75`; `_parse_review:163-198`; `load_config` tail `:287-300`; reload sets `:134-135`)
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_config.py` (write a small YAML to a tmp file and load it). Match the existing test style in that file; if it uses a helper to write config, reuse it. Minimal standalone version:

```python
import textwrap
from ghcr.config import load_config, ConfigError
import pytest


def _write(tmp_path, body):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(body))
    return str(p)


_BASE = """
    github: {bot_login: bot, token_env: GH_TOKEN}
    deepseek: {api_key_env: DS_KEY}
    repos: [owner/repo]
"""


def test_advisor_provider_defaults_to_deepseek(tmp_path):
    cfg = load_config(_write(tmp_path, _BASE), env={"GH_TOKEN": "x", "DS_KEY": "y"})
    assert cfg.review.advisor_provider == "deepseek"
    assert cfg.claude is None


def test_advisor_provider_claude_builds_claude_config(tmp_path):
    body = _BASE + """
    review: {advisor_provider: claude}
    claude: {model: opus, claude_path: /usr/local/bin/claude}
    """
    cfg = load_config(_write(tmp_path, body), env={"GH_TOKEN": "x", "DS_KEY": "y"})
    assert cfg.review.advisor_provider == "claude"
    assert cfg.claude.model == "opus"
    assert cfg.claude.claude_path == "/usr/local/bin/claude"
    assert cfg.claude.prices.input_per_1m == 0.0
    assert cfg.claude.prices.output_per_1m == 0.0


def test_advisor_provider_invalid_raises(tmp_path):
    body = _BASE + "\nreview: {advisor_provider: gemini}\n"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, body), env={"GH_TOKEN": "x", "DS_KEY": "y"})
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.venv/bin/python -m pytest -o addopts="" tests/test_config.py -k advisor -q < /dev/null`
Expected: FAIL (`AttributeError: ... 'advisor_provider'` / `cfg.claude`).

- [ ] **Step 3: Add `ClaudeConfig` dataclass**

In `ghcr/config.py`, after `DeepSeekConfig` (line 75):

```python
@dataclass(frozen=True)
class ClaudeConfig:
    claude_path: str
    model: str
    request_timeout_seconds: int
    prices: Prices  # default 0/0 → subscription is flat, report $0
```

- [ ] **Step 4: Add `advisor_provider` to `ReviewModeConfig`**

In `ReviewModeConfig` (after `referenced_search_limit`, line 107) add:

```python
    advisor_provider: str = "deepseek"  # "deepseek" | "claude" (planner+scoring on Opus)
```

And in `_parse_review`'s returned `ReviewModeConfig(...)` (line 185), add the field + validation. Before the `return`:

```python
    advisor_provider = review.get("advisor_provider", "deepseek")
    if advisor_provider not in ("deepseek", "claude"):
        raise ConfigError(
            f"review.advisor_provider must be 'deepseek' or 'claude', got {advisor_provider!r}"
        )
```

Then pass `advisor_provider=advisor_provider,` inside the `ReviewModeConfig(...)` constructor.

- [ ] **Step 5: Add `Config.claude` field**

In `Config` (line 116-127), after `deepseek: DeepSeekConfig` add:

```python
    claude: "ClaudeConfig | None"
```

Place it so it does not break the existing positional construction — add it at the END of the dataclass fields (after `log_level`) with a default so existing `Config(...)` call sites and `make_config` keep working:

```python
    log_level: str
    claude: "ClaudeConfig | None" = None
```

- [ ] **Step 6: Parse the `claude:` block in `load_config`**

In `load_config`, near the other block extractions (top of function, alongside `ds = raw.get(...)`), add:

```python
    claude_raw = raw.get("claude", {}) or {}
```

After `review_cfg = _parse_review(review)` (line 287), build the optional config:

```python
    claude_cfg = None
    if review_cfg.advisor_provider == "claude":
        cprices_raw = claude_raw.get("prices", {}) or {}
        claude_cfg = ClaudeConfig(
            claude_path=claude_raw.get("claude_path", "claude"),
            model=claude_raw.get("model", "opus"),
            request_timeout_seconds=int(claude_raw.get("request_timeout_seconds", 600)),
            prices=Prices(
                input_per_1m=float(cprices_raw.get("input_per_1m", 0.0)),
                output_per_1m=float(cprices_raw.get("output_per_1m", 0.0)),
            ),
        )
```

Then in the `Config(...)` return (line 289), add `claude=claude_cfg,`.

- [ ] **Step 7: Document restart-only behavior**

`advisor_provider` lives in the hot-reloadable `review` block, but the client is built at startup — a live flip has no effect until restart. Add a comment above `_RELOADABLE` (line 134):

```python
# NOTE: review.advisor_provider is hot-reloadable as a value, but the advisor
# CLIENT is constructed at startup (cli._build) — flipping deepseek<->claude
# requires a restart to take effect.
```

- [ ] **Step 8: Run tests, verify pass**

Run: `.venv/bin/python -m pytest -o addopts="" tests/test_config.py -q < /dev/null`
Expected: PASS (all config tests, including the 3 new ones).

- [ ] **Step 9: Commit**

```bash
git add ghcr/config.py tests/test_config.py
git commit -m "feat(config): add advisor_provider switch + ClaudeConfig"
```

---

## Task 2: ClaudeCliClient + pure argv builder

**Files:**
- Create: `ghcr/claude_cli.py`
- Test: `tests/test_claude_cli.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_claude_cli.py`:

```python
import json
import subprocess
import pytest

from ghcr.claude_cli import ClaudeCliClient, ClaudeCliError, build_claude_argv
from ghcr.deepseek import DeepSeekError
from ghcr.cost import Prices


def test_build_claude_argv_has_expected_flags():
    argv = build_claude_argv("/bin/claude", "opus", "SYS PROMPT")
    assert argv[0] == "/bin/claude"
    assert "-p" in argv
    assert argv[argv.index("--system-prompt") + 1] == "SYS PROMPT"
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--disallowedTools") + 1] == "*"


def _fake_run(stdout, returncode=0, stderr=""):
    def run(argv, input=None, capture_output=True, text=True, timeout=None):
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)
    return run


def test_review_parses_result_and_usage(monkeypatch):
    payload = json.dumps({
        "type": "result", "is_error": False, "result": "FINDING TEXT",
        "total_cost_usd": 0.42, "usage": {"input_tokens": 120, "output_tokens": 30},
    })
    monkeypatch.setattr(subprocess, "run", _fake_run(payload))
    c = ClaudeCliClient(claude_path="/bin/claude", model="opus")
    r = c.review("sys", "user")
    assert r.content == "FINDING TEXT"
    assert r.usage.prompt_tokens == 120
    assert r.usage.completion_tokens == 30
    assert r.model == "opus"


def test_prices_default_zero():
    c = ClaudeCliClient(claude_path="/bin/claude", model="opus")
    assert c.prices == Prices(0.0, 0.0)


def test_thinking_arg_is_accepted_and_ignored(monkeypatch):
    payload = json.dumps({"result": "x", "usage": {"input_tokens": 1, "output_tokens": 1}})
    monkeypatch.setattr(subprocess, "run", _fake_run(payload))
    c = ClaudeCliClient(claude_path="/bin/claude", model="opus")
    r = c.review("sys", "user", thinking="disabled")  # must not raise
    assert r.content == "x"


def test_nonzero_exit_raises_claude_error(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("", returncode=1, stderr="boom"))
    c = ClaudeCliClient(claude_path="/bin/claude", model="opus")
    with pytest.raises(ClaudeCliError):
        c.review("sys", "user")


def test_bad_json_raises_claude_error(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("not json at all"))
    c = ClaudeCliClient(claude_path="/bin/claude", model="opus")
    with pytest.raises(ClaudeCliError):
        c.review("sys", "user")


def test_empty_result_raises_claude_error(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(json.dumps({"result": "", "usage": {}})))
    c = ClaudeCliClient(claude_path="/bin/claude", model="opus")
    with pytest.raises(ClaudeCliError):
        c.review("sys", "user")


def test_claude_error_is_deepseek_error_subclass(monkeypatch):
    # orchestrator's `except DeepSeekError` must catch claude failures unchanged
    monkeypatch.setattr(subprocess, "run", _fake_run("", returncode=1))
    c = ClaudeCliClient(claude_path="/bin/claude", model="opus")
    with pytest.raises(DeepSeekError):
        c.review("sys", "user")


def test_timeout_raises_claude_error(monkeypatch):
    def run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
    monkeypatch.setattr(subprocess, "run", run)
    c = ClaudeCliClient(claude_path="/bin/claude", model="opus")
    with pytest.raises(ClaudeCliError):
        c.review("sys", "user")
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.venv/bin/python -m pytest -o addopts="" tests/test_claude_cli.py -q < /dev/null`
Expected: FAIL (`ModuleNotFoundError: ghcr.claude_cli`).

- [ ] **Step 3: Implement `ghcr/claude_cli.py`**

```python
"""Claude Code CLI client (`claude -p`), used as the multi-pass *advisor* provider.

Subscription auth only works through the Claude CLI, so we shell out rather than
hit the Anthropic API SDK (which would bill per-token credits). Mirrors
``deepseek.py``: a pure ``build_claude_argv`` (unit-testable without subprocess)
plus a thin client exposing the same ``review()`` interface as ``DeepSeekClient``.
"""

from __future__ import annotations

import json
import subprocess

from .cost import Prices
from .deepseek import DeepSeekError
from .models import ReviewResult, Usage


class ClaudeCliError(DeepSeekError):
    """Subclass of DeepSeekError so the orchestrator's existing
    ``except DeepSeekError`` best-effort paths catch CLI failures unchanged."""


def build_claude_argv(claude_path: str, model: str, system_prompt: str) -> list[str]:
    """Pure argv builder (no subprocess). User prompt is fed via stdin, NOT argv,
    to avoid OS arg-length limits on large diffs."""
    return [
        claude_path, "-p",
        "--system-prompt", system_prompt,
        "--model", model,
        "--output-format", "json",
        "--disallowedTools", "*",  # pure text generator: no file reads / tool loops
    ]


class ClaudeCliClient:
    def __init__(
        self,
        claude_path: str = "claude",
        model: str = "opus",
        timeout: int = 600,
        prices: Prices | None = None,
    ):
        self.claude_path = claude_path
        self.model = model
        self.timeout = timeout
        # 0/0 → subscription is flat; the orchestrator's cost split reads this.
        self.prices = prices if prices is not None else Prices(0.0, 0.0)

    def review(self, system_prompt: str, user_prompt: str, *, thinking: str | None = None) -> ReviewResult:
        # `thinking` is accepted for interface parity with DeepSeekClient and
        # intentionally ignored — the CLI has no equivalent toggle.
        argv = build_claude_argv(self.claude_path, self.model, system_prompt)
        try:
            proc = subprocess.run(
                argv, input=user_prompt, capture_output=True, text=True, timeout=self.timeout
            )
        except subprocess.TimeoutExpired as e:
            raise ClaudeCliError(f"claude -p timed out after {self.timeout}s") from e
        except OSError as e:  # binary missing / not executable
            raise ClaudeCliError(f"claude -p failed to launch: {e}") from e

        if proc.returncode != 0:
            raise ClaudeCliError(
                f"claude -p exited {proc.returncode}: {(proc.stderr or '').strip()[:500]}"
            )
        try:
            data = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError) as e:
            raise ClaudeCliError(f"claude -p returned non-JSON output: {e}") from e

        content = (data.get("result") or "").strip()
        if not content:
            raise ClaudeCliError("claude -p returned empty result")

        u = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(u.get("input_tokens", 0) or 0),
            completion_tokens=int(u.get("output_tokens", 0) or 0),
            total_tokens=int(u.get("input_tokens", 0) or 0) + int(u.get("output_tokens", 0) or 0),
        )
        return ReviewResult(content=content, usage=usage, model=self.model)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/python -m pytest -o addopts="" tests/test_claude_cli.py -q < /dev/null`
Expected: PASS (all 9 tests).

- [ ] **Step 5: Commit**

```bash
git add ghcr/claude_cli.py tests/test_claude_cli.py
git commit -m "feat: add ClaudeCliClient (claude -p) advisor provider"
```

---

## Task 3: Test doubles — FakeClaudeCliClient + make_config knob

**Files:**
- Modify: `tests/fakes.py` (after `FakeDeepSeekClient`, ~line 224; `make_config` signature ~line 24 and `ReviewModeConfig(...)` ~line 80)

- [ ] **Step 1: Add `advisor_provider` to `make_config`**

In `make_config`'s signature (after `referenced_search_limit: int = 5,`):

```python
    advisor_provider: str = "deepseek",
```

In the `ReviewModeConfig(...)` it builds (after `referenced_search_limit=referenced_search_limit,`):

```python
            advisor_provider=advisor_provider,
```

`Config.claude` defaults to `None` (added in Task 1 Step 5), so `make_config` needs no other change.

- [ ] **Step 2: Add `FakeClaudeCliClient`**

After `FakeDeepSeekClient` in `tests/fakes.py`:

```python
class FakeClaudeCliClient:
    """Advisor double — same system-prompt-substring routing as FakeDeepSeekClient,
    plus a ``prices`` attr (0/0) so the cost split bills it at $0. Lock-guarded
    because scoring calls it from the thread pool."""

    def __init__(self, *, content="## Summary\nadvisor ok", usage=None, raises=False, responses=None):
        self.content = content
        self.usage = usage or Usage(prompt_tokens=200, completion_tokens=100, total_tokens=300)
        self.raises = raises
        self.responses = responses or {}
        self.prices = Prices(0.0, 0.0)
        self._lock = threading.Lock()
        self.calls = 0
        self.systems: list[str] = []
        self.users: list[str] = []

    def review(self, system_prompt, user_prompt, *, thinking=None):
        with self._lock:
            self.calls += 1
            self.systems.append(system_prompt)
            self.users.append(user_prompt)
        if self.raises:
            from ghcr.claude_cli import ClaudeCliError

            raise ClaudeCliError("claude down")
        return ReviewResult(content=self._route(system_prompt, user_prompt), usage=self.usage, model="opus")

    def _route(self, system_prompt, user_prompt):
        for key, val in self.responses.items():
            if key in system_prompt:
                return val(system_prompt, user_prompt) if callable(val) else val
        return self.content

    def calls_matching(self, substr: str) -> int:
        with self._lock:
            return sum(1 for s in self.systems if substr in s)
```

- [ ] **Step 3: Verify fakes import cleanly**

Run: `.venv/bin/python -c "import tests.fakes" < /dev/null && echo OK`
Expected: `OK`

- [ ] **Step 4: Run full suite (nothing should break — advisor defaults to deepseek)**

Run: `.venv/bin/python -m pytest -o addopts="" -q < /dev/null`
Expected: PASS (existing tests unaffected).

- [ ] **Step 5: Commit**

```bash
git add tests/fakes.py
git commit -m "test: add FakeClaudeCliClient + advisor_provider knob"
```

---

## Task 4: Orchestrator — advisor client, reroute, cost split

**Files:**
- Modify: `ghcr/review.py` (`__init__:81-87`; planner `:238`; scoring `:484`; cost sites `:361,367-376,384-396`)
- Test: `tests/test_review_hybrid.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_review_hybrid.py`. Mirror the construction style in `tests/test_review_fetch_context.py` (same fakes + `make_config(review_mode="multi", fetch_referenced_context=True)`). Read that file first for the exact `ReviewOrchestrator(...)` + `make_pr` + diff setup, then:

```python
from ghcr.review import ReviewOrchestrator
from ghcr.models import ACTION_REVIEW
from tests.fakes import (
    FakeGhClient, FakeDeepSeekClient, FakeClaudeCliClient, make_config, make_pr,
)
# ... StateStore import as used in test_review_fetch_context.py

_DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
 def f():
+    return bad_helper()
"""


def _orch(tmp_path, ds, advisor):
    gh = FakeGhClient(diff=_DIFF, file_contents={"*": "def bad_helper(): ..."},
                      search_results={"*": [{"path": "app.py"}]})
    store = StateStore(str(tmp_path / "s.db"))
    cfg = make_config(db_path=str(tmp_path / "s.db"), review_mode="multi",
                      scoring_votes=1, confidence_threshold=0,
                      fetch_referenced_context=True, advisor_provider="claude")
    return ReviewOrchestrator(gh, ds, store, cfg, advisor=advisor), gh


def test_planner_and_scoring_route_to_advisor(tmp_path):
    ds = FakeDeepSeekClient(responses={"## LENS:": '{"findings":[{"severity":"WARNING","file":"app.py","issue":"x","fix":"y"}]}'})
    advisor = FakeClaudeCliClient(responses={
        "## PASS: context": '{"requests":[]}',
        "## PASS: scoring": '{"confidence": 90, "reason": "ok"}',
    })
    orch, gh = _orch(tmp_path, ds, advisor)
    out = orch.review_pr(make_pr())
    assert out.action == ACTION_REVIEW
    # planner + scoring on the advisor:
    assert advisor.calls_matching("## PASS: context") == 1
    assert advisor.calls_matching("## PASS: scoring") >= 1
    # lenses on deepseek, NOT the advisor:
    assert ds.calls_matching("## LENS:") >= 1
    assert advisor.calls_matching("## LENS:") == 0
    assert ds.calls_matching("## PASS: scoring") == 0


def test_advisor_tokens_billed_at_zero(tmp_path):
    ds = FakeDeepSeekClient(responses={"## LENS:": '{"findings":[{"severity":"WARNING","file":"app.py","issue":"x","fix":"y"}]}'})
    advisor = FakeClaudeCliClient(responses={
        "## PASS: context": '{"requests":[]}',
        "## PASS: scoring": '{"confidence": 90, "reason": "ok"}',
    })
    orch, gh = _orch(tmp_path, ds, advisor)
    out = orch.review_pr(make_pr())
    # cost = lenses only (deepseek prices); advisor 0/0. With 4 lenses @1000 prompt
    # /500 completion tokens and prices 0.28/3.48 per 1M:
    lens_count = ds.calls_matching("## LENS:")
    expected = lens_count * (1000 / 1e6 * 0.28 + 500 / 1e6 * 3.48)
    assert abs(out.cost_usd - expected) < 1e-9


def test_default_advisor_is_worker_unchanged(tmp_path):
    # advisor omitted → advisor is the deepseek worker; everything on ds.
    ds = FakeDeepSeekClient(responses={
        "## LENS:": '{"findings":[]}',
        "## PASS: context": '{"requests":[]}',
    })
    gh = FakeGhClient(diff=_DIFF)
    store = StateStore(str(tmp_path / "s.db"))
    cfg = make_config(db_path=str(tmp_path / "s.db"), review_mode="multi", confidence_threshold=0)
    orch = ReviewOrchestrator(gh, ds, store, cfg)  # no advisor kwarg
    out = orch.review_pr(make_pr())
    assert out.action == ACTION_REVIEW
```

> Note: confirm the lens/scoring JSON shapes against `pipeline.parse_lens_payload` / `parse_score` while writing (read those parsers); adjust the canned JSON to whatever they accept. The assertions on call routing and cost are the point.

- [ ] **Step 2: Run tests, verify they fail**

Run: `.venv/bin/python -m pytest -o addopts="" tests/test_review_hybrid.py -q < /dev/null`
Expected: FAIL (`ReviewOrchestrator.__init__() got an unexpected keyword argument 'advisor'`).

- [ ] **Step 3: Add `advisor` to `__init__`**

In `ghcr/review.py`, `ReviewOrchestrator.__init__` (line 81):

```python
    def __init__(self, gh, deepseek, store, config: Config, now=None, bus=None, advisor=None):
        self.gh = gh
        self.deepseek = deepseek            # worker: lenses + single mode
        self.advisor = advisor or deepseek   # planner + scoring (defaults to worker)
        self.store = store
        self.cfg = config
        self.bus = bus
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.worker_prices = config.deepseek.prices
        self.advisor_prices = getattr(self.advisor, "prices", config.deepseek.prices)
```

- [ ] **Step 4: Add the `_split_cost` helper**

Add a method on `ReviewOrchestrator` (near `_map_parallel`):

```python
    def _split_cost(self, worker_usages, advisor_usages) -> float:
        """Bill each provider's usage at its own price. When advisor IS the worker
        (default), both prices are deepseek's → identical to a single-price total."""
        return (
            estimate_cost_usd(merge_usages(worker_usages), self.worker_prices)
            + estimate_cost_usd(merge_usages(advisor_usages), self.advisor_prices)
        )
```

- [ ] **Step 5: Reroute planner + scoring to `self.advisor`**

In `_fetch_referenced_context` (line 238): change `self.deepseek.review(` → `self.advisor.review(`.

In `_score_finding` (line 484): change `self.deepseek.review(SCORING_SYSTEM_PROMPT, user)` → `self.advisor.review(SCORING_SYSTEM_PROMPT, user)`.

Leave lens (`:455`) and single mode (`:163`) on `self.deepseek`.

- [ ] **Step 6: Split usage buckets in the multi path**

In `review_pr`'s multi branch, replace the single `usages` accumulation. At line 361:

```python
        worker_usages: list[Usage] = [lr.usage for lr in lens_results]
        advisor_usages: list[Usage] = list(ref_usages)
```

All-lenses-failed branch (lines 367-376):

```python
        if len(lens_errors) == len(lenses):
            usage = merge_usages(worker_usages + advisor_usages)
            cost = self._split_cost(worker_usages, advisor_usages)
            if not dry_run:
                self.store.record(
                    pr.repo, pr.number, pr.head_sha, ACTION_ERROR,
                    model=model, usage=usage, cost_usd=cost, error="all review lenses failed",
                )
            log.error("all lenses failed repo=%s pr=%s", pr.repo, pr.number)
            return ReviewOutcome(ACTION_ERROR, cost_usd=cost)
```

Scoring loop (line 384-392): change `usages.extend(score_usages)` → `advisor_usages.extend(score_usages)`.

Final cost (lines 395-396):

```python
        usage = merge_usages(worker_usages + advisor_usages)
        cost = self._split_cost(worker_usages, advisor_usages)
```

Verify no other references to the old `usages` name remain in `review_pr`'s multi branch (search the method). The single-mode branch keeps its own logic untouched.

- [ ] **Step 7: Run hybrid + full suite, verify pass**

Run: `.venv/bin/python -m pytest -o addopts="" tests/test_review_hybrid.py -q < /dev/null`
Expected: PASS.
Run: `.venv/bin/python -m pytest -o addopts="" -q < /dev/null`
Expected: PASS (existing multi-pass tests unaffected — default advisor == worker, single-price math preserved).

- [ ] **Step 8: Commit**

```bash
git add ghcr/review.py tests/test_review_hybrid.py
git commit -m "feat(review): route planner+scoring to advisor, split cost by provider"
```

---

## Task 5: Wire the client in cli.py

**Files:**
- Modify: `ghcr/cli.py` (`_build:28-40`, import line 12)

- [ ] **Step 1: Import the client**

In `ghcr/cli.py`, after `from .deepseek import DeepSeekClient` (line 12):

```python
from .claude_cli import ClaudeCliClient
```

- [ ] **Step 2: Build the advisor in `_build`**

Replace the tail of `_build` (lines 38-40):

```python
    advisor = ds  # default: advisor is the DeepSeek worker
    if cfg.review.advisor_provider == "claude":
        advisor = ClaudeCliClient(
            claude_path=cfg.claude.claude_path,
            model=cfg.claude.model,
            timeout=cfg.claude.request_timeout_seconds,
            prices=cfg.claude.prices,
        )
    store = StateStore(cfg.db_path)
    orch = ReviewOrchestrator(gh, ds, store, cfg, bus=bus, advisor=advisor)
    return gh, ds, store, orch
```

- [ ] **Step 3: Smoke-test config load + build path**

Run: `.venv/bin/python -c "import ghcr.cli; print('ok')" < /dev/null`
Expected: `ok` (import clean — no runtime claude call here).

- [ ] **Step 4: Run full suite**

Run: `.venv/bin/python -m pytest -o addopts="" -q < /dev/null`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ghcr/cli.py
git commit -m "feat(cli): construct ClaudeCliClient when advisor_provider=claude"
```

---

## Task 6: Docs

**Files:**
- Modify: `CLAUDE.md` (Review pipeline section), `README.md` (config reference)

- [ ] **Step 1: Update `CLAUDE.md`**

In the "Review pipeline" section, after the `multi` description, add:

```markdown
**Advisor provider (multi only).** `review.advisor_provider` (default `deepseek`)
routes the **planner + scoring** passes to Claude Opus via `claude -p`
(`ghcr/claude_cli.py`, `ClaudeCliClient`) — leveraging a Claude subscription
(CLI OAuth auth, not API credits) instead of DeepSeek tokens. Lenses stay on
DeepSeek (the bulk finder). This gives cross-model verification (a different model
refutes than raised the finding) and cuts DeepSeek's dominant token cost.
`ClaudeCliError ⊂ DeepSeekError` so best-effort paths catch it unchanged; advisor
usage bills at `claude.prices` (default 0/0 → $0). Restart-only: the client is
built at startup in `cli._build`. Off by default; `claude:` config block sets
`claude_path`/`model`/`request_timeout_seconds`/`prices`.
```

- [ ] **Step 2: Update `README.md`**

Add a `claude:` block to the documented config example and a short note that it requires the `claude` CLI logged into a subscription. Example:

```yaml
review:
  mode: multi
  advisor_provider: claude   # planner + scoring on Claude Opus (default: deepseek)

claude:
  claude_path: claude        # CLI on PATH, logged into your subscription
  model: opus
  request_timeout_seconds: 600
  # prices default 0/0 → subscription is flat, reported as $0
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: document advisor_provider hybrid path"
```

---

## Final verification

- [ ] Run the complete suite: `.venv/bin/python -m pytest -o addopts="" -q < /dev/null` — expect all green.
- [ ] Manual smoke (optional, needs a logged-in `claude` CLI + real config): set `review.advisor_provider: claude`, run `review-once` on a small PR, confirm the review posts and cost shows lenses-only.
