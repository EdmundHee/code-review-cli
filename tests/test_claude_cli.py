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
    monkeypatch.setattr(subprocess, "run", _fake_run("", returncode=1))
    c = ClaudeCliClient(claude_path="/bin/claude", model="opus")
    with pytest.raises(DeepSeekError):
        c.review("sys", "user")


def test_non_object_json_raises_claude_error(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("[]"))
    c = ClaudeCliClient(claude_path="/bin/claude", model="opus")
    with pytest.raises(ClaudeCliError):
        c.review("sys", "user")


def test_timeout_raises_claude_error(monkeypatch):
    def run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
    monkeypatch.setattr(subprocess, "run", run)
    c = ClaudeCliClient(claude_path="/bin/claude", model="opus")
    with pytest.raises(ClaudeCliError):
        c.review("sys", "user")
