#!/bin/bash
# Stops the SLICC that mcp-slicc started, and the Chrome it launched (2026-11-09; narrowed 2026-11-10).
# Set as CLAUDE_SLICC_STOP_CMD="<path>/stop_slicc.sh". Usage: stop_slicc.sh [quiet] [servers]
#
# Only mcp-slicc's own processes are touched:
#   * the node CLI from mcp-slicc's own install, as start_slicc.sh launches it ("<install path> --prompt ...")
#   * the Chrome on that SLICC's debug port, and only when that SLICC was running and has just been stopped
#   * Chrome on mcp-slicc's own profile folder ($CLAUDE_SLICC_HOME/chrome-profile)
#   * with "servers": the endpoint and the UI too, whatever folder they run from: the process listening on the
#     endpoint's port whose command runs endpoint.py, and the one on the UI's port running ui_server.py.
#     `start_slicc.sh servers` starts them again
# A SLICC you run yourself (npx sliccy, its default profile in ~/Library/Application Support/Slicc) is never matched.
set -u
HOME_DIR="${CLAUDE_SLICC_HOME:-$HOME/.claude-slicc}"
NODE_CLI="${CLAUDE_SLICC_NODE_CLI:-$HOME_DIR/slicc/node_modules/sliccy/dist/node-server/index.js}"
PROFILE="$HOME_DIR/chrome-profile"
PIDF="$HOME_DIR/run/slicc.pid"
MATCH="$NODE_CLI --prompt "
QUIET=0
SERVERS=0
for arg in "$@"; do
  case "$arg" in
    quiet) QUIET=1 ;;
    servers) SERVERS=1 ;;
  esac
done

# The debug port of the Chrome this SLICC launched, read from its own log before anything is stopped.
JOB="$(cat "$HOME_DIR/run/slicc.job" 2>/dev/null)"
LOG="$HOME_DIR/jobs/$JOB/slicc.log"
if [ -z "$JOB" ] || [ ! -f "$LOG" ]; then
  LOG="$(ls -t "$HOME_DIR"/jobs/*/slicc.log 2>/dev/null | head -n 1)"
fi
CDP_PORT="$(grep -o 'Chrome CDP listening on port [0-9]*' "${LOG:-/dev/null}" 2>/dev/null | tail -n 1 | grep -o '[0-9]*$')"

# SIGTERM: SLICC closes the Chrome it launched itself (Browser.close over its debug port, up to 3 s).
STOPPED=0
for p in $(cat "$PIDF" 2>/dev/null) $(pgrep -f "$MATCH" 2>/dev/null); do
  if ps -o command= -p "$p" 2>/dev/null | grep -qF "$MATCH"; then
    kill "$p" 2>/dev/null && STOPPED=1
  fi
done
rm -f "$PIDF"
if [ "$STOPPED" = "1" ]; then
  for _ in $(seq 1 25); do
    pgrep -f "$MATCH" >/dev/null 2>&1 || break
    sleep 0.2
  done
  # A Chrome that ignored the close: the one still listening on the debug port of the SLICC just stopped.
  if [ -n "$CDP_PORT" ]; then
    for p in $(lsof -nP -iTCP:"$CDP_PORT" -sTCP:LISTEN -t 2>/dev/null); do
      if ps -o command= -p "$p" 2>/dev/null | grep -q "Google Chrome"; then
        kill "$p" 2>/dev/null
      fi
    done
  fi
fi
pkill -f "user-data-dir=$PROFILE" 2>/dev/null

# The process listening on a port, when its command runs the named script; waits up to 5 s for it to end.
stop_server() {
  local port="$1" pattern="$2" p
  for p in $(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null); do
    if ps -o command= -p "$p" 2>/dev/null | grep -qE "(^|[ /])$pattern\$"; then
      kill "$p" 2>/dev/null
      for _ in $(seq 1 25); do
        kill -0 "$p" 2>/dev/null || break
        sleep 0.2
      done
    fi
  done
}
if [ "$SERVERS" = "1" ]; then
  stop_server "${CLAUDE_SLICC_PORT:-8660}" 'endpoint\.py'
  stop_server "${CLAUDE_SLICC_UI_PORT:-5711}" 'ui_server\.py'
fi
[ "$QUIET" = "1" ] || echo "stopped"
