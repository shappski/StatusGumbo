<img src="assets/statusgumbo.svg" alt="" width="64" align="right">

# StatusGumbo

Every Claude Code session you have running, on one page on your phone.

Claude Code's status line only exists in the terminal that draws it. StatusGumbo
sends each status-line tick to a small collector you run yourself and serves it
as a page: for every session, on every machine, its project, branch, context use
and whether it's still alive; plus the account's 5-hour and 7-day usage with
pace. It costs no tokens, because everything it shows is data Claude Code already
hands to the status line.

- **Self-hosted.** The collector is one Python process (standard library only),
  or one Docker container. Nothing leaves your machines unless you point it
  somewhere.
- **Your status line stays yours.** The reporter is wrapped around it or
  appended to it, never a replacement.
- **Honest.** A machine that goes quiet reads `not reporting · last tick 14:02`,
  never "no sessions". Stale numbers are withheld, not shown.

How it works, and why: [ARCHITECTURE.md](ARCHITECTURE.md).

## Quick start

You need a machine to run the collector, and `jq` and `curl` on each machine
that runs Claude Code.

**1. Run the collector.** Make a token first; anything reachable beyond
loopback and a Tailscale tailnet requires one.

    python3 -c 'import secrets; print(secrets.token_urlsafe(24))'

With Docker:

    docker build -t statusgumbo .
    docker run -d --name statusgumbo --restart unless-stopped \
      -p 4747:4747 -e STATUSGUMBO_TOKEN=<token> statusgumbo

Or directly, from a checkout (Python 3.8+):

    STATUSGUMBO_TOKEN=<token> python3 -m collector.server --bind 0.0.0.0

**2. Install the reporter on each machine:**

    sh install-reporter.sh --url http://<collector>:4747 --token <token>

**3. On your phone,** open `http://<collector>:4747/?t=<token>` once. The
collector swaps the token for a cookie, so later visits need only
`http://<collector>:4747`. The browser's "Add to home screen" then gives it
the StatusGumbo icon, and it opens without the address bar.

New Claude Code sessions appear within ten seconds. A running session picks
the reporter up when it restarts.

## The collector

    python3 -m collector.server [--port 4747] [--bind ADDR ...] [--token-file PATH]
                                [--allow-host NAME ...] [--tls-cert PEM --tls-key PEM]
                                [--behind-tls-proxy] [--cloud] [--no-local-host]

