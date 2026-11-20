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
#
# SLICC seven (2026-11-18, README "SLICC seven"), whatever "slicc" is set to now, so a job started on the other one
# is stopped too: the run is aborted (`slicc abort` over the CDP URL in run/seven.cdp), then the `slicc prompt`,
# owner_watch.py and `slicc` processes this install started, by the pids in run/ and only when their command is the
# one start_seven.sh ran. SIGTERM makes `slicc` say bye to seven and close the Chrome it launched; a Chrome still on
# mcp-slicc's own seven profile folder after that is ended. A seven you run yourself (another profile, your own Chrome)
# is never matched.
set -u
HOME_DIR="${CLAUDE_SLICC_HOME:-$HOME/.claude-slicc}"
NODE_CLI="${CLAUDE_SLICC_NODE_CLI:-$HOME_DIR/slicc/node_modules/sliccy/dist/node-server/index.js}"
PROFILE="$HOME_DIR/chrome-profile"
PIDF="$HOME_DIR/run/slicc.pid"
MATCH="$NODE_CLI --prompt "
QUIET=0
SERVERS=0
ATTACH_ONLY=0
for arg in "$@"; do
  case "$arg" in
    quiet) QUIET=1 ;;
    servers) SERVERS=1 ;;
    attach) ATTACH_ONLY=1 ;;
  esac
done

# An attached job on seven (2026-11-19): attach_seven.sh, by its pid in run/ and only when that is its command. SIGTERM
# makes it put the person's approval policy back and close mcp-slicc's follower Chrome; this waits up to 30 s for it.
# The person's own seven and Chrome are never signalled. With "attach" nothing else is stopped.
AP="$(cat "$HOME_DIR/run/attach.pid" 2>/dev/null)"
if [ -n "$AP" ] && ps -o command= -p "$AP" 2>/dev/null | grep -qF "attach_seven.sh"; then
  kill "$AP" 2>/dev/null
  for _ in $(seq 1 150); do kill -0 "$AP" 2>/dev/null || break; sleep 0.2; done
fi
if [ "$ATTACH_ONLY" = "1" ]; then
  [ "$QUIET" = "1" ] || echo "stopped"
  exit 0
fi

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

# SLICC seven.
SEVEN_BIN="${CLAUDE_SLICC_SEVEN_BIN:-$HOME_DIR/slicc7/node_modules/.bin/slicc}"
SEVEN_PROFILE="${CLAUDE_SLICC_SEVEN_PROFILE:-mcp-slicc}"
R="$HOME_DIR/run"
ours() {   # ours <pidfile> <text the command must contain>: the pid, when it runs that command
  local p
  p="$(cat "$1" 2>/dev/null)"
  [ -n "$p" ] && ps -o command= -p "$p" 2>/dev/null | grep -qF -- "$2" && echo "$p"
}
SEVEN_PID="$(ours "$R/seven.pid" "--profile $SEVEN_PROFILE ")"
if [ -n "$SEVEN_PID" ] && [ -s "$R/seven.cdp" ]; then
  SLICC_NO_TELEMETRY=1 SLICC_NO_UPDATE_CHECK=1 "$SEVEN_BIN" abort --cdp "$(cat "$R/seven.cdp")" >/dev/null 2>&1 &
  A=$!
  for _ in $(seq 1 50); do kill -0 "$A" 2>/dev/null || break; sleep 0.2; done
  kill "$A" 2>/dev/null
fi
for spec in "seven-prompt.pid:prompt --cdp" "owner_watch.pid:owner_watch.py"; do
  p="$(ours "$R/${spec%%:*}" "${spec#*:}")"
  [ -n "$p" ] && kill "$p" 2>/dev/null
done
if [ -n "$SEVEN_PID" ]; then
  kill "$SEVEN_PID" 2>/dev/null
  for _ in $(seq 1 50); do kill -0 "$SEVEN_PID" 2>/dev/null || break; sleep 0.2; done
fi
SEVEN_DIR="$(cat "$R/seven.profile" 2>/dev/null)"
case "$SEVEN_DIR" in
  */seven-profiles/"$SEVEN_PROFILE")
    for p in $(pgrep -f "seven-profiles/$SEVEN_PROFILE" 2>/dev/null); do   # exactly this folder, not a longer name
      ps -o command= -p "$p" 2>/dev/null | grep -qF -- "--user-data-dir=$SEVEN_DIR --" && kill "$p" 2>/dev/null
    done ;;
esac
rm -f "$R/seven.pid" "$R/seven-prompt.pid" "$R/owner_watch.pid" "$R/seven.cdp"

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
