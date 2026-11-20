#!/bin/bash
# The owner tier for an attached job on SLICC seven (2026-11-19, README "Attached jobs on seven"). Run by
# start_slicc.sh in attach mode when "slicc" is 7; it stays up for the length of the job.
# Usage: attach_seven.sh <job-id>
#
# The job runs in the seven open in the person's own Chrome, which nothing on this computer can reach without remote
# debugging. mcp-slicc keeps a follower seven instead: a Chrome of its own, on its own kept profile, that follows the
# person's seven through seven's follow link ($CLAUDE_SLICC_HOME/seven-follow, mode 0600) and that the person allowed
# View once in Linked devices. View makes it an owner device: it may set the approval policy and answer approvals.
#
# 1. launch that Chrome (headless) and wait until it follows with View (seven.mjs follow)
# 2. keep the policy's owner.tools as it is now (run/attach.policy.json; an earlier job's file that is still there is
#    kept, since its restore failed and it holds the person's own value), then set owner.tools "user"
# 3. mark the job owner_tier, and answer every owner-tool item with the change gate (owner_watch.py --follower) until
#    the job ends: done, failed or stopped
# 4. on the way out, whatever ended it (the job, SIGTERM from stop_slicc.sh, a failure here): owner.tools back to the
#    kept value, then the follower's Chrome closed. The person's own seven and Chrome are never touched otherwise.
#
# Without the follow link, or when the follower cannot link with View, nothing is changed in the person's seven: the
# job runs gated at the endpoint only, and its record says why (attach_tier).
set -u
HOME_DIR="${CLAUDE_SLICC_HOME:-$HOME/.claude-slicc}"
HERE="$(cd "$(dirname "$0")" && pwd -P)"
JOB="${1:?job id}"
NODE_BIN="${CLAUDE_SLICC_NODE:-node}"
SEVEN_BIN="${CLAUDE_SLICC_SEVEN_BIN:-$HOME_DIR/slicc7/node_modules/.bin/slicc}"
FOLLOW_FILE="${CLAUDE_SLICC_SEVEN_FOLLOW:-$HOME_DIR/seven-follow}"
PROFILE="${CLAUDE_SLICC_SEVEN_FOLLOWER_PROFILE:-mcp-slicc-follow}"
BROWSER="${CLAUDE_SLICC_SEVEN_BROWSER:-chrome}"
HEADLESS="${CLAUDE_SLICC_SEVEN_FOLLOWER_HEADLESS:-1}"
FOLLOW_WAIT="${CLAUDE_SLICC_SEVEN_FOLLOW_WAIT:-180}"
export SLICC_NO_TELEMETRY=1 SLICC_NO_UPDATE_CHECK=1
RUN="$HOME_DIR/run"
JD="$HOME_DIR/jobs/$JOB"
KEPT="$RUN/attach.policy.json"
LOG="$JD/attach.log"
mkdir -p "$RUN" "$JD"

say() { echo "$(date '+%Y-%m-%dT%H:%M:%S%z') $*" >>"$LOG"; }
tier() { python3 "$HERE/common.py" --attach-tier "$JOB" "$1" >/dev/null 2>&1; say "attach_tier: $1"; }
field() {
  python3 -c 'import json,sys
try: v=json.loads(sys.argv[1]).get(sys.argv[2], "")
except Exception: v=""
print(v if isinstance(v, str) else json.dumps(v))' "$1" "$2"
}

if [ ! -s "$FOLLOW_FILE" ]; then
  tier "off: no follow link ($FOLLOW_FILE), so mcp-slicc cannot set the owner tier in your seven (README: Attached jobs on seven)"
  exit 0
fi
[ -x "$SEVEN_BIN" ] || { tier "off: no slicc binary at $SEVEN_BIN"; exit 0; }

