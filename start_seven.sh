#!/bin/bash
# Starts SLICC seven for one mcp-slicc launcher job (2026-11-18, README "SLICC seven"). Run by start_slicc.sh when
# config.json has "slicc": 7, after the SLICC of an earlier job was stopped and the endpoint checked.
# Usage: start_seven.sh <job-id> <prompt>
#
# Every step that fails ends the job with one line (common.py --fail-job) and stops what this script started.
set -u
HOME_DIR="${CLAUDE_SLICC_HOME:-$HOME/.claude-slicc}"
HERE="$(cd "$(dirname "$0")" && pwd -P)"
JOB="${1:?job id}"
PROMPT="${2:?prompt}"
PORT="${CLAUDE_SLICC_PORT:-8660}"
NODE_BIN="${CLAUDE_SLICC_NODE:-node}"
SEVEN_BIN="${CLAUDE_SLICC_SEVEN_BIN:-$HOME_DIR/slicc7/node_modules/.bin/slicc}"
PROFILE="${CLAUDE_SLICC_SEVEN_PROFILE:-mcp-slicc}"
BROWSER="${CLAUDE_SLICC_SEVEN_BROWSER:-chrome}"
OWNER_TIER="${CLAUDE_SLICC_OWNER_TIER:-user}"          # user: owner_watch.py gates every tool call; off: codemode stays stopped
LINK_WAIT="${CLAUDE_SLICC_SEVEN_LINK_WAIT:-180}"
SEED_WAIT="${CLAUDE_SLICC_SEVEN_SEED_WAIT:-300}"
CLI_CONTRACT=1                                          # `slicc capabilities`: the contract this script was written for
CLI_MAJOR=1                                             # slicc-cli 1.x (sliccy@next 7.0.0-next.1.69.1 ships 1.69.1)
export SLICC_NO_TELEMETRY=1 SLICC_NO_UPDATE_CHECK=1
RUN="$HOME_DIR/run"
JD="$HOME_DIR/jobs/$JOB"
mkdir -p "$RUN" "$JD"

fail() {
  python3 "$HERE/common.py" --fail-job "$JOB" "(launcher) SLICC seven did not start: $1" >/dev/null 2>&1
  echo "SLICC seven did not start for $JOB: $1" >&2
  "$HERE/stop_slicc.sh" quiet
  exit 1
}
field() {   # field <json> <key>: one top-level value of a JSON object, "" when absent
  python3 -c 'import json,sys
try: v=json.loads(sys.argv[1]).get(sys.argv[2], "")
except Exception: v=""
print(v if isinstance(v, str) else json.dumps(v))' "$1" "$2"
}

[ -x "$SEVEN_BIN" ] || fail "no slicc binary at $SEVEN_BIN (README: SLICC seven, install)"
command -v "$NODE_BIN" >/dev/null 2>&1 || fail "no node for seven.mjs ($NODE_BIN)"

# The CLI's contract and version: a newer CLI with another contract is refused here, not half-run.
CAPS="$("$SEVEN_BIN" capabilities --json 2>/dev/null)"
HAVE_CONTRACT="$(field "$CAPS" contract)"
HAVE_VERSION="$(field "$CAPS" version)"
if [ "$HAVE_CONTRACT" != "$CLI_CONTRACT" ] || [ "${HAVE_VERSION%%.*}" != "$CLI_MAJOR" ]; then
  fail "slicc ${HAVE_VERSION:-?} offers contract ${HAVE_CONTRACT:-?}; mcp-slicc is built for slicc $CLI_MAJOR.x, contract $CLI_CONTRACT"
fi

# A fresh profile for every job: nothing of an earlier job (its chat, files, memory, accounts) is left.
PDIR="$(cat "$RUN/seven.profile" 2>/dev/null)"
if [ -z "$PDIR" ]; then
  case "$(uname)" in
    Darwin) PDIR="$HOME/Library/Application Support/slicc/seven-profiles/$PROFILE" ;;
  esac
fi
case "$PDIR" in
  */seven-profiles/"$PROFILE") rm -rf "$PDIR" ;;
esac

echo "$JOB" >"$RUN/slicc.job"
EVENTS="$JD/seven.events"
: >"$EVENTS"
nohup "$SEVEN_BIN" --profile "$PROFILE" --browser "$BROWSER" --json --yes --no-fallback --no-kernel --no-jerk \
  --deny 127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7 --deny "[::1]/128" \
  --allow "127.0.0.1/32:$PORT" >"$EVENTS" 2>"$JD/seven.err" </dev/null &
echo $! >"$RUN/seven.pid"

# The link: `slicc` prints "launched" (with the CDP URL and the profile folder), then "linked".
CDP=""
for _ in $(seq 1 "$LINK_WAIT"); do
  if grep -q '"event":"linked"' "$EVENTS" 2>/dev/null; then
    break
  fi
  kill -0 "$(cat "$RUN/seven.pid")" 2>/dev/null || break
  sleep 1
done
LAUNCHED="$(grep '"event":"launched"' "$EVENTS" 2>/dev/null | tail -n 1)"
CDP="$(field "$LAUNCHED" cdp)"
NEW_PDIR="$(field "$LAUNCHED" profileDir)"
case "$NEW_PDIR" in
  */seven-profiles/"$PROFILE") echo "$NEW_PDIR" >"$RUN/seven.profile" ;;
esac
if ! grep -q '"event":"linked"' "$EVENTS" 2>/dev/null || [ -z "$CDP" ]; then
  LAST="$(tail -n 1 "$EVENTS" 2>/dev/null)"
  WHY="$(field "$LAST" message)"
  fail "it did not link within $LINK_WAIT s (${WHY:-$(tail -n 1 "$JD/seven.err" 2>/dev/null)})"
fi
echo "$CDP" >"$RUN/seven.cdp"

SEED="$("$NODE_BIN" "$HERE/seven.mjs" seed --cdp "$CDP" --endpoint "$(python3 "$HERE/common.py" --seven-endpoint)" \
  --model local/claude-slicc --owner-tier "$OWNER_TIER" --timeout "$SEED_WAIT" 2>>"$JD/seven.err")"
echo "$SEED" >"$JD/seven.seed.json"
if [ "$(field "$SEED" ok)" != "true" ]; then
  fail "$(field "$SEED" error)"
fi

if [ "$OWNER_TIER" = "user" ]; then
  nohup python3 "$HERE/owner_watch.py" "$JOB" "$CDP" >>"$JD/owner_watch.log" 2>&1 </dev/null &
  echo $! >"$RUN/owner_watch.pid"
  python3 "$HERE/common.py" --owner-tier "$JOB" || fail "the job ended while SLICC seven started"
fi

nohup "$SEVEN_BIN" prompt --cdp "$CDP" --json "$PROMPT" >"$JD/slicc.log" 2>&1 </dev/null &
echo $! >"$RUN/seven-prompt.pid"
echo "SLICC seven started for $JOB (cdp $CDP, log $JD/slicc.log)"