| Option | Environment | Meaning |
|---|---|---|
| `--port` | | Port to listen on (default 4747). |
| `--bind ADDR` | `STATUSGUMBO_BIND` (comma-separated) | Addresses to listen on. Default: `127.0.0.1` plus this machine's Tailscale address, if it has one. |
| `--token-file PATH` | `STATUSGUMBO_TOKEN_FILE`, `STATUSGUMBO_TOKEN` | The shared token. Without one, nothing is required. |
| `--allow-host NAME` | `STATUSGUMBO_ALLOW_HOSTS` (comma-separated) | Another name you reach the collector by, when it has no token ([below](#token-and-where-it-listens)). |
| `--tls-cert PEM`, `--tls-key PEM` | `STATUSGUMBO_TLS_CERT`, `STATUSGUMBO_TLS_KEY` | Serve HTTPS. The reporters' machines must trust the certificate (a public CA, or `tailscale cert`). |
| `--behind-tls-proxy` | `STATUSGUMBO_BEHIND_TLS_PROXY=1` | HTTPS is handled in front of the collector; silences the plain-HTTP warning. |
| `--cloud` | | Also show this account's cloud sessions ([below](#cloud-sessions)). Off by default. |
| `--no-local-host` | | Don't list the collector's own machine. The Docker image sets this. |
| | `STATUSGUMBO_HOST`, `STATUSGUMBO_PLACE` | The collector's own machine's name and place, when it also runs Claude Code. |

### Token and where it listens

Loopback (`127.0.0.1`, `::1`) and Tailscale addresses (`100.64.0.0/10`,
`fd7a:115c:a1e0::/48`) need no token. Any other address, `0.0.0.0` and `::`
included, makes the collector **refuse to start** until a token is set. Only IP
addresses are accepted, so DNS never decides what gets exposed.

Without a token, the collector answers only to the names it knows itself by:
`localhost`, this machine's hostname, its Tailscale names, and any IP address.
A request naming another host gets `421`, which stops a web page from using
DNS rebinding to read your sessions. If you reach it by some other name, add
it with `--allow-host`.

In every mode, `/ingest` refuses a request a browser sent (`403` when it
carries `Origin` or `Sec-Fetch-Site`) and a body not sent as
`Content-Type: application/json` (`415`). The reporter is curl and does
neither, so this only stops a web page open on the collector's machine from
posting ticks to it.

The token is at least 16 characters of `A-Z a-z 0-9 . _ ~ -`. A malformed token,
or a token file that can't be read, stops the collector rather than leaving it
open.

- Reporters send it as `Authorization: Bearer`. `/ingest` accepts nothing else,
  so a web page the phone visits can't post sessions.
- The phone opens `/?t=<token>` once and gets a year-long `HttpOnly`,
  `SameSite=Strict` cookie, derived from the token rather than holding it.
  Rotating the token withdraws access. The link stays in the browser's
  history, so treat it like the token itself.
- On any address beyond loopback and Tailscale, the token crosses the network
  on every tick and every page poll, so use HTTPS there: `--tls-cert` and
  `--tls-key`, or a proxy in front with `--behind-tls-proxy`. Without either,
  the collector warns at startup. Under `--tls-cert` the cookie is `Secure`;
  on plain HTTP it can't be, or the browser would drop it.
- `GET /healthz` answers `ok` without the token, for health checks.
- So do the home-screen manifest and its icons (`/manifest.webmanifest`,
  `/icons/…`), since Chrome fetches them without the cookie. They say nothing
  about any session.

### In Docker

The image holds the collector only. It binds `0.0.0.0`, so it won't start
without `STATUSGUMBO_TOKEN`, or a mounted file named by
`STATUSGUMBO_TOKEN_FILE`. Publish another host port with `-p 8080:4747` rather
than `--port`, because the health check probes 4747. The image runs with
`--no-local-host`, since its hostname is a container id and nothing inside
reports. Cloud sessions stay off, because the container has no claude.ai login.

Serve HTTPS by mounting a certificate and key and setting
`STATUSGUMBO_TLS_CERT` and `STATUSGUMBO_TLS_KEY` to their paths, or put an
HTTPS proxy in front and set `STATUSGUMBO_BEHIND_TLS_PROXY=1`. Without
either, the log warns that the token travels unencrypted.

### As a systemd user service (Linux)

`./install.sh` installs and starts `systemd/statusgumbo.service` from the
checkout. Machine-local options go in `~/.config/statusgumbo/collector.env`,
for example:

    STATUSGUMBO_COLLECTOR_ARGS=--cloud
    STATUSGUMBO_TOKEN_FILE=/home/user/.config/statusgumbo/collector-token

`install.sh` also links this checkout's `report.sh` and prints the manual
status-line setup. `install-reporter.sh` does that part better; use it.

## The reporter

### One command

    sh install-reporter.sh --url URL [--token-file FILE | --token TOKEN] [--place WORD] [--socket PATH]
    curl -fsSL https://raw.githubusercontent.com/shappski/StatusGumbo/master/install-reporter.sh \
      | sh -s -- --url URL --token TOKEN

It installs `~/.claude/statusgumbo-report.sh`, saves the URL, token and place
under `~/.config/statusgumbo/` (the token with mode 600), and merges into
`~/.claude/settings.json`:

- `statusLine` points at `~/.claude/statusgumbo-statusline.sh`. That wrapper
  reports each tick, then runs **your** status line with the same input, so
  the line draws exactly what it drew before. Your command is saved in
  `~/.config/statusgumbo/statusline-command`, and your script is never edited.
  With no status line, the wrapper draws nothing. A status line that already
  calls the reporter is left alone.
- `refreshInterval: 10`, if it isn't set. Without it, an idle session stops
  ticking and leaves the page.
- Two hooks for dialogs. Claude Code doesn't run the status line while a
  question, plan approval or permission prompt is open, so without them the
  session that's waiting on *you* would disappear.

The file is validated before it's replaced, a dated backup is left beside it,
and a symlinked `settings.json` (common with dotfiles) stays a symlink.

- **`--check`** answers "is this machine still reporting?" and says what's
  wrong. Run it whenever a machine goes quiet: some tools rewrite
  `settings.json` and drop `refreshInterval` or `statusLine`. It also warns when
  the current directory's project settings define their own `statusLine`, which
  wins over yours there.
- **`--uninstall`** puts `settings.json` back.

A Claude Code plugin can't do this job. Plugins can ship hooks, but not
`statusLine` or `refreshInterval`.

### By hand

Append this to the end of your own status-line script, after it renders:

    # StatusGumbo: fire-and-forget publish; no-op if the reporter is absent.
    if [ -x "$HOME/.claude/statusgumbo-report.sh" ]; then
        printf '%s' "$input" |
            STATUSGUMBO_URL="${STATUSGUMBO_URL:-http://127.0.0.1:4747}" \
            "$HOME/.claude/statusgumbo-report.sh"
    fi

`report.sh` prints nothing, returns in milliseconds, and does nothing without a
URL, so this is safe on any machine. Use `if`, not `[ -x … ] && …`. As the
script's last command, a short-circuited `&&` returns 1, so the status line
would fail wherever the reporter isn't installed.

Don't rely on an export in your shell rc for the URL or token. Claude Code
inherits the environment of whatever launched it, and a status line started from
fish, a desktop launcher or tmux never sees your `.bashrc`. The snippet supplies
a default. `~/.config/statusgumbo/url` and `~/.config/statusgumbo/token` work
everywhere.

Then, in the settings file that wins for your sessions, set
`"refreshInterval": 10` beside the status line's `command`, and merge in the
waiting hooks:

```json
{
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
}
```

`report.sh --wait` re-sends the session's last tick while the dialog is open,
and stops when real ticks resume or the `claude` process exits, so a killed
terminal leaves no ghost card.

### Reporter settings

| File in `~/.config/statusgumbo/` | Environment | Meaning |
|---|---|---|
| `url` | `STATUSGUMBO_URL` | The collector. The environment wins. |
| `token` | `STATUSGUMBO_TOKEN`, `STATUSGUMBO_TOKEN_FILE` | The shared token, if the collector has one. |
| `place` | `STATUSGUMBO_PLACE` | Where this machine is ([below](#where-a-session-runs)). |
| `socket` | `STATUSGUMBO_SOCKET` | Post through this Unix socket instead of TCP, for a tunnel that forwards to one ([contrib/coder](contrib/coder/README.md#on-a-shared-machine)). The URL then defaults to `http://localhost`. |
| | `STATUSGUMBO_HOST` | The name to report under. Default: `hostname -s`. Set it everywhere or nowhere, or one machine splits into two headings. |

## Where a session runs

Each heading and card shows where the session runs, as a glyph and a coloured
left edge. A machine states its place as one word of `a-z 0-9 -`, at most 24
characters, in `~/.config/statusgumbo/place` or `STATUSGUMBO_PLACE`.

- `laptop` 💻 (blue), `coder` 🖥️ (purple) and `cloud` ☁️ (cyan) have their own
  glyph and colour. Any other word (`desktop`, `vm-1`, `ci`) is shown as
  written, with a grey edge.
- With nothing stated, a session inside a Coder workspace (`CODER=true` or
  `CODER_WORKSPACE_NAME` set) reports `coder`. Anything else gets no label.
  A remote machine isn't assumed to be anything.

## Opening a session on claude.ai

A cloud card opens its claude.ai page when tapped. So does a local card whose
session has Remote Control on (`/rc`), so you can answer it from the phone. The
reporter reads the session's Remote Control id from its transcript, and the
collector builds the link only from an id it has validated. A session without
Remote Control has no claude.ai page, so its card has no link.

## Cloud sessions

Sessions on claude.ai/code have no status line of ours. With `--cloud`, the
collector polls `GET https://api.anthropic.com/v1/code/sessions` once a minute,
using the claude.ai login that Claude Code keeps on the collector's machine
(`~/.claude/.credentials.json`, `~/.claude.json`, read only). It shows each
session's state (working, waiting on you and for what, ready for review, done),
repo, branch, context and model.
Sessions started by a routine, which that list leaves out, are found through
`GET /v1/code/triggers` and shown the same way.

**It is off by default, and unofficial.**

- The endpoint is undocumented. It's what Claude Code itself calls, and it can
  change without notice. When it does, the section shows the error rather than
  going quietly empty.
- It shows only the account logged in on the collector's machine. Turn it on
  only on your own machine. A collector run for other people must never offer
  it, because it would mean holding their tokens.
- The collector never refreshes the login. When it lapses, the section says
  so, and running any local `claude` session refreshes it.

## A machine that can't reach the collector

A machine that can reach the collector's URL just posts to it. For one that
can't, such as a cloud VM with no route back to your laptop, `contrib/coder/`
has an SSH reverse tunnel that the collector's machine opens, and a `tunnel
down` card for when it drops. See [contrib/coder/README.md](contrib/coder/README.md).

## Development

    python3 -m unittest discover -s tests -t . -v
    sh tests/test_report.sh
    sh tests/test_heartbeat.sh
    sh tests/test_tunnel.sh

No third-party dependencies, by design. `test_tunnel.sh` takes about half a
minute, because it measures real retry delays.

| Path | Purpose |
|---|---|
| `report.sh` | The reporter: payload on stdin, POST out, prints nothing |
| `statusline-wrapper.sh` | Installed as the status line; reports, then runs yours |
| `install-reporter.sh` | Reporter install, `--check`, `--uninstall` |
| `collector/server.py` | HTTP: `/ingest`, `/api/sessions`, `/`, `/healthz` |
| `collector/store.py` | In-memory sessions and hosts, expiry |
| `collector/usage.py` | Pace maths for the usage windows |
| `collector/cloud.py` | The opt-in cloud-session poller |
| `collector/index.html` | The page |
| `Dockerfile` | The collector image |
| `install.sh`, `systemd/` | The collector as a systemd user service |
| `contrib/coder/` | Optional SSH reverse tunnel |

## License

[GNU Affero General Public License v3.0](LICENSE). If you run a modified
collector for other people over a network, you must offer them its source.
