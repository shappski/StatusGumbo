#!/bin/sh
# Regression harness for the waiting heartbeat in report.sh.
#
# Claude Code does not run the status line while a dialog is open: a question
# (AskUserQuestion), a plan approval or a permission prompt. So a live session
# waiting on you sends no ticks and falls off the page after ACTIVE_SECS,
# which is the one session the page most needs to show. Observed 2026-09-16:
# a session sat on a question for minutes, vanished, and came back within
# seconds of the answer.
#
# The fix keeps the collector's model intact — it still receives ticks and
# nothing else. report.sh caches each real tick's payload, and hooks that fire
# when a wait begins call `report.sh --wait`, which starts a detached loop
# that re-sends the cached tick while the real ones are missing. The loop
# ends when the claude process exits, or once real ticks are back.
#
# A fake `claude` ancestor is a renamed copy of sh: report.sh finds the
# process to watch by name, and a test-only override would be code the real
# hook never runs.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SCRIPT="$ROOT/report.sh"
PAYLOAD="$ROOT/tests/fixtures/payload.json"
SID='6d876063-b801-4738-bc7e-9a425e773b97'

fails=0
pass() { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n' "$1"; fails=$((fails + 1)); }

WORK=$(mktemp -d)
cp "$(command -v sh)" "$WORK/claude"
HOOK="$WORK/hook.json"
printf '{"session_id":"%s","hook_event_name":"PreToolUse","tool_name":"AskUserQuestion"}' "$SID" > "$HOOK"

STATUSGUMBO_STATE_DIR="$WORK/state"
STATUSGUMBO_HEARTBEAT_SECS=1
STATUSGUMBO_HOST='test-host'
export STATUSGUMBO_STATE_DIR STATUSGUMBO_HEARTBEAT_SECS STATUSGUMBO_HOST
CACHE="$STATUSGUMBO_STATE_DIR/sessions/$SID.json"
PIDFILE="$STATUSGUMBO_STATE_DIR/sessions/$SID.wait"

# Poll for up to $1 tenths of a second until the command in $2 succeeds.
within() {
    n=$1; shift
    while [ "$n" -gt 0 ]; do
        if eval "$1"; then return 0; fi
        sleep 0.1; n=$((n - 1))
    done
    return 1
}
heartbeat_alive() {
    [ -s "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}
reset_state() {
    if heartbeat_alive; then kill "$(cat "$PIDFILE")" 2>/dev/null || true; fi
    rm -rf "$STATUSGUMBO_STATE_DIR"
}
# Seed the cache as a real tick would, then age it past the fresh window.
seed_stale_cache() {
    mkdir -p "$STATUSGUMBO_STATE_DIR/sessions"
    cp "$PAYLOAD" "$CACHE"
    touch -d '-1 minute' "$CACHE"
}
# Start a fake claude that runs the wait hook and then stays alive for $1 s.
start_claude() {
    STATUSGUMBO_URL=$URL "$WORK/claude" -c \
        "'$SCRIPT' --wait < '$HOOK' >/dev/null 2>&1; sleep $1" &
    claude_pid=$!
}

printf 'report.sh heartbeat\n'

# 1. A real tick caches its payload, keyed by session, so the heartbeat has
#    something true to repeat.
reset_state
STATUSGUMBO_URL='http://192.0.2.1:8080' sh "$SCRIPT" < "$PAYLOAD" >/dev/null 2>&1
# Compared as JSON: $(cat) drops the fixture's trailing newline, which is not
# part of the payload.
if [ -f "$CACHE" ] && [ "$(jq -cS . < "$PAYLOAD")" = "$(jq -cS . < "$CACHE" 2>/dev/null)" ]; then
    pass 'a tick caches its payload by session_id'
else
    fail 'a tick caches its payload by session_id'
fi

# 2. No collector configured stays a complete no-op: nothing written either.
reset_state
(unset STATUSGUMBO_URL; sh "$SCRIPT" < "$PAYLOAD" >/dev/null 2>&1) || true
if [ ! -e "$STATUSGUMBO_STATE_DIR" ]; then
    pass 'no cache is written when STATUSGUMBO_URL is unset'
else
    fail 'no cache is written when STATUSGUMBO_URL is unset'
fi

# 3. session_id becomes a file name, so anything but a plain id is refused.
reset_state
jq -c '.session_id = "../escaped"' < "$PAYLOAD" \
    | STATUSGUMBO_URL='http://192.0.2.1:8080' sh "$SCRIPT" >/dev/null 2>&1
if [ ! -e "$STATUSGUMBO_STATE_DIR/escaped.json" ] \
   && [ -z "$(ls -A "$STATUSGUMBO_STATE_DIR/sessions" 2>/dev/null)" ]; then
    pass 'a session_id that is not a plain id is not cached'
else
    fail 'a session_id that is not a plain id is not cached'
fi

# 4. The hook is run by Claude Code while it opens a dialog: it must return at
#    once and print nothing, whatever the loop it starts goes on to do.
reset_state
seed_stale_cache
URL='http://192.0.2.1:8080'
start_ns=$(date +%s%N)
out=$(STATUSGUMBO_URL=$URL "$WORK/claude" -c "'$SCRIPT' --wait < '$HOOK'" 2>/dev/null) || true
elapsed_ms=$(( ($(date +%s%N) - start_ns) / 1000000 ))
if [ "$elapsed_ms" -lt 500 ] && [ -z "$out" ]; then
    pass "--wait returns at once and prints nothing (${elapsed_ms}ms)"
else
    fail "--wait returns at once and prints nothing (${elapsed_ms}ms, out='$out')"
fi
reset_state

# 5. While the real ticks are missing, the heartbeat re-sends the cached tick
#    in the same envelope a real one would use.
seed_stale_cache
CAPTURE="$WORK/capture.json"
: > "$CAPTURE"
python3 "$ROOT/tests/helpers/capture_post.py" 49996 "$CAPTURE" &
capture_pid=$!
sleep 1
URL='http://127.0.0.1:49996'
start_claude 10
# used_percentage is in the cached tick and not in the hook's own input, so a
# reporter that mistakes the hook JSON for a tick and posts it cannot pass.
if within 50 '[ -s "$CAPTURE" ]' \
   && [ "$(jq -r '.host' < "$CAPTURE")" = 'test-host' ] \
   && [ "$(jq -r '.payload.session_id' < "$CAPTURE")" = "$SID" ] \
   && [ "$(jq -r '.payload.context_window.used_percentage' < "$CAPTURE")" = '27' ] \
   && [ "$(jq -r 'has("branch")' < "$CAPTURE")" = 'true' ]; then
    pass 'the heartbeat re-sends the cached tick while ticks are missing'
else
    fail 'the heartbeat re-sends the cached tick while ticks are missing'
    printf '    captured: %s\n' "$(cat "$CAPTURE" 2>/dev/null || echo '<empty>')"
fi
kill "$capture_pid" 2>/dev/null || true

# 6. One heartbeat per session. A second dialog in the same wait must not
#    start a second loop.
first=$(cat "$PIDFILE" 2>/dev/null || echo none)
STATUSGUMBO_URL=$URL "$WORK/claude" -c "'$SCRIPT' --wait < '$HOOK'" >/dev/null 2>&1
sleep 0.5
if heartbeat_alive && [ "$(cat "$PIDFILE")" = "$first" ] \
   && [ "$(pgrep -fc -- "report.sh --heartbeat $SID" || true)" -eq 1 ]; then
    pass 'a second --wait does not start a second heartbeat'
else
    fail "a second --wait does not start a second heartbeat (pids: $(pgrep -f -- "--heartbeat $SID" | tr '\n' ' '))"
fi

# 7. The ghost-card guard: when the claude process is gone, so is the loop.
kill "$claude_pid" 2>/dev/null || true
wait "$claude_pid" 2>/dev/null || true
if within 30 '! heartbeat_alive'; then
    pass 'the heartbeat stops when the claude process exits'
else
    fail 'the heartbeat stops when the claude process exits'
fi
reset_state

# 8. Real ticks are back: the heartbeat sends nothing, then ends by itself.
mkdir -p "$STATUSGUMBO_STATE_DIR/sessions"
cp "$PAYLOAD" "$CACHE"
: > "$CAPTURE"
python3 "$ROOT/tests/helpers/capture_post.py" 49995 "$CAPTURE" &
capture_pid=$!
sleep 1
URL='http://127.0.0.1:49995'
start_claude 15
# Precondition, or both checks below pass against a loop that never started.
if within 20 'heartbeat_alive'; then
    pass 'a heartbeat starts even when the cached tick is fresh'
else
    fail 'a heartbeat starts even when the cached tick is fresh'
fi
fresh_ok=true
i=0
while [ "$i" -lt 30 ]; do
    touch "$CACHE"
    heartbeat_alive || break
    sleep 0.2; i=$((i + 1))
done
if [ -s "$CAPTURE" ]; then fresh_ok=false; fi
if $fresh_ok; then
    pass 'the heartbeat sends nothing while real ticks arrive'
else
    fail 'the heartbeat sends nothing while real ticks arrive'
fi
if within 60 '{ touch "$CACHE"; ! heartbeat_alive; }'; then
    pass 'the heartbeat ends by itself once real ticks are back'
else
    fail 'the heartbeat ends by itself once real ticks are back'
fi
kill "$capture_pid" "$claude_pid" 2>/dev/null || true
wait "$claude_pid" 2>/dev/null || true
reset_state

# 9. Nothing to repeat: no tick was ever cached, so no loop starts.
URL='http://192.0.2.1:8080'
start_claude 3
sleep 0.5
if ! heartbeat_alive; then
    pass 'no heartbeat starts without a cached tick'
else
    fail 'no heartbeat starts without a cached tick'
fi
kill "$claude_pid" 2>/dev/null || true
wait "$claude_pid" 2>/dev/null || true
reset_state

# 10. No claude ancestor to watch: starting a loop would have nothing to end
#     it, so it does not start. Failing safe means today's behaviour.
#     Run orphaned: the harness itself may be running under a real claude a
#     few levels up (a Claude Code shell), which the search would rightly find.
#     The outer subshell exits at once, so the hook is reparented before the
#     sleep ends and has no claude anywhere above it.
seed_stale_cache
( (sleep 0.3; STATUSGUMBO_URL=$URL sh "$SCRIPT" --wait < "$HOOK" >/dev/null 2>&1) & )
sleep 1
if ! heartbeat_alive; then
    pass 'no heartbeat starts without a claude process to watch'
else
    fail 'no heartbeat starts without a claude process to watch'
fi
reset_state

# 11. --wait with no collector configured is a no-op like everything else.
seed_stale_cache
(unset STATUSGUMBO_URL; "$WORK/claude" -c "'$SCRIPT' --wait < '$HOOK'" >/dev/null 2>&1) || true
sleep 0.5
if ! heartbeat_alive; then
    pass 'no heartbeat starts when STATUSGUMBO_URL is unset'
else
    fail 'no heartbeat starts when STATUSGUMBO_URL is unset'
fi
reset_state

rm -rf "$WORK"
printf '\n'
if [ "$fails" -eq 0 ]; then
    printf 'all heartbeat tests passed\n'
else
    printf '%s heartbeat test(s) failed\n' "$fails"
fi
exit "$fails"
