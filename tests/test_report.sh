#!/bin/sh
# Regression harness for report.sh, the standalone reporter.
#
# report.sh exists so the status line does not have to be forked to gain
# reporting. It is called from the *end* of whatever status line the machine
# already has:
#
#     printf '%s' "$input" | ~/.claude/statusgumbo-report.sh
#
# That calling convention creates an invariant the in-script reporter never
# had to satisfy: report.sh runs inside a process whose stdout IS the rendered
# status line. A single stray character on stdout corrupts the line the CLI
# draws, and a child that keeps the descriptor open hangs the draw until it
# exits. So "prints nothing" and "returns at once" are correctness here, not
# tidiness.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SCRIPT="$ROOT/report.sh"
PAYLOAD="$ROOT/tests/fixtures/payload.json"

fails=0

# report.sh caches each tick for the waiting heartbeat. Without this, every
# run of this harness writes the fixture session into the real runtime dir.
STATUSGUMBO_STATE_DIR=$(mktemp -d)
export STATUSGUMBO_STATE_DIR
# And an empty config dir, so a real ~/.config/statusgumbo/url or token on
# the machine running this cannot turn "nothing configured" into a POST.
XDG_CONFIG_HOME=$(mktemp -d)
export XDG_CONFIG_HOME
trap 'rm -rf "$STATUSGUMBO_STATE_DIR" "$XDG_CONFIG_HOME"' EXIT

