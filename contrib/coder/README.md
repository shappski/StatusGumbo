# Reverse tunnel to a remote machine (optional)

You only need this when a machine running Claude Code **cannot reach the
collector's URL**. The case it was built for is a Coder workspace with no
inbound route to the laptop that runs the collector. If your remote machine can
reach the collector, point its reporter at the collector's URL and skip this
directory.

The collector's machine opens an SSH reverse tunnel, so the remote's
`127.0.0.1:4747` lands on the collector. The status-line hook in the main
README already defaults `STATUSGUMBO_URL` to that address, so the remote needs
no URL of its own.

| File | Purpose |
|---|---|
| `tunnel.sh` | Supervises `ssh -N -R`, owns the retry backoff, publishes `up`/`down` |
| `statusgumbo-tunnel.service` | systemd user unit that runs it (`@REPO@` is filled in by `install.sh`) |

## Setup

On the machine that runs the collector:

    mkdir -p ~/.config/statusgumbo
    echo 'CODER_HOST=<ssh host that reaches the remote>' > ~/.config/statusgumbo/tunnel.env
    ./install.sh

`install.sh` installs and starts the unit only when `tunnel.env` exists. It
needs systemd; without it, run `tunnel.sh` under whatever supervisor you use,
with `CODER_HOST` in its environment.

The unit sets `CODER_SSH_DISABLE_AUTOSTART=true` so that the tunnel never
wakes a stopped Coder workspace. Drop it if your host isn't Coder.

## On a shared machine

By default the tunnel lands on the remote's `127.0.0.1:4747`, and every user
and process on that machine can reach it. Without a token, any of them can
read the page's data and post ticks. With one, while the tunnel is down, one
of them can listen on port 4747 and collect the token from the next tick. On
a machine only you use, that doesn't matter. On a shared one, forward to a
Unix socket instead, which only your user on the remote can open. Add the
path to `tunnel.env`:

    STATUSGUMBO_TUNNEL_REMOTE=/run/user/<remote uid>/statusgumbo.sock

and point the remote's reporter at it:

    sh install-reporter.sh --socket /run/user/<remote uid>/statusgumbo.sock

This needs the remote's SSH server to accept Unix-socket forwards (OpenSSH
does, with `AllowStreamLocalForwarding` on, and creates the socket mode 0600).
A socket left behind by a dropped connection blocks the next forward unless
the server's `StreamLocalBindUnlink` is `yes`. **Not yet tried against a
Coder workspace's own SSH server.** Check that a ticking session shows up
before relying on it.

## On the page

The collector asks systemd about `statusgumbo-tunnel.service` and reads
`tunnel.sh`'s state file. A **tunnel down** card appears only when the unit is
installed and the link is down. Without systemd, or without the unit, there is
no card.

## Checking it

`systemctl --user is-active statusgumbo-tunnel` says only that the
supervisor runs, because it stays up between failed attempts. Ask the link:

    cat "${XDG_RUNTIME_DIR:-$HOME/.cache}/statusgumbo/tunnel.state"   # up | down

A rapid `up`/`down` flap there, or short connections in `journalctl --user -u
statusgumbo-tunnel -n 30`, means something is overriding the pinned
`ControlMaster=no` / `ControlPath=none`. ssh would otherwise hand the forward to
an existing multiplexer master and exit at once.

To confirm the unit really won't wake a stopped workspace, read the environment
of the coder process **inside the unit's cgroup**, not one found by `pgrep`,
which may be an unrelated client:

    systemctl --user status statusgumbo-tunnel | grep coder      # its PID
    tr '\0' '\n' < /proc/<pid>/environ | grep CODER_SSH_DISABLE_AUTOSTART

While the workspace is stopped, every attempt fails and the page shows
**tunnel down**. That's the honest state, not a fault.

## End to end, without Claude Code

From the remote machine:

    curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:4747/ingest \
      -H 'Content-Type: application/json' \
      -d '{"host":"vm-1","branch":"probe","payload":{"session_id":"probe","workspace":{"current_dir":"/tmp/probe"}}}'

Add `-H "Authorization: Bearer <token>"` if the collector has a token. Expect
`204`, and a `probe` card under `vm-1` for about 45 seconds.

## Removing it

    systemctl --user disable --now statusgumbo-tunnel
    rm ~/.config/systemd/user/statusgumbo-tunnel.service ~/.config/statusgumbo/tunnel.env

`ARCHITECTURE.md` (section "Tunnel (optional)") explains why the retry policy
lives in the script rather than systemd, and why the ssh options are pinned.
Tests: `sh tests/test_tunnel.sh`.
