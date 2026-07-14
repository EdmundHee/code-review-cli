"""Claude Code CLI client (`claude -p`), used as the multi-pass *advisor* provider
and — with tools enabled — the agentic *verifier* (scoring pass).

Subscription auth only works through the Claude CLI, so we shell out rather than
hit the Anthropic API SDK (which would bill per-token credits). Mirrors
``deepseek.py``: pure argv builders (unit-testable without subprocess) plus a thin
client exposing the same ``review()`` interface as ``DeepSeekClient``, and a
``verify()`` that runs the loop with read-only tools inside a repo worktree.

Two invocation modes:
- ``review()`` — pure text generator, no tools (``--disallowedTools "*"``). Used as
  the advisor for planner/scoring.
- ``verify()`` — Read/Grep/Glob enabled, ``cwd`` = a PR-head worktree, so the model
  confirms/refutes a finding against the REAL code, not a diff snippet.

A ``base_url`` + ``auth_token`` point the CLI at an Anthropic-compatible endpoint
(e.g. GLM via Z.ai) through the child env (``ANTHROPIC_BASE_URL`` /
``ANTHROPIC_AUTH_TOKEN``) — never argv, and scrubbed from any error text. Absent
those, the CLI uses its own subscription auth.
"""

from __future__ import annotations

import json
import os
import subprocess

from .cost import Prices
from .deepseek import DeepSeekError
from .models import ReviewResult, Usage


class ClaudeCliError(DeepSeekError):
    """Subclass of DeepSeekError so the orchestrator's existing
    ``except DeepSeekError`` best-effort paths catch CLI failures unchanged."""


def build_claude_argv(claude_path: str, model: str, system_prompt: str) -> list[str]:
    """Pure argv builder for the no-tools text-generation mode (advisor). User prompt
    is fed via stdin, NOT argv, to avoid OS arg-length limits on large diffs."""
    return [
        claude_path, "-p",
        "--system-prompt", system_prompt,
        "--model", model,
        "--output-format", "json",
        "--disallowedTools", "*",  # pure text generator: no file reads / tool loops
    ]


# Read-only tools the agentic verifier may use; everything state-changing / network
# is explicitly denied as defense in depth (the allow-list already gates the rest).
_AGENTIC_ALLOWED = "Read,Grep,Glob"
_AGENTIC_DISALLOWED = "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Task,TodoWrite"


def build_claude_agentic_argv(claude_path: str, model: str, system_prompt: str) -> list[str]:
    """Pure argv builder for the agentic verifier: read-only tools enabled, project
    settings NOT loaded (``--setting-sources user`` — a reviewed PR's own ``.claude/``
    must never configure the verifier). No ``--max-turns`` (absent in the installed
    CLI); the subprocess timeout bounds a runaway loop. ponytail: timeout is the only
    ceiling on turns — add a turn cap if the CLI grows the flag."""
    return [
        claude_path, "-p",
        "--system-prompt", system_prompt,
        "--model", model,
        "--output-format", "json",
        "--allowedTools", _AGENTIC_ALLOWED,
        "--disallowedTools", _AGENTIC_DISALLOWED,
        "--setting-sources", "user",
    ]


def _extract_error(stdout: str, stderr: str) -> str:
    """Best-effort error string from a failed claude -p call. In ``--output-format
    json`` claude writes API errors to STDOUT (``api_error_status`` + ``result``) and
    leaves stderr EMPTY — so reading stderr alone yields a blank message. Read the
    JSON body first, fall back to stderr, then to raw stdout."""
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        data = None
    if isinstance(data, dict):
        parts = []
        if data.get("api_error_status"):
            parts.append(f"api_error_status={data['api_error_status']}")
        msg = (data.get("result") or "").strip()
        if msg:
            parts.append(msg)
        if parts:
            return " ".join(parts)[:500]
    return ((stderr or "").strip() or (stdout or "").strip() or "(no output)")[:500]


