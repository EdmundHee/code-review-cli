import io
import sys

from rich.console import Console

from ghcr.config import load_config
from ghcr.wizard import run_wizard

ENV = {"GH_TOKEN": "t", "DEEPSEEK_API_KEY": "k"}

# One line per prompt, in wizard order. "" means "accept the shown default".
DEFAULT_ANSWERS = [
    "",           # 1  gh_path
    "",           # 2  token_env
    "reviewbot",  # 3  bot_login (required)
    "",           # 4  github timeout
    "",           # 5  api_key_env
    "",           # 6  base_url
    "",           # 7  model
    "",           # 8  thinking
    "",           # 9  reasoning_effort
    "",           # 10 deepseek timeout
    "",           # 11 input price
    "",           # 12 output price
    "owner/one",  # 13 repo
    "owner/two",  # 14 repo
    "",           # 15 repos done
    "",           # 16 poll interval
    "",           # 17 skip_drafts
    "",           # 18 review_backlog_on_start
    "",           # 19 ignore_authors
    "",           # 20 max_diff_bytes
    "",           # 21 per_run_input_token_cap
    "",           # 22 oversized_behavior
    "",           # 23 daily_usd_budget
    "",           # 24 budget_exceeded_behavior
    "",           # 25 db_path
    "",           # 26 log level
    "",           # 27 review mode
    "",           # 28 confidence threshold
    "",           # 29 scoring votes
]


def _run(path, lines, monkeypatch, env=ENV):
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(lines) + "\n"))
    console = Console(file=io.StringIO(), force_terminal=False)
    return run_wizard(str(path), env=env, console=console)


def test_wizard_writes_loadable_config(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    assert _run(p, DEFAULT_ANSWERS, monkeypatch) == 0
    cfg = load_config(str(p), env=ENV)
    assert cfg.github.bot_login == "reviewbot"
    assert cfg.repos == ("owner/one", "owner/two")
    assert cfg.deepseek.model == "deepseek-v4-pro"
    assert cfg.budgets.daily_usd_budget == 5.0
    assert cfg.diff.skip_globs  # defaults written in
    assert cfg.review.mode == "multi"  # accuracy-first default
    assert cfg.review.confidence_threshold == 80
    assert cfg.diff.test_globs  # test-coverage path heuristic defaults


def test_wizard_rejects_then_accepts_repo(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    answers = DEFAULT_ANSWERS[:12] + ["not a repo", "owner/ok", ""] + DEFAULT_ANSWERS[15:]
    assert _run(p, answers, monkeypatch) == 0
    cfg = load_config(str(p), env=ENV)
    assert cfg.repos == ("owner/ok",)


def test_wizard_backs_up_existing(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text("github:\n  bot_login: old\nrepos:\n  - a/b\n")
    assert _run(p, DEFAULT_ANSWERS, monkeypatch) == 0
    bak = tmp_path / "config.yaml.bak"
    assert bak.exists()
    assert "bot_login: old" in bak.read_text()


def test_wizard_prefills_from_existing(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text(
        "github:\n  bot_login: oldbot\n"
        "deepseek:\n  model: deepseek-custom\n"
        "repos:\n  - a/b\n"
    )
    answers = DEFAULT_ANSWERS.copy()
    answers[2] = ""  # blank bot_login -> should fall back to existing 'oldbot'
    assert _run(p, answers, monkeypatch) == 0
    cfg = load_config(str(p), env=ENV)
    assert cfg.github.bot_login == "oldbot"
    assert cfg.deepseek.model == "deepseek-custom"
