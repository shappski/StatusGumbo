#!/bin/sh
# Install StatusGumbo on this machine.
#
# Deliberately does NOT edit settings.json — that file has other content and
# a botched merge would break Claude Code. The required snippet is printed
# instead.
set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
UNIT_DIR="$HOME/.config/systemd/user"

printf 'StatusGumbo — installing from %s\n\n' "$REPO"

# 1. The reporter. This is the supported integration: the machine keeps its own
#    status line and appends the hook, so nothing here has to be forked.
mkdir -p "$HOME/.claude"
REPORTER="$HOME/.claude/statusgumbo-report.sh"
if [ -e "$REPORTER" ] || [ -L "$REPORTER" ]; then
    printf '  left existing %s alone\n' "$REPORTER"
else
    ln -s "$REPO/report.sh" "$REPORTER"
    printf '  linked %s -> %s\n' "$REPORTER" "$REPO/report.sh"
fi

# 2. The status line — which this project no longer supplies at all.
#
#    This script used to replace it unconditionally, backing the old one up
#    first. That looked safe and was not: on the first real deployment the
#    file it displaced was NEWER than this repo's copy — a status line
#    maintained in a dotfiles repo, which this repo had forked a day earlier
#    and then fallen behind. The install silently downgraded the very thing it
#    exists to report on, and a backup nobody reads does not undo that.
#
#    That was fixed by never replacing an existing line, and printing the hook
#    instead. The fork it linked on a machine with none stayed, and drifted the
#    same way — three weeks behind, invisible to its own golden test, which
#    differed by one colour code. So the fork is gone too. This project is a
#    reporter; the status line belongs to whoever maintains it.
#
#    Adding two lines to a file you own is a decision; having it swapped out
#    from under you is not.
TARGET="$HOME/.claude/statusline.sh"
if [ -e "$TARGET" ] || [ -L "$TARGET" ]; then
    if grep -q 'statusgumbo-report' "$TARGET" 2>/dev/null; then
        printf '  %s already calls the reporter\n' "$TARGET"
    else
        printf '  kept your existing %s — append this to the end of it:\n\n' "$TARGET"
        printf '      if [ -x "$HOME/.claude/statusgumbo-report.sh" ]; then\n'
        printf '          printf %s "$input" |\n' "'%s'"
        printf '              STATUSGUMBO_URL="${STATUSGUMBO_URL:-http://127.0.0.1:4747}" \\\n'
        printf '              "$HOME/.claude/statusgumbo-report.sh"\n'
        printf '      fi\n\n'
    fi
else
    printf '  no status line found at %s.\n' "$TARGET"
    printf '  This project does not ship one — install any status line you like\n'
    printf '  (claude-statusline is the one these docs assume), then append the\n'
    printf '  hook from the README and re-run.\n\n'
fi

# 2. Install and start the collector unit.
mkdir -p "$UNIT_DIR"
sed "s|@REPO@|$REPO|g" "$REPO/systemd/statusgumbo.service" \
    > "$UNIT_DIR/statusgumbo.service"
systemctl --user daemon-reload
systemctl --user enable --now statusgumbo.service
printf '  started statusgumbo.service\n'

# 3. Tell the operator what is left, since it cannot be done safely here.
#
#    This block used to open by telling you to export STATUSGUMBO_URL from a
#    shell profile, and to set STATUSGUMBO_HOST=coder-vm on the VM. Both are
#    documented in README.md as mistakes that caused real
#    outages, and the installer went on printing them anyway — so an operator
#    following this script's own output reproduced both. Kept as a comment
#    because "why is the advice missing" is a question someone will ask.
cat <<'EOF'