pass() { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n' "$1"; fails=$((fails + 1)); }

printf 'report.sh\n'

# 0. Guard. Without this, a missing report.sh passes four of the tests below
#    by accident: `sh` on a nonexistent file writes to stderr, prints nothing
#    on stdout and returns instantly, which is exactly what "silent" and
#    "returns at once" look for. A harness that goes green against no
#    implementation is worse than no harness.
if [ ! -f "$SCRIPT" ]; then
    printf '  FAIL report.sh does not exist at %s\n' "$SCRIPT"
    printf '\n1 report test(s) failed\n'
    exit 1
fi
if [ ! -x "$SCRIPT" ]; then
    fail 'report.sh is executable (it is invoked directly, not via `sh`)'
fi

# 1. No collector configured is the common case on a machine that has never
#    heard of StatusGumbo. It must cost nothing and say nothing.
unset STATUSGUMBO_URL 2>/dev/null || true
out=$(sh "$SCRIPT" < "$PAYLOAD" 2>/dev/null) || true
if [ -z "$out" ]; then
    pass 'silent when STATUSGUMBO_URL is unset'
else
    fail "silent when STATUSGUMBO_URL is unset (printed: $out)"
fi
if sh "$SCRIPT" < "$PAYLOAD" >/dev/null 2>&1; then
    pass 'exits 0 when STATUSGUMBO_URL is unset'
else
    fail 'exits 0 when STATUSGUMBO_URL is unset'
fi

# 2. The invariant. 192.0.2.1 is RFC 5737 TEST-NET: packets are dropped, so
#    curl burns its full timeout rather than failing fast. Command
#    substitution waits for any process still holding stdout, so a reporter
#    that forgets to detach blocks here for the whole cap. The 500ms threshold
#    sits between the two outcomes.
STATUSGUMBO_URL='http://192.0.2.1:8080'
export STATUSGUMBO_URL
start_ns=$(date +%s%N)
out=$(sh "$SCRIPT" < "$PAYLOAD" 2>/dev/null) || true
end_ns=$(date +%s%N)
elapsed_ms=$(( (end_ns - start_ns) / 1000000 ))

if [ "$elapsed_ms" -lt 500 ]; then
    pass "returns without waiting for the collector (${elapsed_ms}ms)"
else
    fail "returns without waiting for the collector (${elapsed_ms}ms, expected <500)"
fi
if [ -z "$out" ]; then
    pass 'prints nothing on stdout with the collector unreachable'
else
    fail "prints nothing on stdout with the collector unreachable (printed: $out)"
fi
unset STATUSGUMBO_URL

# 3. Slim container: no curl on PATH must fail soft. The stub dir holds only
#    what report.sh legitimately needs, since curl shares /usr/bin with them.
STUB=$(mktemp -d)
for tool in jq git hostname cat sh; do
    src=$(command -v "$tool" 2>/dev/null) || continue
    ln -s "$src" "$STUB/$tool"
done
STATUSGUMBO_URL='http://192.0.2.1:8080'
export STATUSGUMBO_URL
if PATH="$STUB" sh "$SCRIPT" < "$PAYLOAD" >/dev/null 2>&1; then
    pass 'exits 0 when curl is unavailable'
else
    fail 'exits 0 when curl is unavailable'
fi
out=$(PATH="$STUB" sh "$SCRIPT" < "$PAYLOAD" 2>/dev/null) || true
if [ -z "$out" ]; then
    pass 'silent when curl is unavailable'
else
    fail "silent when curl is unavailable (printed: $out)"
fi
rm -rf "$STUB"
unset STATUSGUMBO_URL

# 4. The envelope is the same contract the collector already parses. This is
#    the test that pins report.sh to the forked reporter it replaces: same
#    keys, same payload, passed through untouched.
CAPTURE=$(mktemp)
python3 "$ROOT/tests/helpers/capture_post.py" 49997 "$CAPTURE" &
capture_pid=$!
sleep 1
STATUSGUMBO_URL='http://127.0.0.1:49997'
STATUSGUMBO_HOST='test-host'
export STATUSGUMBO_URL STATUSGUMBO_HOST
sh "$SCRIPT" < "$PAYLOAD" >/dev/null 2>&1
wait "$capture_pid" 2>/dev/null || true

if [ -s "$CAPTURE" ] \
   && [ "$(jq -r '.host' < "$CAPTURE")" = 'test-host' ] \
   && [ "$(jq -r '.payload.session_id' < "$CAPTURE")" = '6d876063-b801-4738-bc7e-9a425e773b97' ] \
   && [ "$(jq -r '.payload.context_window.used_percentage' < "$CAPTURE")" = '27' ] \
   && [ "$(jq -r 'has("branch")' < "$CAPTURE")" = 'true' ]; then
    pass 'posts {host, branch, payload} envelope with the payload intact'
else
    fail 'posts {host, branch, payload} envelope with the payload intact'
    printf '    captured: %s\n' "$(cat "$CAPTURE" 2>/dev/null || echo '<empty>')"
fi
rm -f "$CAPTURE"
unset STATUSGUMBO_URL STATUSGUMBO_HOST

# 4b. `where` says what kind of machine sent the tick, and only when known:
#     set inside a Coder workspace, absent everywhere else so the page draws
#     no label rather than a guessed one.
where_of() {
    CAPTURE=$(mktemp)
    python3 "$ROOT/tests/helpers/capture_post.py" 49997 "$CAPTURE" &
    capture_pid=$!
    sleep 1
    env -u CODER -u CODER_WORKSPACE_NAME -u STATUSGUMBO_PLACE XDG_CONFIG_HOME="${PLACECFG:-/nonexistent}" "$@" \
        STATUSGUMBO_URL='http://127.0.0.1:49997' sh "$SCRIPT" < "$PAYLOAD" >/dev/null 2>&1
    wait "$capture_pid" 2>/dev/null || true
    jq -r 'if has("where") then .where else "<absent>" end' < "$CAPTURE" 2>/dev/null || echo '<empty>'
    rm -f "$CAPTURE"
}
got=$(where_of CODER=true)
if [ "$got" = coder ]; then pass 'a Coder workspace says where: coder'
else fail "a Coder workspace says where: coder (got $got)"; fi
got=$(where_of CODER_WORKSPACE_NAME=devbox)
if [ "$got" = coder ]; then pass 'CODER_WORKSPACE_NAME alone is enough'
else fail "CODER_WORKSPACE_NAME alone is enough (got $got)"; fi
got=$(where_of)
if [ "$got" = '<absent>' ]; then pass 'anywhere else, where is left out'
else fail "anywhere else, where is left out (got $got)"; fi

# 4b'. A machine can state its own place: STATUSGUMBO_PLACE, else the file
#      ~/.config/statusgumbo/place (a status line inherits no shell rc).
#      A stated place wins over Coder detection; a value that is not one
#      plain word is not sent.
got=$(where_of STATUSGUMBO_PLACE=desktop)
if [ "$got" = desktop ]; then pass 'STATUSGUMBO_PLACE is sent as where'
else fail "STATUSGUMBO_PLACE is sent as where (got $got)"; fi
got=$(where_of STATUSGUMBO_PLACE=vm-1 CODER=true)
if [ "$got" = vm-1 ]; then pass 'a stated place wins over Coder detection'
else fail "a stated place wins over Coder detection (got $got)"; fi
PLACECFG=$(mktemp -d)
mkdir -p "$PLACECFG/statusgumbo"
printf 'laptop\n' > "$PLACECFG/statusgumbo/place"
got=$(where_of)
if [ "$got" = laptop ]; then pass 'the place file is read without any env'
else fail "the place file is read without any env (got $got)"; fi
rm -rf "$PLACECFG"; PLACECFG=
got=$(where_of 'STATUSGUMBO_PLACE=My Laptop')
if [ "$got" = '<absent>' ]; then pass 'a place that is not one plain word is not sent'
else fail "a place that is not one plain word is not sent (got $got)"; fi

# 4c. `bridge` is the claude.ai session a Remote Control session mirrors to.
#     Claude Code records it in the transcript, not the status-line payload,
#     so it is read from there: the last record wins, since /rc can be turned
#     off and on again. No record, or no readable transcript, and it is left
#     out.
bridge_of() {
    CAPTURE=$(mktemp)
    python3 "$ROOT/tests/helpers/capture_post.py" 49997 "$CAPTURE" &
    capture_pid=$!
    sleep 1
    jq -c --arg t "$1" '.transcript_path = $t' < "$PAYLOAD" \
        | STATUSGUMBO_URL='http://127.0.0.1:49997' sh "$SCRIPT" >/dev/null 2>&1
    wait "$capture_pid" 2>/dev/null || true
    jq -r 'if has("bridge") then .bridge else "<absent>" end' < "$CAPTURE" 2>/dev/null || echo '<empty>'
    rm -f "$CAPTURE"
}
TRANSCRIPT=$(mktemp)
{
    printf '%s\n' '{"type":"user","message":{"content":"hi"}}'
    printf '%s\n' '{"type":"bridge-session","sessionId":"6d876063-b801-4738-bc7e-9a425e773b97","bridgeSessionId":"cse_01old"}'
    printf '%s\n' '{"type":"assistant","message":{"content":"bridge-session"}}'
    printf '%s\n' '{"type":"bridge-session","sessionId":"6d876063-b801-4738-bc7e-9a425e773b97","bridgeSessionId":"cse_01new"}'
} > "$TRANSCRIPT"
got=$(bridge_of "$TRANSCRIPT")
if [ "$got" = cse_01new ]; then pass 'a Remote Control session sends its latest bridge id'
else fail "a Remote Control session sends its latest bridge id (got $got)"; fi
printf '%s\n' '{"type":"user","message":{"content":"hi"}}' > "$TRANSCRIPT"
got=$(bridge_of "$TRANSCRIPT")
if [ "$got" = '<absent>' ]; then pass 'without Remote Control, bridge is left out'
else fail "without Remote Control, bridge is left out (got $got)"; fi
got=$(bridge_of "$TRANSCRIPT.missing")
if [ "$got" = '<absent>' ]; then pass 'an unreadable transcript leaves bridge out'
else fail "an unreadable transcript leaves bridge out (got $got)"; fi
{
    printf '%s\n' '{"type":"bridge-session","sessionId":"6d876063-b801-4738-bc7e-9a425e773b97","bridgeSessionId":"cse_01real"}'
    printf '%s\n' '{"type":"user","toolUseResult":{"type":"bridge-session","note":"not a record"}}'
} > "$TRANSCRIPT"
got=$(bridge_of "$TRANSCRIPT")
if [ "$got" = cse_01real ]; then pass 'a later line that only contains the marker does not hide the record'
else fail "a later line that only contains the marker does not hide the record (got $got)"; fi
rm -f "$TRANSCRIPT"

# 4d. The shared token. A collector with a token refuses reports without it,
#     so the reporter sends it as a Bearer header -- from STATUSGUMBO_TOKEN,
#     or from a file, because a status line inherits no shell rc (the fish
#     trap). It goes to curl through a config on a file descriptor, never
#     argv, where any user's `ps` would show it.
auth_of() {
    CAPTURE=$(mktemp)
    AUTH=$(mktemp)
    python3 "$ROOT/tests/helpers/capture_post.py" 49997 "$CAPTURE" "$AUTH" &
    capture_pid=$!
    sleep 1
    env -u STATUSGUMBO_TOKEN -u STATUSGUMBO_TOKEN_FILE XDG_CONFIG_HOME="$CFG" "$@" \
        STATUSGUMBO_URL='http://127.0.0.1:49997' sh "$SCRIPT" < "$PAYLOAD" >/dev/null 2>&1
    wait "$capture_pid" 2>/dev/null || true
    if [ -s "$CAPTURE" ]; then cat "$AUTH"; else printf '<no post>'; fi
    rm -f "$CAPTURE" "$AUTH"
}
CFG=$(mktemp -d)
TOK=correct-horse-battery-staple
got=$(auth_of)
if [ -z "$got" ]; then pass 'no token configured, no Authorization header'
else fail "no token configured, no Authorization header (got $got)"; fi
got=$(auth_of STATUSGUMBO_TOKEN=$TOK)
if [ "$got" = "Bearer $TOK" ]; then pass 'STATUSGUMBO_TOKEN is sent as a Bearer header'
else fail "STATUSGUMBO_TOKEN is sent as a Bearer header (got $got)"; fi
mkdir -p "$CFG/statusgumbo"
printf '%s\n' "$TOK" > "$CFG/statusgumbo/token"
got=$(auth_of)
if [ "$got" = "Bearer $TOK" ]; then pass 'the token file is read without any env'
else fail "the token file is read without any env (got $got)"; fi
printf '%s\n' "other-token-other-token" > "$CFG/alt"
got=$(auth_of STATUSGUMBO_TOKEN_FILE="$CFG/alt")
if [ "$got" = "Bearer other-token-other-token" ]; then pass 'STATUSGUMBO_TOKEN_FILE names another file'
else fail "STATUSGUMBO_TOKEN_FILE names another file (got $got)"; fi
if grep -n 'Bearer' "$SCRIPT" | grep -v '^[0-9]*: *#' | grep -q -- '-H'; then
    fail 'the token is never put on the curl command line'
else
    pass 'the token is never put on the curl command line'
fi
rm -rf "$CFG"

# 4e. The collector's URL from a file. install-reporter.sh writes it there for
#     the same reason as the token: a status line inherits no shell rc, and a
#     collector that isn't on this machine's loopback needs its URL to reach
#     every session. The environment still wins, so the existing hooks, which
#     pass the loopback default inline, behave exactly as before.
url_post() {
    CAPTURE=$(mktemp)
    python3 "$ROOT/tests/helpers/capture_post.py" 49996 "$CAPTURE" /dev/null &
    capture_pid=$!
    sleep 1
    env -u STATUSGUMBO_URL XDG_CONFIG_HOME="$CFG" "$@" sh "$SCRIPT" < "$PAYLOAD" >/dev/null 2>&1
    sleep 2; kill "$capture_pid" 2>/dev/null || true; wait "$capture_pid" 2>/dev/null || true
    if [ -s "$CAPTURE" ]; then printf 'posted'; else printf 'nothing'; fi
    rm -f "$CAPTURE"
}
CFG=$(mktemp -d)
mkdir -p "$CFG/statusgumbo"
printf 'http://127.0.0.1:49996/\n' > "$CFG/statusgumbo/url"
got=$(url_post)
if [ "$got" = posted ]; then pass 'the url file is read when STATUSGUMBO_URL is unset'
else fail "the url file is read when STATUSGUMBO_URL is unset (got $got)"; fi
got=$(url_post STATUSGUMBO_URL=http://127.0.0.1:1)
if [ "$got" = nothing ]; then pass 'STATUSGUMBO_URL wins over the url file'
else fail "STATUSGUMBO_URL wins over the url file (got $got)"; fi
printf 'file:///etc/passwd\n' > "$CFG/statusgumbo/url"
out=$(env -u STATUSGUMBO_URL XDG_CONFIG_HOME="$CFG" sh "$SCRIPT" < "$PAYLOAD" 2>&1); rc=$?
if [ -z "$out" ] && [ "$rc" -eq 0 ]; then pass 'a url file that is not http(s) is ignored, silently'
else fail "a url file that is not http(s) is ignored, silently (rc=$rc out=$out)"; fi
for bad in 'ftp://127.0.0.1:49996' '-K/etc/passwd' 'http://127.0.0.1:49996 -v'; do
    got=$(url_post STATUSGUMBO_URL="$bad")
    if [ "$got" = nothing ]; then pass "a STATUSGUMBO_URL of '$bad' is not used"
    else fail "a STATUSGUMBO_URL of '$bad' is not used (got $got)"; fi
done
rm -rf "$CFG"

# 4f. A Unix socket instead of TCP, for a tunnel that forwards to one. Set,
#     the post goes through it and no URL is needed.
CFG=$(mktemp -d)
SOCK="$CFG/collector.sock"
CAPTURE=$(mktemp)
python3 "$ROOT/tests/helpers/capture_post.py" "$SOCK" "$CAPTURE" &
capture_pid=$!
sleep 1
env -u STATUSGUMBO_URL XDG_CONFIG_HOME="$CFG" STATUSGUMBO_SOCKET="$SOCK" sh "$SCRIPT" < "$PAYLOAD" >/dev/null 2>&1
wait "$capture_pid" 2>/dev/null || true
if [ "$(jq -r '.payload.session_id' < "$CAPTURE" 2>/dev/null)" = "$(jq -r .session_id < "$PAYLOAD")" ]; then
    pass 'STATUSGUMBO_SOCKET posts through a Unix socket, with no URL needed'
else
    fail 'STATUSGUMBO_SOCKET posts through a Unix socket, with no URL needed'
fi
: > "$CAPTURE"
mkdir -p "$CFG/statusgumbo"
printf '%s\n' "$SOCK" > "$CFG/statusgumbo/socket"
rm -f "$SOCK"
python3 "$ROOT/tests/helpers/capture_post.py" "$SOCK" "$CAPTURE" &
capture_pid=$!
sleep 1
env -u STATUSGUMBO_URL -u STATUSGUMBO_SOCKET XDG_CONFIG_HOME="$CFG" sh "$SCRIPT" < "$PAYLOAD" >/dev/null 2>&1
wait "$capture_pid" 2>/dev/null || true
if [ -s "$CAPTURE" ]; then pass 'the socket file is read without any env'
else fail 'the socket file is read without any env'; fi
rm -rf "$CFG" "$CAPTURE"

# 5. The documented integration snippet itself, in both states that matter.
#    Caught for real on deployment: the first version of this hook was
#    `[ -x ... ] && printf ... | reporter`, which is the last command in the
#    host status line. When the reporter is absent the guard is false, the &&
#    short-circuits, and the compound command returns 1 — so the whole status
#    line exited non-zero on precisely the machines the guard exists to
#    protect. The render was perfect; only the exit code was wrong, which is
#    why eyeballing it missed it.
HOOKDIR=$(mktemp -d)
REPORTER="$HOOKDIR/statusgumbo-report.sh"
cp "$SCRIPT" "$REPORTER"
chmod +x "$REPORTER"

# A minimal host status line carrying the snippet exactly as README gives it.
cat > "$HOOKDIR/statusline.sh" <<EOF
input=\$(cat)
printf 'RENDERED'
if [ -x "$REPORTER" ]; then
    printf '%s' "\$input" | "$REPORTER"
fi
EOF

unset STATUSGUMBO_URL 2>/dev/null || true
hook_out=$(sh "$HOOKDIR/statusline.sh" < "$PAYLOAD" 2>/dev/null); hook_rc=$?
if [ "$hook_out" = 'RENDERED' ] && [ "$hook_rc" -eq 0 ]; then
    pass 'host status line renders and exits 0 with the reporter present'
else
    fail "host status line with reporter present (out='$hook_out' rc=$hook_rc)"
fi

rm -f "$REPORTER"
hook_out=$(sh "$HOOKDIR/statusline.sh" < "$PAYLOAD" 2>/dev/null); hook_rc=$?
if [ "$hook_out" = 'RENDERED' ] && [ "$hook_rc" -eq 0 ]; then
    pass 'host status line renders and exits 0 with the reporter absent'
else
    fail "host status line with reporter absent (out='$hook_out' rc=$hook_rc)"
fi
rm -rf "$HOOKDIR"

printf '\n'
if [ "$fails" -eq 0 ]; then
    printf 'all report tests passed\n'
else
    printf '%s report test(s) failed\n' "$fails"
fi
exit "$fails"
