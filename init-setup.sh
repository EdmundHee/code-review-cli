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
# Interpreter is explicit, not ambient. Honors .python-version (pyenv shim);
# override with: PYTHON=/path/to/python3 bash init-setup.sh
PYTHON="${PYTHON:-python3}"
if [[ ! -d .venv ]]; then
  "$PYTHON" -m venv .venv || { echo "ERROR: venv creation failed with '$PYTHON'." >&2; exit 1; }
  echo "    created .venv ($("$PYTHON" --version 2>&1))"
else
  echo "    .venv exists"
fi
VENV_PY=".venv/bin/python"
# Fail fast if the venv's Python has a broken stdlib (e.g. Homebrew 3.14 pyexpat/
# expat symbol mismatch). Catches a broken base interpreter AND a stale bad .venv.
if ! "$VENV_PY" -c "import pyexpat" 2>/dev/null; then
  echo "ERROR: .venv Python broken (pyexpat won't load) — built on a broken interpreter" >&2
  echo "       (e.g. Homebrew 3.14 expat mismatch). Fix:" >&2
  echo "         pyenv local 3.12.1   # or: PYTHON=/path/to/good/python3" >&2
  echo "         rm -rf .venv && bash init-setup.sh" >&2
  exit 1
fi

echo "==> Installing ghcr + dev deps"
"$VENV_PY" -m pip install -q --upgrade pip || true
"$VENV_PY" -m pip install -q -e ".[dev]"

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
"$VENV_PY" -m pytest -q

cat <<EOF

Setup done. Next:
  1. Edit $SECRETS_FILE with your rotated bot PAT + DeepSeek key.
  2. ./start.sh check-config                              # validate config + bot identity
  3. ./start.sh review-once --repo owner/repo --pr <N>    # dry single-PR test
  4. ./start.sh run                                       # start the polling daemon
EOF
