#!/bin/sh
# StatusGumbo reporter — publish one status-line tick to the collector.
#
# Reads a Claude Code status-line payload on stdin and POSTs
# {host, branch, payload} to $STATUSGUMBO_URL/ingest. Prints nothing, waits
# for nothing, and fails soft on every path.
#
# Call it from the end of whatever status line the machine already has:
#
#     printf '%s' "$input" | ~/.claude/statusgumbo-report.sh
#
# That is the whole integration. Keeping it a separate file is the point: the
# status line stays the machine's own, maintained wherever it already lives,
# instead of being forked into this repo and left to drift.
#
# Why each guard is load-bearing, given the caller's stdout is the status line
# the CLI is drawing:
#   * prints nothing  — a stray character corrupts the rendered line.
#   * >/dev/null 2>&1 — a background child that inherits stdout holds the pipe
#                       open, and the CLI reads the status line until EOF.
#   * setsid          — Claude Code cancels the in-flight status-line script
#                       when the next update fires; a plain child shares its
#                       process group and dies with it, mid-POST.
#   * -m 1            — bounds the worst case if a socket wedges.
#   * unset URL, or a missing tool — a complete no-op, so a machine without
#                       StatusGumbo behaves byte-for-byte as it did before.
#
# The waiting heartbeat — `report.sh --wait`, run from a hook:
#
# Claude Code does not run the status line while a dialog is open (a question,
# a plan approval, a permission prompt), so a session waiting on you sends no
# ticks and leaves the page after ACTIVE_SECS — the one session the page most
# needs to show. Observed 2026-09-16; see
# ARCHITECTURE.md, "The heartbeat and the waiting hooks".
#
# So every tick also caches its payload by session, and hooks that fire as a
# wait begins (PreToolUse on AskUserQuestion|ExitPlanMode, PermissionRequest)
# call --wait. That starts one detached loop per session which re-sends the
# cached tick while real ones are missing. The collector is unchanged: it
# still receives ticks and nothing else. The loop ends when the claude process
# it watches exits — so a killed terminal leaves no ghost card — or once real
# ticks have been back for a few cycles.
set -u

# The collector's URL: the environment, else the file install-reporter.sh
# writes, for the token's reason below -- a status line inherits no shell rc.
# Only http(s), and never with whitespace, since it goes to curl as-is.
if [ -z "${STATUSGUMBO_URL:-}" ]; then
    urlfile=${XDG_CONFIG_HOME:-$HOME/.config}/statusgumbo/url
    if [ -r "$urlfile" ]; then
        STATUSGUMBO_URL=$(tr -d ' \t\r\n' < "$urlfile" 2>/dev/null) || STATUSGUMBO_URL=
        STATUSGUMBO_URL=${STATUSGUMBO_URL%/}
        case $STATUSGUMBO_URL in http://?*|https://?*) ;; *) STATUSGUMBO_URL= ;; esac
    fi
fi

# No collector configured: the overwhelmingly common case elsewhere. Leave.
[ -n "${STATUSGUMBO_URL:-}" ] || exit 0

# jq builds the envelope and curl sends it; without either there is nothing to
# do. Exit 0 regardless — a slim container is not an error condition.
command -v curl >/dev/null 2>&1 || exit 0
command -v jq   >/dev/null 2>&1 || exit 0

# The collector's shared token, when it has one. Read here rather than
# expected in the environment: a status line inherits no shell rc, so an
# export in one reaches nothing (the fish trap in README). A file needs no
# export at all. The pattern matches the collector's, and anything else is
# dropped rather than sent: it would be refused anyway, and a quote in it
# would break curl's config syntax below.
token=${STATUSGUMBO_TOKEN:-}
if [ -z "$token" ]; then
    tokfile=${STATUSGUMBO_TOKEN_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/statusgumbo/token}
    if [ -r "$tokfile" ]; then
        token=$(tr -d ' \t\r\n' < "$tokfile" 2>/dev/null) || token=
    fi
fi
case $token in *[!A-Za-z0-9._~-]*) token= ;; esac
auth=
if [ -n "$token" ]; then
    auth="header = \"Authorization: Bearer $token\""
