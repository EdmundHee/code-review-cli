#!/usr/bin/env bash
# Mimic a reboot/login and verify ghcr auto-starts. Does NOT reboot: kills the tmux
# session (as a reboot would drop the tmux server), then fires the login LaunchAgent
# the same way RunAtLoad does, and checks the daemon came back up.
#
#   ./test-autostart.sh
set -uo pipefail   # not -e: we want to run every check and report, not abort on first

LABEL="com.edmundhee.ghcr-tui"
LOG="$HOME/.local/state/ghcr/ghcr.log"
UID_="$(id -u)"

pass=0; fail=0
ok()   { echo "  PASS: $*"; pass=$((pass+1)); }
bad()  { echo "  FAIL: $*"; fail=$((fail+1)); }

echo "==> Simulate reboot: kill tmux 'ghcr' session"
tmux kill-session -t ghcr 2>/dev/null && echo "    killed" || echo "    (none running)"
base_mtime=$(stat -f %m "$LOG" 2>/dev/null || echo 0)   # log-freshness baseline

echo "==> Fire LaunchAgent like login would (RunAtLoad)"
# kickstart -k = restart the agent = re-run 'open warp://launch/ghcr-tui'.
if ! launchctl kickstart -k "gui/$UID_/$LABEL" 2>/dev/null; then
  echo "    kickstart unavailable, falling back to 'launchctl start'"
  launchctl start "$LABEL" 2>/dev/null || echo "    WARN: could not fire agent (is it loaded? run ./setup-autostart.sh)"
fi

echo "==> Wait for daemon (up to 25s)"
# ponytail: fixed 25s poll ceiling; bump if a cold Warp launch is slower on the mini.
deadline=$((SECONDS + 25))
while (( SECONDS < deadline )); do
  tmux has-session -t ghcr 2>/dev/null && break
  sleep 1
done

echo "==> Checks"
if tmux has-session -t ghcr 2>/dev/null; then ok "tmux session 'ghcr' exists"; else bad "tmux session 'ghcr' missing"; fi

cmd=$(tmux list-panes -t ghcr -F '#{pane_current_command}' 2>/dev/null | head -1)
[[ "$cmd" == python* ]] && ok "tui process running in pane ($cmd)" || bad "pane not running python (got '${cmd:-none}')"

# Daemon writes to the log on startup + each poll; a bumped mtime proves it's alive.
for _ in 1 2 3 4 5; do
  now_mtime=$(stat -f %m "$LOG" 2>/dev/null || echo 0)
  (( now_mtime > base_mtime )) && break
  sleep 2
done
(( now_mtime > base_mtime )) && ok "log advanced (daemon writing)" || bad "log stale — daemon may not have started"
tail -5 "$LOG" 2>/dev/null | grep -q 'poll' && ok "recent poll activity in log" || bad "no recent poll line in log"

echo ""
echo "==> $pass passed, $fail failed"
(( fail == 0 )) || exit 1
