import pytest

from ghcr.config import ConfigError, load_config

VALID = """\
github:
  bot_login: reviewbot
  gh_path: /opt/homebrew/bin/gh
deepseek:
  model: deepseek-v4-pro
repos:
  - owner/repo-one
  - owner/repo-two
diff:
  oversized_behavior: notice
budgets:
  daily_usd_budget: 5.0
"""


def _write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return str(p)


def test_loads_valid_with_secrets(tmp_path):
    env = {"GH_TOKEN": "tok", "DEEPSEEK_API_KEY": "key"}
    cfg = load_config(_write(tmp_path, VALID), env=env)
    assert cfg.github.token == "tok"
    assert cfg.deepseek.api_key == "key"
    assert cfg.repos == ("owner/repo-one", "owner/repo-two")
    assert cfg.review_policy.skip_drafts is True
    assert cfg.review_policy.review_backlog_on_start is False
    assert cfg.diff.max_diff_bytes == 400_000  # default applied


def test_missing_env_fails_fast(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, VALID), env={"DEEPSEEK_API_KEY": "key"})


def test_resolve_secrets_false_skips_env(tmp_path):
    cfg = load_config(_write(tmp_path, VALID), env={}, resolve_secrets=False)
    assert cfg.github.token == ""


def test_rejects_bad_repo(tmp_path):
    bad = VALID.replace("owner/repo-one", "not-a-repo")
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, bad), env={}, resolve_secrets=False)


def test_requires_repos(tmp_path):
    text = "github:\n  bot_login: x\ndeepseek: {}\nrepos: []\n"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, text), env={}, resolve_secrets=False)


def test_requires_bot_login(tmp_path):
    text = "github: {}\nrepos:\n  - o/r\n"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, text), env={}, resolve_secrets=False)


def test_bad_behavior_rejected(tmp_path):
    bad = VALID.replace("oversized_behavior: notice", "oversized_behavior: explode")
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, bad), env={}, resolve_secrets=False)