fi

detach=
command -v setsid >/dev/null 2>&1 && detach=setsid

# Beside the collector's own tunnel.state when there is a runtime dir; a
# per-user /tmp dir on a box without one.
if [ -n "${STATUSGUMBO_STATE_DIR:-}" ]; then
    sessions=$STATUSGUMBO_STATE_DIR/sessions
elif [ -n "${XDG_RUNTIME_DIR:-}" ]; then
    sessions=$XDG_RUNTIME_DIR/statusgumbo/sessions
else
    sessions=/tmp/statusgumbo-$(id -u)/sessions
fi

interval=${STATUSGUMBO_HEARTBEAT_SECS:-10}
case $interval in ''|*[!0-9]*|0) interval=10 ;; esac

# A session_id becomes a file name, so only a plain id is accepted.
plain_id() {
    case $1 in ''|*[!A-Za-z0-9-]*) return 1 ;; esac
}

# POST one payload ($1) in the envelope the collector parses.
post() {
    # Branch is derived the same way the status line derives it, from the
    # payload's own working directory rather than $PWD — the reporter may be
    # invoked from anywhere. Not a git checkout is normal, and yields "".
    dir=$(printf '%s' "$1" | jq -r '.workspace.current_dir // empty' 2>/dev/null) || dir=
    branch=$(git -C "${dir:-.}" rev-parse --abbrev-ref HEAD 2>/dev/null) || branch=

    # STATUSGUMBO_HOST names a machine whose real hostname is unreadable, such
    # as a generated Coder workspace. Falling back to "unknown" keeps the
    # envelope well-formed on a box with no hostname command at all.
    host=${STATUSGUMBO_HOST:-$(hostname -s 2>/dev/null || echo unknown)}

    # What kind of machine this is, said only when it is known. The Coder
    # agent puts CODER=true and CODER_WORKSPACE_NAME into every session it
    # starts; anywhere else the key is left out, and the page draws no label
    # rather than a guessed one.
    #
    # A stated place comes first: STATUSGUMBO_PLACE, else the place file (a
    # file, because a status line inherits no shell rc). Only one plain
    # [a-z0-9-] word, as the collector requires. Anything else is not sent.
    where=${STATUSGUMBO_PLACE:-}
    if [ -z "$where" ]; then
        placefile=${XDG_CONFIG_HOME:-$HOME/.config}/statusgumbo/place
        if [ -r "$placefile" ]; then
            where=$(tr -d ' \t\r\n' < "$placefile" 2>/dev/null) || where=
        fi
    fi
    case $where in *[!a-z0-9-]*) where= ;; esac
    [ "${#where}" -le 24 ] || where=
    if [ -z "$where" ] && { [ "${CODER:-}" = true ] || [ -n "${CODER_WORKSPACE_NAME:-}" ]; }; then
        where=coder
    fi

    # The claude.ai session a Remote Control session mirrors to, so its card
    # can open it there. The status-line payload does not carry it; Claude
    # Code writes a `bridge-session` record to the transcript instead
    # (observed 2.1.283, 2026-09-28). The last one wins: /rc can be turned
    # off and on again. The collector validates the id and builds the link.
    transcript=$(printf '%s' "$1" | jq -r '.transcript_path // empty' 2>/dev/null) || transcript=
    bridge=
    if [ -n "$transcript" ] && [ -r "$transcript" ]; then
        bridge=$(grep -F '"type":"bridge-session"' "$transcript" 2>/dev/null | tail -n 1 \
            | jq -r '.bridgeSessionId // empty' 2>/dev/null) || bridge=
    fi

    printf '%s' "$1" \
        | jq -c --arg host "$host" --arg branch "$branch" --arg where "$where" \
              --arg bridge "$bridge" \
              '{host: $host, branch: $branch, payload: .}
               + (if $where == "" then {} else {where: $where} end)
               + (if $bridge == "" then {} else {bridge: $bridge} end)' \
        | $detach curl -s -m 1 -X POST "$STATUSGUMBO_URL/ingest" \
              -H 'Content-Type: application/json' --data-binary @- \
              -K /dev/fd/3 3<<EOF