class ClaudeCliClient:
    def __init__(
        self,
        claude_path: str = "claude",
        model: str = "opus",
        timeout: int = 600,
        prices: Prices | None = None,
        base_url: str = "",
        auth_token: str = "",
    ):
        self.claude_path = claude_path
        self.model = model
        self.timeout = timeout
        # 0/0 → subscription is flat; the orchestrator's cost split reads this.
        self.prices = prices if prices is not None else Prices(0.0, 0.0)
        self.base_url = base_url
        self.auth_token = auth_token

    def _child_env(self) -> dict | None:
        """Env for the child process. None → inherit (CLI subscription auth). When a
        base_url override is set, add ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN — env
        only, so the token never lands in argv/ps."""
        if not self.base_url:
            return None
        env = dict(os.environ)
        env["ANTHROPIC_BASE_URL"] = self.base_url
        if self.auth_token:
            env["ANTHROPIC_AUTH_TOKEN"] = self.auth_token
        return env

    def _scrub(self, text: str) -> str:
        return text.replace(self.auth_token, "***") if self.auth_token else text

    def _invoke(self, argv: list[str], user_prompt: str, cwd: str | None = None) -> ReviewResult:
        try:
            proc = subprocess.run(
                argv, input=user_prompt, capture_output=True, text=True,
                timeout=self.timeout, env=self._child_env(), cwd=cwd,
            )
        except subprocess.TimeoutExpired as e:
            raise ClaudeCliError(f"claude -p timed out after {self.timeout}s") from e
        except OSError as e:  # binary missing / not executable
            raise ClaudeCliError(f"claude -p failed to launch: {e}") from e

        if proc.returncode != 0:
            raise ClaudeCliError(
                f"claude -p exited {proc.returncode}: {self._scrub(_extract_error(proc.stdout, proc.stderr))}"
            )
        try:
            data = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError) as e:
            raise ClaudeCliError(
                f"claude -p returned non-JSON output: {e}; "
                f"stderr: {self._scrub((proc.stderr or '').strip()[:200])}"
            ) from e

        if not isinstance(data, dict):
            raise ClaudeCliError(f"claude -p returned non-object JSON: {type(data).__name__}")

        # claude can exit 0 yet flag an API error in the body — treat as failure.
        if data.get("is_error"):
            raise ClaudeCliError(
                f"claude -p reported error: {self._scrub(_extract_error(proc.stdout, proc.stderr))}"
            )

        content = (data.get("result") or "").strip()
        if not content:
            raise ClaudeCliError("claude -p returned empty result")

        # Count cache tokens too: input_tokens excludes cache_creation/cache_read,
        # which are the bulk of input on a cached subscription call (else we undercount).
        u = data.get("usage") or {}
        in_tok = (int(u.get("input_tokens", 0) or 0)
                  + int(u.get("cache_creation_input_tokens", 0) or 0)
                  + int(u.get("cache_read_input_tokens", 0) or 0))
        out_tok = int(u.get("output_tokens", 0) or 0)
        usage = Usage(prompt_tokens=in_tok, completion_tokens=out_tok, total_tokens=in_tok + out_tok)
        return ReviewResult(content=content, usage=usage, model=self.model)

    def review(self, system_prompt: str, user_prompt: str, *, thinking: str | None = None) -> ReviewResult:
        # `thinking` is accepted for interface parity with DeepSeekClient and
        # intentionally ignored — the CLI has no equivalent toggle.
        return self._invoke(build_claude_argv(self.claude_path, self.model, system_prompt), user_prompt)

    def verify(self, system_prompt: str, user_prompt: str, *, cwd: str) -> ReviewResult:
        """Agentic scoring run: read-only tools enabled, ``cwd`` a PR-head worktree so
        the model reads the real code. Same JSON envelope / usage handling as
        ``review()``; raises ``ClaudeCliError`` on any failure."""
        return self._invoke(
            build_claude_agentic_argv(self.claude_path, self.model, system_prompt), user_prompt, cwd=cwd
        )
