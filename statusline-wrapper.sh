#!/bin/sh
# StatusGumbo's status-line wrapper, installed as ~/.claude/statusgumbo-statusline.sh.
#
# install-reporter.sh points statusLine at this and saves the machine's own
# command in ~/.config/statusgumbo/statusline-command. Each tick goes to the
# reporter, then to that command with the same input, so the line draws
# exactly what it drew before, exit code included. With no saved command
# there was no status line, and this draws nothing.
#
# The user's script is never edited: that is what keeps it theirs. The
# reporter prints nothing and detaches its POST, so it costs the line nothing.

input=$(cat)

reporter="$HOME/.claude/statusgumbo-report.sh"
if [ -x "$reporter" ]; then
    printf '%s' "$input" | "$reporter"
fi

saved=${XDG_CONFIG_HOME:-$HOME/.config}/statusgumbo/statusline-command
if [ -s "$saved" ]; then
    original=$(cat "$saved")
    # bash when there is one, for a command written with bash in mind (a
    # `~` path is the common case, and both expand that). The saved command
    # never names this wrapper: the installer refuses to save it.
    if command -v bash >/dev/null 2>&1; then shell='bash'; else shell='sh'; fi
    printf '%s' "$input" | "$shell" -c "$original"
fi
