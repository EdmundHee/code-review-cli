#!/usr/bin/env bash
# launchd wrapper: load secrets from a chmod-600 env file, then exec the daemon.
# Keeps GH_TOKEN / DEEPSEEK_API_KEY out of the (world-readable) plist and the YAML.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRETS="${GHCR_SECRETS:-$HOME/.config/ghcr/secrets.env}"

if [[ -f "$SECRETS" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$SECRETS"
  set +a
fi

exec "$HERE/.venv/bin/python" -m ghcr.cli --config "$HERE/config.yaml" run
