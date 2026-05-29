#!/usr/bin/env bash
# Start ghcr. Sources secrets, activates the venv, then runs a subcommand.
# Usage: ./start.sh [run|check-config|status|review-once --repo o/r --pr N]
# Default (no args) runs the polling daemon.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

SECRETS_FILE="${GHCR_SECRETS:-$HOME/.config/ghcr/secrets.env}"

if [[ ! -d .venv ]]; then
  echo "no .venv — run ./init-setup.sh first" >&2
  exit 1
fi
if [[ ! -f "$SECRETS_FILE" ]]; then
  echo "no secrets file at $SECRETS_FILE — run ./init-setup.sh, then edit it" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$SECRETS_FILE"
set +a

if [[ "${GH_TOKEN:-}" == REPLACE_* || "${DEEPSEEK_API_KEY:-}" == REPLACE_* || -z "${GH_TOKEN:-}" || -z "${DEEPSEEK_API_KEY:-}" ]]; then
  echo "secrets unset or still placeholders — edit $SECRETS_FILE" >&2
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate
exec ghcr --config config.yaml "${@:-run}"
