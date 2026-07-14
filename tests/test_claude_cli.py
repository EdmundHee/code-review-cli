import json
import subprocess
import pytest

from ghcr.claude_cli import (
    ClaudeCliClient,
    ClaudeCliError,
    build_claude_agentic_argv,
    build_claude_argv,
)
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


def test_build_agentic_argv_enables_readonly_tools_no_wildcard_deny():
    argv = build_claude_agentic_argv("/bin/claude", "glm-4.7", "SYS")
    assert argv[argv.index("--model") + 1] == "glm-4.7"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--allowedTools") + 1] == "Read,Grep,Glob"
    # never the pure-text-generator wildcard deny (would starve the tool loop)
    disallowed = argv[argv.index("--disallowedTools") + 1]
    assert disallowed != "*"
    for t in ("Bash", "Edit", "Write"):
        assert t in disallowed
    # a reviewed repo's own .claude/ must not configure the verifier
    assert argv[argv.index("--setting-sources") + 1] == "user"


def _fake_run(stdout, returncode=0, stderr=""):
    def run(argv, input=None, capture_output=True, text=True, timeout=None, env=None, cwd=None):
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


def test_nonzero_exit_includes_stdout_json_error(monkeypatch):
    # claude -p writes API errors to STDOUT as JSON and leaves stderr empty.
    body = json.dumps({"is_error": True, "api_error_status": 429,
                       "result": "Claude usage limit reached"})
    monkeypatch.setattr(subprocess, "run", _fake_run(body, returncode=1, stderr=""))
    c = ClaudeCliClient(claude_path="/bin/claude", model="opus")
    with pytest.raises(ClaudeCliError) as ei:
        c.review("sys", "user")
    msg = str(ei.value)
    assert "429" in msg
    assert "usage limit" in msg.lower()


def test_exit0_but_is_error_raises(monkeypatch):
    body = json.dumps({"is_error": True, "api_error_status": 529, "result": "Overloaded",
                       "usage": {"input_tokens": 0, "output_tokens": 0}})
    monkeypatch.setattr(subprocess, "run", _fake_run(body, returncode=0))
    c = ClaudeCliClient(claude_path="/bin/claude", model="opus")
    with pytest.raises(ClaudeCliError) as ei:
        c.review("sys", "user")
    assert "529" in str(ei.value)


def test_usage_counts_cache_tokens(monkeypatch):
    body = json.dumps({"result": "ok", "usage": {
        "input_tokens": 5283, "cache_creation_input_tokens": 7314,
        "cache_read_input_tokens": 100, "output_tokens": 30}})
    monkeypatch.setattr(subprocess, "run", _fake_run(body))
    c = ClaudeCliClient(claude_path="/bin/claude", model="opus")
    r = c.review("sys", "user")
    assert r.usage.prompt_tokens == 5283 + 7314 + 100
    assert r.usage.completion_tokens == 30
    assert r.usage.total_tokens == 5283 + 7314 + 100 + 30


def test_timeout_raises_claude_error(monkeypatch):
    def run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
    monkeypatch.setattr(subprocess, "run", run)
    c = ClaudeCliClient(claude_path="/bin/claude", model="opus")
    with pytest.raises(ClaudeCliError):
        c.review("sys", "user")


def _capturing_run(seen, stdout, returncode=0, stderr=""):
    def run(argv, input=None, capture_output=True, text=True, timeout=None, env=None, cwd=None):
        seen["argv"] = argv
        seen["env"] = env
        seen["cwd"] = cwd
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)
    return run


def test_verify_passes_cwd_and_agentic_argv(monkeypatch):
    seen = {}
    payload = json.dumps({"result": '{"confidence": 90, "reason": "read the code"}',
                          "usage": {"input_tokens": 10, "output_tokens": 5}})
    monkeypatch.setattr(subprocess, "run", _capturing_run(seen, payload))
    c = ClaudeCliClient(claude_path="/bin/claude", model="glm-4.7")
    r = c.verify("sys", "user", cwd="/tmp/wt")
    assert seen["cwd"] == "/tmp/wt"
    assert "--allowedTools" in seen["argv"]
    assert "90" in r.content
    assert seen["env"] is None  # no override → inherit (subscription auth)


def test_verify_env_carries_base_url_and_token_not_argv(monkeypatch):
    seen = {}
    payload = json.dumps({"result": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}})
    monkeypatch.setattr(subprocess, "run", _capturing_run(seen, payload))
    c = ClaudeCliClient(claude_path="/bin/claude", model="glm-4.7",
                        base_url="https://api.z.ai/api/anthropic", auth_token="ZSECRET")
    c.verify("sys", "user", cwd="/tmp/wt")
    assert seen["env"]["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
    assert seen["env"]["ANTHROPIC_AUTH_TOKEN"] == "ZSECRET"
    assert "ZSECRET" not in " ".join(seen["argv"])  # never in argv


def test_verify_scrubs_token_from_error(monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "run",
                        _capturing_run(seen, "", returncode=1, stderr="auth failed for ZSECRET"))
    c = ClaudeCliClient(claude_path="/bin/claude", model="glm-4.7",
                        base_url="https://api.z.ai/api/anthropic", auth_token="ZSECRET")
    with pytest.raises(ClaudeCliError) as ei:
        c.verify("sys", "user", cwd="/tmp/wt")
    assert "ZSECRET" not in str(ei.value)
    assert "***" in str(ei.value)
