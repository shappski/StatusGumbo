#!/bin/sh
# Install the StatusGumbo reporter for Claude Code on this machine.
#
#   sh install-reporter.sh --url http://collector:4747 [--token-file F | --token T]
#                          [--place WORD] [--socket /path/on/this/machine.sock]
#   sh install-reporter.sh --check        # is this machine still reporting?
#   sh install-reporter.sh --uninstall    # put settings.json back
#
# or, without a checkout:
#
#   curl -fsSL <repo raw URL>/install-reporter.sh | sh -s -- --url ... --token ...
#
# What it changes, and nothing else:
#   ~/.claude/statusgumbo-report.sh       the reporter
#   ~/.claude/statusgumbo-statusline.sh   a wrapper that reports each tick, then
#                                         runs your own status line unchanged
#   ~/.config/statusgumbo/{url,token,place,socket,statusline-command}
#   ~/.claude/settings.json               statusLine -> the wrapper (your old
#                                         command is saved, never edited),
#                                         refreshInterval 10 if unset, and the
#                                         two waiting hooks. A dated backup of
#                                         the file is left beside it.
#
# settings.json is merged with jq, validated before it replaces anything, and
# written through a symlink rather than over it, since dotfiles repos link it.
set -eu

SOURCE=${STATUSGUMBO_SOURCE:-https://raw.githubusercontent.com/shappski/StatusGumbo/master}
CLAUDE="$HOME/.claude"
SETTINGS="$CLAUDE/settings.json"
REPORTER="$CLAUDE/statusgumbo-report.sh"
WRAPPER="$CLAUDE/statusgumbo-statusline.sh"
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/statusgumbo"
SAVED="$CFG/statusline-command"

# The hook command reads the URL and token from the files above, so it needs
# no environment: a hook, like a status line, inherits no shell rc.
HOOK='if [ -x "$HOME/.claude/statusgumbo-report.sh" ]; then "$HOME/.claude/statusgumbo-report.sh" --wait; fi'

die() { printf 'error: %s\n' "$1" >&2; exit 1; }
say() { printf '  %s\n' "$1"; }

mode=install url='' token=${STATUSGUMBO_TOKEN:-} place='' socket=''
while [ $# -gt 0 ]; do
    case $1 in
        --url) [ $# -ge 2 ] || die "--url needs a value"; url=$2; shift 2 ;;
        --token) [ $# -ge 2 ] || die "--token needs a value"; token=$2; shift 2 ;;
        # A file keeps the token out of argv, where any user's ps can see it.
        --token-file) [ $# -ge 2 ] || die "--token-file needs a value"
            token=$(tr -d ' \t\r\n' < "$2") || die "cannot read $2"; shift 2 ;;
        --socket) [ $# -ge 2 ] || die "--socket needs a value"; socket=$2; shift 2 ;;
        --place) [ $# -ge 2 ] || die "--place needs a value"; place=$2; shift 2 ;;
        --check) mode=check; shift ;;
        --uninstall) mode=uninstall; shift ;;
        -h|--help) sed -n '2,13p' "$0" 2>/dev/null || true; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

command -v jq >/dev/null 2>&1 || die "jq is required (the reporter needs it too)"
command -v curl >/dev/null 2>&1 || die "curl is required (the reporter needs it too)"

# Rewrite settings.json with a jq program, or change nothing at all.
edit_settings() {
    target=$SETTINGS
    if [ -L "$SETTINGS" ]; then
        target=$(readlink -f "$SETTINGS") || die "cannot resolve the $SETTINGS symlink"
    fi
    if [ -e "$target" ]; then
        jq -e 'type == "object"' "$target" >/dev/null 2>&1 \
            || die "$SETTINGS is not a JSON object; fix it by hand, nothing was changed"
        current=$(cat "$target")
    else
        current='{}'
    fi
    tmp=$(mktemp "$(dirname "$target")/.settings.statusgumbo.XXXXXX")
    if ! printf '%s' "$current" | jq "$@" > "$tmp" \
        || ! jq -e 'type == "object"' "$tmp" >/dev/null 2>&1; then
        rm -f "$tmp"
        die "could not merge $SETTINGS; nothing was changed"
    fi
    if [ -e "$target" ]; then
        if cmp -s "$target" "$tmp" || [ "$(jq -S . "$target")" = "$(jq -S . "$tmp")" ]; then
            rm -f "$tmp"
            return 0
        fi
        cp -p "$target" "$SETTINGS.statusgumbo-$(date +%Y%m%d-%H%M%S)"
        chmod --reference="$target" "$tmp" 2>/dev/null || chmod 644 "$tmp"
    else
        chmod 644 "$tmp"
    fi
    mv "$tmp" "$target"
}

