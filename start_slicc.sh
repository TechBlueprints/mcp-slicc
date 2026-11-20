#!/bin/bash
# Starts SLICC for one mcp-slicc job (2026-11-09; own profile folder and endpoint version check, 2026-11-10).
# Set as CLAUDE_SLICC_START_CMD="<path>/start_slicc.sh {job} {prompt}".
# Usage: start_slicc.sh <job-id> <prompt> [attach]    or    start_slicc.sh servers
#
# With "attach" (an attached job, 2026-11-10) only steps 1 and 2 run, without the UI: nothing is launched and no
# profile is touched. The job runs in the SLICC that is already open in the person's own Chrome. With "slicc": 7
# (2026-11-19) attach_seven.sh then runs for the length of the job: mcp-slicc's follower seven sets the owner tier in
# the person's seven and answers it, and puts the policy back when the job ends (README "Attached jobs on seven").
# With "servers" only step 2 runs, UI included, and nothing else is started or stopped: after `stop_slicc.sh servers`
# it brings the endpoint and the UI back up at the code on disk. Run it when no job is running.
#
# 1. the SLICC of an earlier mcp-slicc job is stopped (stop_slicc.sh: mcp-slicc's own processes only)
# 2. the endpoint (8660) and the local UI (5711) run. An endpoint that runs older code than the files on disk is
#    restarted here, where no job can be in flight (slicc_task refuses to start while one runs)
# 3. mcp-slicc's own Chrome profile is wiped, so every job starts fresh (a job must never see an earlier job's chat).
#    The profile is $CLAUDE_SLICC_HOME/chrome-profile, set through SLICC_USER_DATA_DIR. SLICC's default profile
#    (~/Library/Application Support/Slicc) and ~/.slicc belong to a SLICC you run yourself and are never deleted
# 4. SLICC starts with --prompt, and its Chrome opens the local UI, which seeds the endpoint and telemetry-off
#
# With "slicc": 7 in config.json (2026-11-18, README "SLICC seven"), steps 3 and 4 are seven's instead, and the local UI
# is not started:
# 3. mcp-slicc's own seven profile is wiped (only "<...>/seven-profiles/<profile>", the folder `slicc` itself reports)
# 4. `slicc` launches Chrome on it, opens seven and links it, with the narrowest link it offers: no kernel services, no
#    slicc-jerk, no SSH runner, no host folders, and loopback and the LAN refused except the endpoint's port
# 5. seven.mjs seeds the page over CDP: the keyless `local` account on this endpoint, the model, and the approval policy
#    owner.tools "user", which owner_watch.py answers with the change gate. A mismatch of versions, or a step that
#    fails, ends the job with one line
# 6. `slicc prompt` sends the launch prompt; the job's events go to jobs/<job>/slicc.log, the CDP URL to run/seven.cdp
set -u
HOME_DIR="${CLAUDE_SLICC_HOME:-$HOME/.claude-slicc}"
HERE="$(cd "$(dirname "$0")" && pwd -P)"   # the real folder, also when this script is run through a symlink
PORT="${CLAUDE_SLICC_PORT:-8660}"
UI_PORT="${CLAUDE_SLICC_UI_PORT:-5711}"
if [ "${1:-}" = "servers" ]; then
  JOB=""
  PROMPT=""
  MODE="servers"
else
  JOB="${1:?job id}"
  PROMPT="${2:?prompt}"
  MODE="${3:-launch}"
fi
NODE_BIN="${CLAUDE_SLICC_NODE:-node}"
VERSION="$(python3 "$HERE/common.py" --slicc-version 2>/dev/null)"
NODE_CLI="${CLAUDE_SLICC_NODE_CLI:-$HOME_DIR/slicc/node_modules/sliccy/dist/node-server/index.js}"
PROFILE="$HOME_DIR/chrome-profile"

mkdir -p "$HOME_DIR/run"
if [ "$MODE" != "servers" ]; then
  mkdir -p "$HOME_DIR/jobs/$JOB"
  "$HERE/stop_slicc.sh" quiet
fi

endpoint_up() {
  curl -sf -m 3 -H "Authorization: Bearer $(cat "$HOME_DIR/token" 2>/dev/null)" "http://127.0.0.1:$PORT/v1/models"
}

if [ "${CLAUDE_SLICC_NO_SERVERS:-0}" != "1" ]; then
  WANT="$(python3 "$HERE/common.py" --code-version 2>/dev/null)"
  HAVE="$(endpoint_up | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0].get("version", ""))' 2>/dev/null)"
  if [ -z "$WANT" ] || [ "$HAVE" != "$WANT" ]; then
    # The endpoint on this port only, whatever folder it was started from (an older checkout, a symlinked path)
    for p in $(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null); do
      if ps -o command= -p "$p" 2>/dev/null | grep -qE '(^|[ /])endpoint\.py$'; then
        kill "$p" 2>/dev/null && sleep 1
      fi
    done
    nohup python3 "$HERE/endpoint.py" >>"$HOME_DIR/endpoint.out" 2>&1 &
    for _ in $(seq 1 20); do
      endpoint_up >/dev/null && break
      sleep 0.5
    done
  fi
  if [ "$MODE" != "attach" ] && [ "$VERSION" = "6" ] && ! curl -sf -m 3 -o /dev/null "http://127.0.0.1:$UI_PORT/"; then
    nohup python3 "$HERE/ui_server.py" >>"$HOME_DIR/ui.out" 2>&1 &
    sleep 1
  fi
fi

if [ "$MODE" = "servers" ]; then
  echo "endpoint and UI checked: $HERE"
  exit 0
fi
if [ "$MODE" = "attach" ]; then
  if [ "$VERSION" = "7" ]; then
    nohup "$HERE/attach_seven.sh" "$JOB" >/dev/null 2>&1 </dev/null &
    echo "attached job $JOB: nothing launched; the owner tier through mcp-slicc's follower (jobs/$JOB/attach.log)"
  else
    echo "attached job $JOB: nothing launched"
  fi
  exit 0
fi

if [ "$VERSION" = "7" ]; then
  exec "$HERE/start_seven.sh" "$JOB" "$PROMPT"
fi
if [ "$VERSION" != "6" ]; then
  python3 "$HERE/common.py" --fail-job "$JOB" "(launcher) slicc \"$VERSION\" is not 6 or 7 (mcp-slicc config)"
  exit 1
fi

case "$PROFILE" in
  */chrome-profile) rm -rf "$PROFILE" ;;
esac
echo "$JOB" >"$HOME_DIR/run/slicc.job"

SLICC_USER_DATA_DIR="$PROFILE" WORKER_BASE_URL="http://localhost:$UI_PORT" \
  BRIDGE_DEV_ALLOWED_ORIGINS="http://localhost:$UI_PORT" \
  nohup "$NODE_BIN" "$NODE_CLI" --prompt "$PROMPT" >"$HOME_DIR/jobs/$JOB/slicc.log" 2>&1 &
echo $! >"$HOME_DIR/run/slicc.pid"
echo "SLICC started for $JOB (pid $!, log $HOME_DIR/jobs/$JOB/slicc.log)"
