# GithubCodeReview (`ghcr`)

A local daemon that watches a configured list of GitHub repos, sends each new
PR commit's diff to the **DeepSeek API** (`deepseek-v4-pro`), and posts the AI
review as a comment using a **dedicated bot GitHub account**.

> ⚠️ **Your diffs leave your machine.** Each reviewed PR's filtered diff is sent
> to DeepSeek (a third-party API). Only watch repos you are cleared to share.
> The `skip_globs` reduce accidental transmission of lockfiles/binaries/generated
> code, but review the policy before pointing this at private code.

## How it works

- Polls `gh pr list` per repo every `interval_seconds`.
- Reviews each PR on open **and on every new push**, deduped by head SHA (each
  unique commit reviewed once). State lives in SQLite.
- Posts one **summary comment per reviewed commit** with a hidden marker.
- Reads the PR's **existing comments** (its own earlier reviews + human remarks,
  timeline + inline) first, so it won't re-raise issues already raised and stays
  aware of the discussion. Toggle with `review.read_prior_comments` (default on).
- **@mention to re-review:** comment `@<bot_login>` on a PR and the bot runs a fresh
  review on the next poll — no new commit needed (toggle `review.rereview_on_mention`,
  default on). Otherwise it only reviews on a new commit (deduped by head SHA).
- **Claude Opus advisor (optional):** set `review.advisor_provider: claude` to route the
  planner + scoring passes to Claude Opus via the `claude -p` CLI — using your Claude
  **subscription** (not API credits) for cross-model verification and lower DeepSeek spend.
  Lenses stay on DeepSeek. Needs a logged-in `claude` CLI and the `claude:` config block;
  restart to apply. Default off (DeepSeek does everything).
- Skips drafts, ignored authors, and its own PRs; skips noisy files and caps
  diff size + a rolling 24h USD budget.

Pure CLI — nothing listens on a port; it reaches out via `gh` + DeepSeek on a timer.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp config.example.yaml config.yaml   # then edit repos + bot_login
```

Secrets come from env (never the YAML):

```bash
export GH_TOKEN="<bot account fine-grained PAT>"   # NOT your personal token
export DEEPSEEK_API_KEY="<deepseek key>"
```

**Bot PAT scopes (fine-grained, only the watched repos):** Pull requests R/W,
Contents RO, Metadata RO.

## Commands

```bash
ghcr --config config.yaml check-config              # validate config + bot identity
ghcr --config config.yaml run                       # the polling daemon (foreground)
ghcr --config config.yaml review-once --repo o/r --pr 1   # one-shot, for testing
ghcr --config config.yaml status                    # recent reviews + 24h spend
```

`run` verifies on startup that `gh` is authenticated **as `bot_login`** and
refuses to run otherwise — so it can never post as your personal account.

## Always-on (macOS launchd)

1. Put secrets in `~/.config/ghcr/secrets.env` (`chmod 600`) as `export VAR=...`.
2. Copy `com.edmundhee.ghcr.plist` to `~/Library/LaunchAgents/`, fix paths.
3. `launchctl load ~/Library/LaunchAgents/com.edmundhee.ghcr.plist`

## Tests

```bash
pytest
```