echo $$ >"$RUN/attach.pid"
CDP=""
WATCH=""
cleanup() {
  trap - EXIT
  trap '' TERM INT                # a second stop must not cut the restore short (stop_slicc.sh waits for it)
  [ -n "$WATCH" ] && kill "$WATCH" 2>/dev/null
  if [ -n "$CDP" ] && [ -s "$KEPT" ]; then
    BACK="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("owner",{}).get("tools","off"))' "$KEPT" 2>/dev/null)"
    OUT="$("$NODE_BIN" "$HERE/seven.mjs" policy --cdp "$CDP" --owner-tier "${BACK:-off}" --timeout 60 2>>"$LOG")"
    if [ "$(field "$OUT" ok)" = "true" ]; then
      rm -f "$KEPT"
      say "owner.tools back to ${BACK:-off}"
    else
      say "owner.tools NOT put back to ${BACK:-off}: $(field "$OUT" error); $KEPT kept for the next job"
    fi
  fi
  FP="$(cat "$RUN/follower.pid" 2>/dev/null)"
  if [ -n "$FP" ] && ps -o command= -p "$FP" 2>/dev/null | grep -qF -- "--profile $PROFILE "; then
    kill "$FP" 2>/dev/null       # slicc launch closes its Chrome: Browser.close, then a signal, then a kill (8 s)
    for _ in $(seq 1 100); do kill -0 "$FP" 2>/dev/null || break; sleep 0.2; done
  fi
  rm -f "$RUN/follower.pid" "$RUN/follower.cdp" "$RUN/attach.pid"
  say "follower closed"
  exit 0
}
trap cleanup EXIT TERM INT

FLAGS=(--profile "$PROFILE" --browser "$BROWSER" --json --url "https://seven.sliccy.ai/os/")
[ "$HEADLESS" = "1" ] && FLAGS+=(--headless)
EVENTS="$JD/follower.events"
"$SEVEN_BIN" launch "${FLAGS[@]}" >"$EVENTS" 2>>"$LOG" </dev/null &
echo $! >"$RUN/follower.pid"
for _ in $(seq 1 60); do
  CDP="$(field "$(grep '"event":"launched"' "$EVENTS" 2>/dev/null | tail -n 1)" cdp)"
  [ -n "$CDP" ] && break
  sleep 1
done
if [ -z "$CDP" ]; then
  tier "off: mcp-slicc's follower Chrome did not start ($(tail -n 1 "$LOG" 2>/dev/null))"
  exit 0
fi
echo "$CDP" >"$RUN/follower.cdp"

FOLLOWED="$("$NODE_BIN" "$HERE/seven.mjs" follow --cdp "$CDP" --follow-file "$FOLLOW_FILE" --timeout "$FOLLOW_WAIT" 2>>"$LOG")"
if [ "$(field "$FOLLOWED" ok)" != "true" ]; then
  tier "off: $(field "$FOLLOWED" error)"
  exit 0
fi
if [ ! -s "$KEPT" ]; then
  python3 -c 'import json,sys; json.dump({"owner": json.loads(sys.argv[1]).get("owner") or {"tools": "off"}}, open(sys.argv[2], "w"))' \
    "$FOLLOWED" "$KEPT"
fi
SET="$("$NODE_BIN" "$HERE/seven.mjs" policy --cdp "$CDP" --owner-tier user --timeout 60 2>>"$LOG")"
if [ "$(field "$SET" ok)" != "true" ]; then
  tier "off: your seven did not take owner.tools user ($(field "$SET" error))"
  exit 0
fi
python3 "$HERE/common.py" --owner-tier "$JOB" || exit 0     # the job ended meanwhile: put the policy back
tier "on: owner.tools user in your seven, answered by mcp-slicc's follower ($(field "$FOLLOWED" name)); was $(field "$(cat "$KEPT")" owner)"

python3 "$HERE/owner_watch.py" "$JOB" "$CDP" --follower >>"$JD/owner_watch.log" 2>&1 </dev/null &
WATCH=$!
wait "$WATCH"
WATCH=""
