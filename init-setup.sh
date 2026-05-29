#!/usr/bin/env bash
# One-time setup for GithubCodeReview (ghcr). Idempotent — safe to re-run.
# Creates the venv, installs the package, seeds config.yaml + a chmod-600
# secrets file (only if missing), and runs the test suite.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

SECRETS_DIR="$HOME/.config/ghcr"
SECRETS_FILE="$SECRETS_DIR/secrets.env"

echo "==> Python venv"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  echo "    created .venv"
else
  echo "    .venv exists"
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Installing ghcr + dev deps"
pip install -q --upgrade pip || true
pip install -q -e ".[dev]"

echo "==> config.yaml"
if [[ ! -f config.yaml ]]; then
  cp config.example.yaml config.yaml
  echo "    created from example — EDIT repos + bot_login"
else
  echo "    exists (left untouched)"
fi

echo "==> secrets file ($SECRETS_FILE)"
mkdir -p "$SECRETS_DIR"
if [[ ! -f "$SECRETS_FILE" ]]; then
  cat > "$SECRETS_FILE" <<'EOF'
# ghcr secrets — sourced by start.sh / run-ghcr.sh. NEVER commit this file.
# Use the BOT account's fine-grained PAT (Pull requests R/W, Contents RO, Metadata RO).
export GH_TOKEN="REPLACE_WITH_BOT_PAT"
export DEEPSEEK_API_KEY="REPLACE_WITH_DEEPSEEK_KEY"
EOF
  chmod 600 "$SECRETS_FILE"
  echo "    created template (chmod 600) — EDIT with your rotated secrets"
else
  chmod 600 "$SECRETS_FILE"
  echo "    exists (left untouched; ensured chmod 600)"
fi

echo "==> Running tests"
python -m pytest -q

cat <<EOF

Setup done. Next:
  1. Edit $SECRETS_FILE with your rotated bot PAT + DeepSeek key.
  2. ./start.sh check-config                              # validate config + bot identity
  3. ./start.sh review-once --repo owner/repo --pr <N>    # dry single-PR test
  4. ./start.sh run                                       # start the polling daemon
EOF
