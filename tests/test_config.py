import dataclasses

import pytest

from ghcr.config import ConfigError, load_config, merge_reloadable
from tests.fakes import make_config

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


# -- review section ----------------------------------------------------------

def test_review_defaults_to_multi(tmp_path):
    cfg = load_config(_write(tmp_path, VALID), env={}, resolve_secrets=False)
    assert cfg.review.mode == "multi"
    assert cfg.review.confidence_threshold == 80
    assert cfg.review.scoring_votes == 1
    assert set(cfg.review.lenses) == {"correctness", "security", "maintainability", "test_coverage"}
    assert cfg.diff.test_globs  # defaults applied


def test_review_parsed_and_clamped(tmp_path):
    text = VALID + (
        "review:\n"
        "  mode: multi\n"
        "  confidence_threshold: 250\n"
        "  scoring_votes: 3\n"
        "  lenses:\n"
        "    - correctness\n"
        "    - security\n"
    )
    cfg = load_config(_write(tmp_path, text), env={}, resolve_secrets=False)
    assert cfg.review.confidence_threshold == 100  # clamped
    assert cfg.review.scoring_votes == 3
    assert cfg.review.lenses == ("correctness", "security")


def test_review_rejects_bad_mode(tmp_path):
    text = VALID + "review:\n  mode: turbo\n"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, text), env={}, resolve_secrets=False)


def test_review_rejects_unknown_lens(tmp_path):
    text = VALID + "review:\n  lenses:\n    - vibes\n"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, text), env={}, resolve_secrets=False)


def test_review_rejects_zero_votes(tmp_path):
    text = VALID + "review:\n  scoring_votes: 0\n"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, text), env={}, resolve_secrets=False)


def test_review_prior_comment_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, VALID), env={}, resolve_secrets=False)
    assert cfg.review.read_prior_comments is True
    assert cfg.review.prior_comment_max_chars == 6000


def test_review_prior_comments_parsed_and_clamped(tmp_path):
    text = VALID + (
        "review:\n"
        "  read_prior_comments: false\n"
        "  prior_comment_max_chars: -5\n"
    )
    cfg = load_config(_write(tmp_path, text), env={}, resolve_secrets=False)
    assert cfg.review.read_prior_comments is False
    assert cfg.review.prior_comment_max_chars == 0  # clamped to >= 0


def test_review_rereview_on_mention_default_and_parsed(tmp_path):
    cfg = load_config(_write(tmp_path, VALID), env={}, resolve_secrets=False)
    assert cfg.review.rereview_on_mention is True  # default on
    text = VALID + "review:\n  rereview_on_mention: false\n"
    cfg2 = load_config(_write(tmp_path, text), env={}, resolve_secrets=False)
    assert cfg2.review.rereview_on_mention is False


# -- merge_reloadable (hot-reload) -------------------------------------------

def test_merge_swaps_safe_fields_no_restart(tmp_path):
    old = make_config(db_path=str(tmp_path / "db"))
    new = dataclasses.replace(
        old,
        repos=("owner/repo", "owner/added"),
        poll_interval_seconds=300,
        budgets=dataclasses.replace(old.budgets, daily_usd_budget=10.0),
    )
    new = dataclasses.replace(new, review=dataclasses.replace(old.review, confidence_threshold=90))
    merged, restart = merge_reloadable(old, new)
    assert merged.repos == ("owner/repo", "owner/added")
    assert merged.poll_interval_seconds == 300
    assert merged.budgets.daily_usd_budget == 10.0
    assert merged.review.confidence_threshold == 90  # review is hot-reloadable
    assert restart == []


def test_merge_preserves_restart_only_fields(tmp_path):
    old = make_config(db_path=str(tmp_path / "db"), bot_login="reviewbot", model="deepseek-v4-pro")
    new = make_config(db_path=str(tmp_path / "other"), bot_login="otherbot", model="deepseek-v5")
    new = dataclasses.replace(new, repos=("owner/added",))
    merged, restart = merge_reloadable(old, new)
    # safe field applied
    assert merged.repos == ("owner/added",)
    # restart-only fields kept from old
    assert merged.github.bot_login == "reviewbot"
    assert merged.deepseek.model == "deepseek-v4-pro"
    assert merged.db_path == str(tmp_path / "db")
    # and reported
    assert set(restart) == {"github", "deepseek", "db_path"}
