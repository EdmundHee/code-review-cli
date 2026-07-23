#!/usr/bin/env bash
# One-shot: wire ghcr TUI to auto-start in a detached tmux session, opened by Warp
# at login (LaunchAgent). Idempotent — safe to re-run. Run this ON the target Mac.
#
#   ./setup-autostart.sh
#
# Writes 2 files (Warp launch config + login LaunchAgent), loads the agent, which
# (RunAtLoad) immediately opens Warp on the tmux session. Paths are derived from
# THIS script's location, so it works regardless of username / clone path.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.edmundhee.ghcr-tui"
WARP_CFG="$HOME/.warp/launch_configurations/ghcr-tui.yaml"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
STATE_DIR="$HOME/.local/state/ghcr"
SECRETS="${GHCR_SECRETS:-$HOME/.config/ghcr/secrets.env}"

fail() { echo "ERROR: $*" >&2; exit 1; }

echo "==> Preflight"
command -v tmux >/dev/null || fail "tmux not on PATH — 'brew install tmux'"
[[ -d /Applications/Warp.app ]] || fail "Warp not in /Applications — install from warp.dev"

# .venv is the app itself; build it via the repo's own idempotent setup if absent.
if [[ ! -d "$REPO_DIR/.venv" ]]; then
  echo "    no .venv — running init-setup.sh"
  ( cd "$REPO_DIR" && ./init-setup.sh )
fi

[[ -f "$REPO_DIR/config.yaml" ]] || fail "no $REPO_DIR/config.yaml — scp it from the other machine (gitignored)"
[[ -f "$SECRETS" ]]            || fail "no $SECRETS — scp ~/.config/ghcr from the other machine"
if grep -q 'REPLACE_' "$SECRETS"; then
  fail "$SECRETS still has REPLACE_ placeholders — edit it with real secrets first"
fi
grep -q 'ZAI_API_KEY' "$SECRETS" || echo "    WARN: no ZAI_API_KEY in secrets — GLM advisor will degrade (reviews still run)"
echo "    ok (repo=$REPO_DIR)"

echo "==> Warp launch config ($WARP_CFG)"
mkdir -p "$(dirname "$WARP_CFG")"
cat > "$WARP_CFG" <<EOF
---
name: ghcr-tui
windows:
  - tabs:
      - title: ghcr
        layout:
          cwd: $REPO_DIR
          commands:
            - exec: tmux new-session -A -s ghcr $REPO_DIR/start.sh tui
EOF

echo "==> LaunchAgent ($PLIST)"
mkdir -p "$(dirname "$PLIST")"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/open</string>
    <string>warp://launch/ghcr-tui</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
EOF

mkdir -p "$STATE_DIR"

echo "==> Loading LaunchAgent (RunAtLoad fires it now)"
launchctl unload "$PLIST" 2>/dev/null || true   # idempotent: drop any prior copy first
launchctl load "$PLIST"

cat <<EOF

Done. Warp should have opened on the tmux 'ghcr' session running the TUI.
  - detach (leave daemon running):  Ctrl-b then d
  - reattach later:                 tmux attach -t ghcr
  - verify / mimic reboot:          ./test-autostart.sh
  - disable auto-start:             launchctl unload "$PLIST"
  - stop the daemon entirely:       tmux kill-session -t ghcr

CAUTION: do not run ghcr on another machine against the same repos (double reviews,
double cost). Kill it there first.
EOF
