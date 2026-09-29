#!/bin/sh
# The reverse tunnel's supervisor: keep an ssh forward to the remote machine
# alive, and publish whether it currently is.
#
# WHY THIS IS NOT JUST `Restart=always`
#
# It was, and the retry policy was expressed with `RestartSec` / `RestartSteps`
# / `RestartMaxDelaySec`. Those derive the delay from `NRestarts`, and systemd
# 255 never decays that counter — a `systemctl restart` leaves it untouched,
# only an explicit stop *then* start clears it, and a long healthy run does not
# clear it at all. Measured on 2026-07-31: the unit ran eleven hours straight
# (00:28→11:26) and still carried NRestarts=16 from the previous night's
# outage, so it sat permanently at its 10-minute ceiling.
#
# That made an ordinary event expensive. A reverse tunnel cannot survive a
# laptop suspend, and there are several suspends a day; each resume then cost
# up to ten minutes of a blind phone page, for a link that would have come
# back on the first try. Backoff has to decay on success, and the state that
# would let it cannot live in systemd. It lives here.
#
# The unit keeps `Restart=always` as a backstop for this script dying, not as
# the retry policy.
set -eu

: "${CODER_HOST:?CODER_HOST must be set (see ~/.config/statusgumbo/tunnel.env)}"

# Tunables, all seconds. Overridable mainly so the harness can run in seconds
# rather than in the quarter-hours the real thresholds imply.
MIN_DELAY=${STATUSGUMBO_TUNNEL_MIN_DELAY:-10}
MAX_DELAY=${STATUSGUMBO_TUNNEL_MAX_DELAY:-600}
# How long ssh must survive before its connection is believed and published as
# up. Not zero: with ExitOnForwardFailure a doomed attempt still exists for a
# moment, and publishing up for that moment would put a false all-clear on the
# page during exactly the outage it is meant to report.
SETTLE_SECS=${STATUSGUMBO_TUNNEL_SETTLE_SECS:-5}
# How long a connection must last to count as healthy and earn a reset delay.
# Far above SETTLE, because a *failing* attempt can be long-lived: the
# overnight failures on 2026-07-31 were `dial tcp … i/o timeout` after nearly
# six minutes. A threshold near those would reset the backoff on the failures
# it exists to slow down.
HEALTHY_SECS=${STATUSGUMBO_TUNNEL_HEALTHY_SECS:-900}

# The runtime dir, not the home dir: this answer describes a live process, and
# a stale "up" surviving a reboot would silence the alarm exactly when it is
# most needed. tmpfs forgets for us. collector/server.py reads this path.
STATE_DIR="${XDG_RUNTIME_DIR:-$HOME/.cache}/statusgumbo"
STATE_FILE="$STATE_DIR/tunnel.state"
mkdir -p "$STATE_DIR"

# Write-then-rename, so a reader never sees a half-written file and has to
# guess what a truncated word means.
publish() {
    printf '%s\n' "$1" > "$STATE_FILE.tmp"
    mv "$STATE_FILE.tmp" "$STATE_FILE"
}

trap 'publish down; exit 0' INT TERM

delay=$MIN_DELAY
while :; do
    publish down
    started=$(date +%s)

    # ControlMaster/ControlPath: this process must own its connection. Under a
    # user `Host *` block with `ControlMaster auto` — a common setup — ssh
    # would instead hand the forward to an existing mux master and exit 0 at
    # once, which reads here as a connection that closed immediately. The loop
    # would spin while the forward quietly lived and died with someone else's
    # session. Both flags are needed: `none` declines to join a master, `no`
    # declines to become one.
    ssh -N \
        -o ExitOnForwardFailure=yes \
        -o ServerAliveInterval=30 \
        -o ServerAliveCountMax=3 \
        -o BatchMode=yes \
        -o ControlMaster=no \
        -o ControlPath=none \
        -R 4747:127.0.0.1:4747 "$CODER_HOST" &
    ssh_pid=$!

    # Publish up only once the connection has stood up for SETTLE_SECS, in a
    # child so the wait below is still what notices ssh exiting.
    (
        sleep "$SETTLE_SECS"
        if kill -0 "$ssh_pid" 2>/dev/null; then
            publish up
        fi
    ) &
    settle_pid=$!

    wait "$ssh_pid" 2>/dev/null || true
    kill "$settle_pid" 2>/dev/null || true
    publish down

    lasted=$(( $(date +%s) - started ))

    # A connection that lasted is evidence the path works, so the next attempt
    # is prompt. Anything shorter is a failure and the wait doubles toward the
    # ceiling: against a workspace deliberately left stopped overnight, a flat
    # retry would be a Coder API call every few seconds until morning.
    if [ "$lasted" -ge "$HEALTHY_SECS" ]; then
        delay=$MIN_DELAY
    fi

    sleep "$delay"

    # Grown after the wait, not before, so the first retry following a healthy
    # run is the full-speed one.
    #
    # `if`, never `[ … ] && …`: as the last command of a loop body under
    # `set -e`, a false test would exit the whole supervisor. The same trap is
    # documented for the status line in README.md; it bites identically here,
    # and silently, because the render is unaffected.
    if [ "$lasted" -lt "$HEALTHY_SECS" ]; then
        delay=$(( delay * 2 ))
        if [ "$delay" -gt "$MAX_DELAY" ]; then
            delay=$MAX_DELAY
        fi
    fi
done