# Put a file from this checkout, or from $SOURCE when piped, at $2.
fetch() {
    here=$(dirname -- "$0")
    if [ -f "$here/$1" ] && [ -f "$here/install-reporter.sh" ]; then
        cp "$here/$1" "$2.tmp"
    else
        # https only, redirects included; file: is for testing a checkout.
        curl -fsSL --proto '=https,file' --proto-redir '=https' "$SOURCE/$1" -o "$2.tmp" \
            || { rm -f "$2.tmp"; die "could not download $SOURCE/$1"; }
    fi
    chmod 755 "$2.tmp"
    mv "$2.tmp" "$2"
}

# Whether a status-line command already runs the reporter itself (the README's
# append-a-snippet setup). Only a plain script path can be looked into.
already_reports() {
    first=${1%% *}
    case $first in "~/"*) first="$HOME/${first#\~/}" ;; esac
    [ -f "$first" ] && grep -q 'statusgumbo-report' "$first" 2>/dev/null
}

check() {
    problems=0
    ok() { printf '  ok    %s\n' "$1"; }
    bad() { printf '  FAIL  %s\n' "$1"; problems=$((problems + 1)); }

    if [ -x "$REPORTER" ]; then ok "reporter installed"; else bad "no reporter at $REPORTER"; fi

    line=$(jq -r '.statusLine.command // empty' "$SETTINGS" 2>/dev/null) || line=
    if [ -z "$line" ]; then
        bad "no statusLine in $SETTINGS"
    elif printf '%s' "$line" | grep -q 'statusgumbo-statusline.sh'; then
        if [ -x "$WRAPPER" ]; then ok "statusLine runs the wrapper"; else bad "statusLine names $WRAPPER, which is missing"; fi
    elif already_reports "$line"; then
        ok "statusLine ($line) calls the reporter itself"
    else
        bad "statusLine is '$line', which does not report (something rewrote it; re-run the install)"
    fi

    interval=$(jq -r '.statusLine.refreshInterval // empty' "$SETTINGS" 2>/dev/null) || interval=
    if [ -z "$interval" ]; then
        bad "no statusLine.refreshInterval: an idle session stops reporting and leaves the page"
    elif [ "$interval" -gt 10 ] 2>/dev/null; then
        bad "statusLine.refreshInterval is $interval; the page expects a tick every 10 s"
    else
        ok "refreshInterval $interval"
    fi

    for event in PreToolUse PermissionRequest; do
        if jq -e --arg e "$event" \
            'any(.hooks[$e][]?.hooks[]?; (.command // "") | test("statusgumbo-report"))' \
            "$SETTINGS" >/dev/null 2>&1; then
            ok "$event hook"
        else
            bad "no $event hook: a session waiting on you drops off the page"
        fi
    done

    # Project settings beat the user's. A project that sets its own statusLine
    # replaces ours in that directory, and nothing there reports.
    for project in .claude/settings.json .claude/settings.local.json; do
        pline=$(jq -r '.statusLine.command // empty' "$project" 2>/dev/null) || pline=
        [ -n "$pline" ] || continue
        if printf '%s' "$pline" | grep -q 'statusgumbo' || already_reports "$pline"; then
            ok "$PWD/$project statusLine reports"
        else
            bad "$PWD/$project sets its own statusLine ('$pline'); sessions in this directory don't report"
        fi
    done

    # Read the way report.sh reads them, so a pass here means the reporter
    # will get through too.
    sock=${STATUSGUMBO_SOCKET:-$(tr -d ' \t\r\n' < "$CFG/socket" 2>/dev/null || true)}
    case $sock in /*) ;; *) sock= ;; esac
    target=${STATUSGUMBO_URL:-$(tr -d ' \t\r\n' < "$CFG/url" 2>/dev/null || true)}
    target=${target%/}
    if [ -z "$target" ] && [ -n "$sock" ]; then target=http://localhost; fi
    if [ -z "$target" ]; then
        bad "no collector URL (STATUSGUMBO_URL or $CFG/url)"
    elif ! curl -fsS -m 5 ${sock:+--unix-socket} ${sock:+"$sock"} -o /dev/null "$target/healthz" 2>/dev/null; then
        bad "collector not reachable at $target${sock:+ through $sock}"
    else
        tok=${STATUSGUMBO_TOKEN:-$(tr -d ' \t\r\n' < "${STATUSGUMBO_TOKEN_FILE:-$CFG/token}" 2>/dev/null || true)}
        # It goes into a curl config below, where a quote would break out.
        case $tok in *[!A-Za-z0-9._~-]*) tok= ;; esac
        code=$( { [ -z "$tok" ] || printf 'header = "Authorization: Bearer %s"\n' "$tok"; } \
            | curl -s -m 5 ${sock:+--unix-socket} ${sock:+"$sock"} -o /dev/null -w '%{http_code}' -K - "$target/api/sessions") || code=000
        case $code in
            200) ok "collector at $target accepts this machine" ;;
            401) bad "collector at $target refuses this machine's token" ;;
            *) bad "collector at $target answered $code" ;;
        esac
    fi

    [ "$problems" -eq 0 ]
}

case $mode in
check)
    printf 'StatusGumbo reporter check\n'
    if check; then printf '\nall good\n'; else printf '\nnot reporting correctly; see FAIL above\n'; exit 1; fi
    exit 0
    ;;
uninstall)
    printf 'StatusGumbo reporter uninstall\n'
    if [ -e "$SETTINGS" ]; then
        original=$(cat "$SAVED" 2>/dev/null || true)
        added=no
        [ -e "$CFG/added-refresh-interval" ] && added=yes
        edit_settings --arg original "$original" --arg added "$added" '
            def ours: (.command // "") | test("statusgumbo-report");
            (if ((.statusLine.command // "") | test("statusgumbo-statusline.sh")) then
                (if $original == "" then del(.statusLine)
                 else .statusLine.command = $original
                      | if $added == "yes" then del(.statusLine.refreshInterval) else . end
                 end)
             else . end)
            | if .hooks then
                .hooks |= (with_entries(.value |= map(select(any(.hooks[]?; ours) | not)))
                           | with_entries(select(.value != [])))
                | if .hooks == {} then del(.hooks) else . end
              else . end'
        say "restored $SETTINGS"
    fi
    rm -f "$WRAPPER" "$SAVED" "$CFG/added-refresh-interval"
    [ -L "$REPORTER" ] || rm -f "$REPORTER"
    say "removed the wrapper and the reporter; left $CFG/url, token and place"
    exit 0
    ;;
esac

# Install. Validate everything before touching anything.
url=${url:-http://127.0.0.1:4747}
url=${url%/}
case $url in
    http://?*|https://?*) ;;
    *) die "--url must be an http:// or https:// URL" ;;
esac
case $url in *[[:space:]]*) die "--url must not contain spaces" ;; esac
if [ -n "$token" ]; then
    case $token in *[!A-Za-z0-9._~-]*) die "the token must be A-Z a-z 0-9 . _ ~ - only" ;; esac
    [ ${#token} -ge 16 ] || die "the token must be at least 16 characters"
fi
if [ -n "$place" ]; then
    case $place in *[!a-z0-9-]*) die "--place must be one word of a-z 0-9 -" ;; esac
    [ ${#place} -le 24 ] || die "--place must be at most 24 characters"
fi
if [ -n "$socket" ]; then
    case $socket in /*) ;; *) die "--socket must be an absolute path" ;; esac
    case $socket in *[[:space:]]*) die "--socket must not contain spaces" ;; esac
fi

# Plain http carries the token in the clear on every tick. That's fine on
# loopback and inside a tailnet (WireGuard encrypts it), and nowhere else.
if [ -n "$token" ] && [ -z "$socket" ]; then
    case $url in http://*)
        hostpart=${url#http://}; hostpart=${hostpart%%/*}
        case $hostpart in \[*) hostpart=${hostpart%%]*}]; ;; *) hostpart=${hostpart%%:*} ;; esac
        case $hostpart in
            localhost|127.*|'[::1]'|*.ts.net) ;;
            100.*)
                second=${hostpart#100.}; second=${second%%.*}
                if [ "$second" -lt 64 ] 2>/dev/null || [ "$second" -gt 127 ] 2>/dev/null; then
                    printf 'warning: %s is plain http outside loopback and Tailscale; the token will cross the network unencrypted. Use https.\n' "$url" >&2
                fi ;;
            *) printf 'warning: %s is plain http outside loopback and Tailscale; the token will cross the network unencrypted. Use https.\n' "$url" >&2 ;;
        esac ;;
    esac
fi

printf 'StatusGumbo reporter, installing for %s\n' "$url"
mkdir -p "$CLAUDE" "$CFG"

if [ -L "$REPORTER" ]; then
    say "left the symlinked $REPORTER alone"
else
    fetch report.sh "$REPORTER"
    say "installed $REPORTER"
fi
fetch statusline-wrapper.sh "$WRAPPER"
say "installed $WRAPPER"

printf '%s\n' "$url" > "$CFG/url"
if [ -n "$token" ]; then
    (umask 077; printf '%s\n' "$token" > "$CFG/token")
    chmod 600 "$CFG/token"
    say "saved the token in $CFG/token"
fi
if [ -n "$place" ]; then
    printf '%s\n' "$place" > "$CFG/place"
fi
if [ -n "$socket" ]; then
    printf '%s\n' "$socket" > "$CFG/socket"
    say "posting through the Unix socket $socket"
fi

current=$(jq -r '.statusLine.command // empty' "$SETTINGS" 2>/dev/null) || current=
# Remembered so that --uninstall takes back only a refreshInterval it added.
if [ -z "$(jq -r '.statusLine.refreshInterval // empty' "$SETTINGS" 2>/dev/null || true)" ]; then
    : > "$CFG/added-refresh-interval"
fi
wrap=yes
if printf '%s' "$current" | grep -q 'statusgumbo-statusline.sh'; then
    say "statusLine already runs the wrapper"
elif [ -n "$current" ] && already_reports "$current"; then
    wrap=no
    say "your status line ($current) already calls the reporter; left it as it is"
elif [ -n "$current" ]; then
    printf '%s\n' "$current" > "$SAVED"
    say "wrapping your status line ($current); it draws exactly as before"
else
    rm -f "$SAVED"
fi

edit_settings --arg wrapper "$WRAPPER" --arg hook "$HOOK" --arg wrap "$wrap" '
    def ours: any(.hooks[]?; (.command // "") | test("statusgumbo-report"));
    def add($event; $entry):
        .hooks[$event] = ((.hooks[$event] // [])
                          | if any(.[]; ours) then . else . + [$entry] end);
    (if $wrap == "yes" then
        .statusLine = ((.statusLine // {}) + {type: "command", command: $wrapper})
     else . end)
    | if .statusLine.refreshInterval == null then .statusLine.refreshInterval = 10 else . end
    | add("PreToolUse"; {matcher: "AskUserQuestion|ExitPlanMode",
                         hooks: [{type: "command", command: $hook}]})
    | add("PermissionRequest"; {hooks: [{type: "command", command: $hook}]})'
say "updated $SETTINGS"

printf '\nChecking:\n'
if check; then
    printf '\nDone. New Claude Code sessions report; running ones pick it up on restart.\n'
else
    printf '\nInstalled, but see FAIL above. Re-check any time with: sh install-reporter.sh --check\n'
fi
