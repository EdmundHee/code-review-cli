#!/usr/bin/env bash
# Unregister ghcr auto-start: unload the login LaunchAgent, stop the daemon, and
# remove the two config files. Idempotent — safe if nothing is installed.
# Leaves the repo, .venv, config.yaml, secrets, and SQLite state untouched.
#
#   ./teardown-autostart.sh
set -uo pipefail

LABEL="com.edmundhee.ghcr-tui"
WARP_CFG="$HOME/.warp/launch_configurations/ghcr-tui.yaml"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "==> Unload LaunchAgent"
if [[ -f "$PLIST" ]]; then
  launchctl unload "$PLIST" 2>/dev/null && echo "    unloaded" || echo "    (was not loaded)"
else
  echo "    (no plist)"
fi

echo "==> Stop daemon (kill tmux 'ghcr' session)"
tmux kill-session -t ghcr 2>/dev/null && echo "    stopped" || echo "    (not running)"

echo "==> Remove config files"
rm -f "$PLIST"    && echo "    removed $PLIST"
rm -f "$WARP_CFG" && echo "    removed $WARP_CFG"

cat <<EOF

Auto-start unregistered. Repo, venv, config.yaml, secrets, and SQLite state kept.
Re-enable: ./setup-autostart.sh
EOF