One manual step remains — the heartbeat:

  Add refreshInterval to whichever settings file WINS for that directory.
  Precedence, highest first:

    .claude/settings.local.json > .claude/settings.json > ~/.claude/settings.json

  In a repo whose project settings define statusLine, the override must go in
  settings.local.json or the heartbeat never ticks. Find the winner with jq,
  not grep — "statusLine" also appears inside permission strings, and grepping
  for it sends you chasing a file that does not decide anything:

    jq -c '.statusLine' ~/.claude/settings.json \
       ./.claude/settings.json ./.claude/settings.local.json 2>/dev/null

  Only a non-null answer counts. The key:

    "statusLine": {
      "type": "command",
      "command": "~/.claude/statusline.sh",
      "refreshInterval": 10
    }

  Then CHECK IT AGAIN once Claude Code has started at least once. Observed
  2026-09-01 on a freshly provisioned Coder workspace: Claude Code rewrote the
  statusLine object itself — normalising the "~" to an absolute path and
  dropping refreshInterval in the same edit. It writes THROUGH a symlink, so a
  settings.json owned by a dotfiles repo is not immune: the committed copy
  still said 10 while the checked-out copy no longer did.

  Without the key an idle session publishes nothing, and a session past
  ACTIVE_SECS is not drawn at all — so it vanishes from the page rather than
  greying out. Do not compensate by widening ACTIVE_SECS: with no heartbeat
  the silence is unbounded and no finite cutoff covers it.

  In the same file, merge in the waiting hooks. Claude Code does not run the
  status line while a question, plan approval or permission prompt is open,
  so without them the session waiting on YOU leaves the page:

    "hooks": {
      "PreToolUse": [
        {
          "matcher": "AskUserQuestion|ExitPlanMode",
          "hooks": [{"type": "command", "command": "if [ -x \"$HOME/.claude/statusgumbo-report.sh\" ]; then STATUSGUMBO_URL=\"${STATUSGUMBO_URL:-http://127.0.0.1:4747}\" \"$HOME/.claude/statusgumbo-report.sh\" --wait; fi"}]
        }
      ],
      "PermissionRequest": [
        {
          "hooks": [{"type": "command", "command": "if [ -x \"$HOME/.claude/statusgumbo-report.sh\" ]; then STATUSGUMBO_URL=\"${STATUSGUMBO_URL:-http://127.0.0.1:4747}\" \"$HOME/.claude/statusgumbo-report.sh\" --wait; fi"}]
        }
      ]
    }

Nothing needs exporting, here or on the remote machine. The hook supplies
STATUSGUMBO_URL=http://127.0.0.1:4747 itself, and that same value is right on
the VM too, because the VM's loopback is carried home by the tunnel.

  Do NOT put it in a shell profile instead. That is where it was, and it
  reached only sessions launched from an interactive bash: the shell here was
  fish, so every session rendered a perfect status line and published nothing
  while the page read "no sessions reporting" with five sessions live. Claude
  Code inherits the environment of whatever launched it — a desktop launcher,
  zsh, tmux, a systemd unit and cron all read no .bashrc either.

STATUSGUMBO_HOST is optional and usually unnecessary. It was added assuming a
Coder workspace hostname is generated and unreadable; on a real workspace
`hostname -s` is the workspace's own name, which is exactly what you want.
Setting it somewhere only SOME sessions inherit splits one machine across two
host badges on the page — set it where every session sees it, or not at all.

EOF

# 4. Tunnel to the Coder VM — only if configured, since the workspace name
#    is machine-specific and deliberately not committed.
TUNNEL_ENV="$HOME/.config/statusgumbo/tunnel.env"
if [ -f "$TUNNEL_ENV" ]; then
    # Substituted, not copied: ExecStart is this repo's tunnel.sh now, so a
    # verbatim copy would install a literal @REPO@ and fail at start with a
    # bare 203/EXEC.
    sed "s|@REPO@|$REPO|g" "$REPO/contrib/coder/statusgumbo-tunnel.service" \
        > "$UNIT_DIR/statusgumbo-tunnel.service"
    systemctl --user daemon-reload
    # Non-fatal on purpose. Under `set -e` a failure here aborted the whole
    # script with the collector unit already enabled, and swallowed the
    # closing "open http://<tailnet-ip>:4747" line — which is the reason the
    # operator ran the installer. A tunnel that will not start is a real
    # problem, but it is the *remote* machine's sessions that are affected;
    # the local collector and the phone URL still work.
    if systemctl --user enable --now statusgumbo-tunnel.service; then
        printf '  started statusgumbo-tunnel.service\n'
    else
        printf '  WARNING: statusgumbo-tunnel.service failed to start.\n'
        printf '           Sessions on the remote machine will not report.\n'
        printf '           Diagnose with: journalctl --user -u statusgumbo-tunnel\n'
    fi
else
    printf '  skipped tunnel: create %s with CODER_HOST=<ssh host> to enable\n' \
        "$TUNNEL_ENV"
fi

if command -v tailscale >/dev/null 2>&1; then
    ip=$(tailscale ip -4 2>/dev/null | head -1 || true)
    # `if`, never `[ … ] && …`. As the last command of the script under set -e,
    # a false test makes install.sh exit 1 on a machine where the install
    # actually SUCCEEDED and only the tailnet address was missing — tailscaled
    # down, or logged out. Measured in sh, dash and bash on 2026-09-01: all
    # three exit 1. README.md documents this trap for the status line and
    # tunnel.sh documents it for the supervisor loop; it bit this file too, and
    # silently, because everything printed above is already correct.
    if [ -n "$ip" ]; then
        printf 'Then open http://%s:4747 on your phone.\n' "$ip"
    else
        printf 'No tailnet address yet — once tailscaled is up, the phone page\n'
        printf 'is http://<tailnet-ip>:4747 (tailscale ip -4).\n'
    fi
fi
