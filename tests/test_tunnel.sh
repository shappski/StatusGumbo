#!/bin/sh
# Regression harness for tunnel.sh, the reverse tunnel's retry wrapper.
#
# The wrapper exists because systemd's restart backoff is a one-way ratchet.
# `RestartSteps` / `RestartMaxDelaySec` derive the delay from `NRestarts`, and
# systemd 255 never decays that counter: a `systemctl restart` leaves it
# untouched, only an explicit stop *then* start clears it, and a long healthy
# run does not clear it at all. Measured on 2026-07-31, when the tunnel ran
# eleven hours (00:28→11:26) and still reported NRestarts=16 from the previous
# night's outage. So an ordinary laptop resume — the connection cannot survive
# a suspend, and there are several a day — inherited the 10-minute ceiling and
# left the phone blind for the whole gap.
#
# Retry state that has to reset on success cannot live in systemd. It lives
# here, and these tests pin the two properties that motivated the move: a
# healthy run earns a short delay, a run of failures does not.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SCRIPT="$ROOT/contrib/coder/tunnel.sh"

fails=0
# Delays under test are a second apart; process startup and shell overhead are
# tens of milliseconds. This separates a real difference from that noise
# without being loose enough to accept an unchanged delay.
MARGIN_MS=300
pass() { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n' "$1"; fails=$((fails + 1)); }

printf 'tunnel.sh\n'

if [ ! -f "$SCRIPT" ]; then
    printf '  FAIL tunnel.sh does not exist at %s\n' "$SCRIPT"
    printf '\n1 tunnel test(s) failed\n'
    exit 1
fi
if [ ! -x "$SCRIPT" ]; then
    fail 'tunnel.sh is executable (systemd runs it directly)'
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# A stub ssh, shadowing the real one on PATH. Each invocation reads its own
# duration from a behaviour file — one line per attempt, seconds to stay
# "connected" — and records when it started and stopped, which is how the
# tests below see the delays the wrapper chose.
mkdir -p "$WORK/bin"
cat > "$WORK/bin/ssh" <<'STUB'
#!/bin/sh
n=$(cat "$STUB_COUNT" 2>/dev/null || echo 0)
n=$((n + 1))
printf '%s' "$n" > "$STUB_COUNT"
[ -z "${STUB_ARGS:-}" ] || printf '%s\n' "$@" > "$STUB_ARGS"
dur=$(sed -n "${n}p" "$STUB_PLAN" 2>/dev/null || true)
[ -n "$dur" ] || dur=0
# Milliseconds. Whole seconds put the measurement inside its own rounding
# error: the delays under test are 1s and 2s apart, and second-boundary
# rounding made a flat-retry mutation read as 1s-then-2s — a clean pass for
# an implementation with the backoff removed.
now() { printf '%s' "$(( $(date +%s%N) / 1000000 ))"; }
printf 'start %s %s\n' "$n" "$(now)" >> "$STUB_LOG"
[ "$dur" -gt 0 ] && sleep "$dur"
printf 'end %s %s\n' "$n" "$(now)" >> "$STUB_LOG"
exit 255
STUB
chmod +x "$WORK/bin/ssh"

# Short, integer knobs so the suite runs in seconds rather than minutes. The
# defaults they replace are in tunnel.sh; this harness deliberately does not
# assert on those, only on the behaviour they parameterise.
run_wrapper() {  # run_wrapper <plan-file> <seconds-to-observe>
    export STUB_PLAN="$1"
    export STUB_LOG="$WORK/log"
    export STUB_COUNT="$WORK/count"
    : > "$STUB_LOG"
    : > "$STUB_COUNT"
    # Each run starts with no published state. Without this the link-state
    # tests read the "down" left behind by the previous run's shutdown trap,
    # and pass against a wrapper that published nothing at all.
    rm -rf "$WORK/run"
    XDG_RUNTIME_DIR="$WORK/run" \
    PATH="$WORK/bin:$PATH" \
    CODER_HOST=stub-host \
    STATUSGUMBO_TUNNEL_MIN_DELAY=1 \
    STATUSGUMBO_TUNNEL_MAX_DELAY="${WRAP_MAX:-8}" \
    STATUSGUMBO_TUNNEL_SETTLE_SECS=1 \
    STATUSGUMBO_TUNNEL_HEALTHY_SECS=3 \
    "$SCRIPT" >"$WORK/out" 2>"$WORK/err" &
    wrapper_pid=$!
    sleep "$2"
    # Sample the link state while the wrapper is still up. Reading it after
    # the kill would only ever see the "down" its shutdown trap writes, and
    # both state tests would pass without the wrapper having published
    # anything at all during the run.
    cat "$WORK/run/statusgumbo/tunnel.state" 2>/dev/null > "$WORK/observed" \
        || : > "$WORK/observed"
    kill "$wrapper_pid" 2>/dev/null || true
    wait "$wrapper_pid" 2>/dev/null || true
}

observed() { tr -d '\n' < "$WORK/observed"; }

stamp() {  # stamp <start|end> <attempt-number>
    awk -v k="$1" -v n="$2" '$1 == k && $2 == n { print $3; exit }' "$WORK/log"
}

# An attempt that never happened yields an empty stamp, and `$(( - ))` is 0 in
# POSIX arithmetic — so a delay test comparing missing stamps silently reads as
# an enormous negative gap and PASSES. That is not hypothetical: it is how the
# first version of test 5 went green against an implementation with the
# healthy-run reset deleted, which is the one behaviour this file exists for.
# Every gap is now guarded on both of its endpoints.
gap() {  # gap <from-attempt> <to-attempt>; empty output if either is missing
    _from=$(stamp end "$1")
    _to=$(stamp start "$2")
    if [ -z "$_from" ] || [ -z "$_to" ]; then
        return 0
    fi
    printf '%s' "$(( _to - _from ))"
}

# 1. Without a target there is nothing to do, and failing loudly beats looping
#    forever against an empty host name.
if XDG_RUNTIME_DIR="$WORK/run" PATH="$WORK/bin:$PATH" \
        sh "$SCRIPT" >/dev/null 2>&1; then
    fail 'exits non-zero when CODER_HOST is unset'
else
    pass 'exits non-zero when CODER_HOST is unset'
fi

# 2. The link state the page reads. `ActiveState=active` cannot answer this
#    any more — the wrapper is active while it sleeps between failures too —
#    so it publishes the answer separately. Getting this wrong silences the
#    one alarm the project exists to raise, which is why it is pinned.
printf '9\n' > "$WORK/plan-up"
run_wrapper "$WORK/plan-up" 3
if [ "$(observed)" = "up" ]; then
    pass 'publishes up once the connection has settled'
else
    fail "publishes up once the connection has settled (got: $(observed))"
fi

# 3. The same signal in the failing direction: while the wrapper is waiting
#    to retry it is running, but the tunnel is not up.
printf '0\n0\n0\n0\n0\n' > "$WORK/plan-down"
run_wrapper "$WORK/plan-down" 2
if [ "$(observed)" = "down" ]; then
    pass 'publishes down while waiting to retry'
else
    fail "publishes down while waiting to retry (got: $(observed))"
fi

# 3b. Startup, before the first connection has settled. Publishing nothing
#     here is not neutral: an absent file means "no wrapper", which
#     _classify_tunnel deliberately reads as up on the strength of the unit
#     being active. So staying silent during the connect would put a false
#     all-clear on the page for the first few seconds of every retry.
printf '9\n' > "$WORK/plan-startup"
run_wrapper "$WORK/plan-startup" 0.5
if [ "$(observed)" = "down" ]; then
    pass 'publishes down before the first connection settles'
else
    fail "publishes down before the first connection settles (got: $(observed))"
fi

# 4. Repeated fast failures must not become a request loop. This is what the
#    systemd backoff was for and the wrapper still has to provide it: against
#    a workspace that is stopped all night, a flat retry is a Coder API call
#    every few seconds until morning.
printf '0\n0\n0\n0\n' > "$WORK/plan-grow"
run_wrapper "$WORK/plan-grow" 6
gap1=$(gap 1 2)
gap2=$(gap 2 3)
if [ -z "$gap1" ] || [ -z "$gap2" ]; then
    fail 'backs off after repeated failures (an expected attempt never ran)'
elif [ "$gap2" -gt "$(( gap1 + MARGIN_MS ))" ]; then
    pass "backs off after repeated failures (${gap1}ms then ${gap2}ms)"
else
    fail "backs off after repeated failures (${gap1}ms then ${gap2}ms)"
fi

# 4b. The growth has a ceiling. Unbounded doubling would keep working all the
#     way through the night and then leave the tunnel asleep for hours after
#     the workspace came back — trading the fault this change fixes for a
#     slower version of the same one. With the ceiling at 2s the delays run
#     1, 2, 2, 2; without it, 1, 2, 4, 8.
printf '0\n0\n0\n0\n0\n' > "$WORK/plan-cap"
WRAP_MAX=2 run_wrapper "$WORK/plan-cap" 10
capped1=$(gap 3 4)
capped2=$(gap 4 5)
if [ -z "$capped1" ] || [ -z "$capped2" ]; then
    fail 'the delay stops growing at the ceiling (an expected attempt never ran)'
elif [ "$capped2" -lt "$(( capped1 + MARGIN_MS ))" ]; then
    pass "the delay stops growing at the ceiling (${capped1}ms then ${capped2}ms)"
else
    fail "the delay stops growing at the ceiling (${capped1}ms then ${capped2}ms)"
fi

# 5. THE REGRESSION. A connection that lasted is evidence the path works, so
#    the next attempt must be prompt again — this is precisely what systemd
#    would not do, and the reason the wrapper exists. Attempts 1 and 2 fail
#    and climb the delay; attempt 3 lasts longer than the healthy threshold;
#    the wait after it must be shorter than the wait before it.
printf '0\n0\n4\n0\n' > "$WORK/plan-reset"
# A correct wrapper reaches attempt 4 at about 8s (1 + 2 + 4 healthy + 1).
# The window is not tight around that: under load a marginal one turned this
# into an intermittent failure, and a delay test that flaps gets ignored. It
# is still far short of the ~11s a wrapper without the reset would need, which
# is what the assertion below actually distinguishes.
run_wrapper "$WORK/plan-reset" 14
before=$(gap 2 3)
after=$(gap 3 4)
if [ -z "$before" ] || [ -z "$after" ]; then
    # Attempt 4 not having run inside the window IS the failure: it means the
    # wrapper was still serving out a grown delay that the healthy attempt 3
    # should have cleared.
    fail 'a healthy run resets the delay (the retry after it never came)'
elif [ "$after" -lt "$(( before - MARGIN_MS ))" ]; then
    pass "a healthy run resets the delay (${before}ms before, ${after}ms after)"
else
    fail "a healthy run resets the delay (${before}ms before, ${after}ms after)"
fi

# The forward's remote end: loopback port 4747 by default, or a Unix socket
# when STATUSGUMBO_TUNNEL_REMOTE is a path. The host always follows --, so a
# CODER_HOST beginning with - can't become an ssh option.
printf '5\n' > "$WORK/plan-args"
export STUB_ARGS="$WORK/args"
: > "$STUB_ARGS"
run_wrapper "$WORK/plan-args" 1
if grep -qx -- '4747:127.0.0.1:4747' "$STUB_ARGS" \
   && [ "$(tail -n 2 "$STUB_ARGS" | tr '\n' ' ')" = '-- stub-host ' ] \
   && ! grep -q StreamLocalBindUnlink "$STUB_ARGS"; then
    pass 'the default forward is loopback port 4747, host after --'
else
    fail "the default forward is loopback port 4747, host after -- (args: $(tr '\n' ' ' < "$STUB_ARGS"))"
fi
: > "$STUB_ARGS"
STATUSGUMBO_TUNNEL_REMOTE=/run/user/1000/statusgumbo.sock run_wrapper "$WORK/plan-args" 1
if grep -qx -- '/run/user/1000/statusgumbo.sock:127.0.0.1:4747' "$STUB_ARGS" \
   && grep -qx -- 'StreamLocalBindUnlink=yes' "$STUB_ARGS"; then
    pass 'an absolute STATUSGUMBO_TUNNEL_REMOTE forwards to a remote Unix socket'
else
    fail "an absolute STATUSGUMBO_TUNNEL_REMOTE forwards to a remote Unix socket (args: $(tr '\n' ' ' < "$STUB_ARGS"))"
fi
unset STUB_ARGS
if STATUSGUMBO_TUNNEL_REMOTE='-oProxyCommand=x' CODER_HOST=h sh "$SCRIPT" >/dev/null 2>&1; then
    fail 'a STATUSGUMBO_TUNNEL_REMOTE that is neither port nor path is refused'
else
    pass 'a STATUSGUMBO_TUNNEL_REMOTE that is neither port nor path is refused'
fi

if [ "$fails" -eq 0 ]; then
    printf '\nall tunnel tests passed\n'
    exit 0
fi
printf '\n%s tunnel test(s) failed\n' "$fails"
exit 1