$auth
EOF
}

# The Claude Code process this hook runs under, found by name among the
# nearest ancestors: claude itself, or node for an npm install. Only a few
# levels up, because a hook is claude's child or grandchild (via sh -c) — and
# a deeper search would find the claude a *test* or a nested shell happens to
# run under, and watch the wrong process.
claude_ancestor() {
    p=$PPID
    for _ in 1 2 3; do
        case $p in ''|0|1) return 1 ;; esac
        case $(ps -o comm= -p "$p" 2>/dev/null) in
            claude|node) printf '%s' "$p"; return 0 ;;
        esac
        p=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
    done
    return 1
}

mtime() {
    stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo 0
}

case ${1:-} in
--wait)
    # Run by Claude Code as a dialog opens: return at once, print nothing.
    input=$(cat)
    sid=$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null) || exit 0
    plain_id "$sid" || exit 0
    # No tick cached means nothing true to repeat.
    [ -f "$sessions/$sid.json" ] || exit 0
    # Nothing to watch means nothing would ever end the loop. Fail safe.
    pid=$(claude_ancestor) || exit 0
    pidfile=$sessions/$sid.wait
    if [ -s "$pidfile" ] && kill -0 "$(cat "$pidfile" 2>/dev/null)" 2>/dev/null; then
        exit 0
    fi
    $detach sh "$0" --heartbeat "$sid" "$pid" </dev/null >/dev/null 2>&1 &
    exit 0
    ;;
--heartbeat)
    sid=${2:-}; pid=${3:-}
    plain_id "$sid" || exit 0
    case $pid in ''|*[!0-9]*) exit 0 ;; esac
    cache=$sessions/$sid.json
    pidfile=$sessions/$sid.wait

    # Claim the session. noclobber makes the create atomic, so two dialogs
    # opening together still yield one loop; a pidfile left by a dead loop is
    # taken over.
    if ! (set -C; printf '%s' "$$" > "$pidfile") 2>/dev/null; then
        old=$(cat "$pidfile" 2>/dev/null)
        if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then exit 0; fi
        rm -f "$pidfile"
        (set -C; printf '%s' "$$" > "$pidfile") 2>/dev/null || exit 0
    fi
    trap '[ "$(cat "$pidfile" 2>/dev/null)" = "$$" ] && rm -f "$pidfile"' EXIT
    trap 'exit 0' HUP INT TERM

    # A tick within two intervals is a real one: the status line is running,
    # so send nothing. Three such cycles in a row and the wait is over. The
    # day cap is a backstop only; the claude process is the real bound.
    fresh=0
    cycles=0
    max=$((86400 / interval))
    while kill -0 "$pid" 2>/dev/null && [ "$cycles" -lt "$max" ]; do
        [ -f "$cache" ] || break
        age=$(( $(date +%s) - $(mtime "$cache") ))
        if [ "$age" -le $((interval * 2)) ]; then
            fresh=$((fresh + 1))
            [ "$fresh" -ge 3 ] && break
        else
            fresh=0
            post "$(cat "$cache")" >/dev/null 2>&1
        fi
        cycles=$((cycles + 1))
        sleep "$interval"
    done
    exit 0
    ;;
esac

input=$(cat)
[ -n "$input" ] || exit 0

# Cache the tick for the heartbeat. Written aside and renamed, so a loop never
# reads half a payload.
sid=$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null) || sid=
if plain_id "$sid"; then
    (
        umask 077
        mkdir -p "$sessions" &&
            printf '%s' "$input" > "$sessions/.$sid.$$" &&
            mv -f "$sessions/.$sid.$$" "$sessions/$sid.json"
    ) >/dev/null 2>&1
fi

# The whole pipeline is backgrounded and fully redirected, so no descriptor
# belonging to the caller's status line is held open by anything here. The
# braces are load-bearing: dash forks a backgrounded *function call* before
# applying its redirections, so `post … >/dev/null &` holds stdout for the
# whole curl timeout. Measured: 1012ms bare, 8ms braced.
{ post "$input"; } >/dev/null 2>&1 &

exit 0
