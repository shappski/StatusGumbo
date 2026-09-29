# Architecture

StatusGumbo shows every running Claude Code session on one phone page: context use, the account's 5-hour and 7-day usage windows, and whether each session is alive. It costs no tokens. Everything it shows is data Claude Code already hands to its status line.

This document explains how the parts fit together and why the load-bearing decisions are what they are. The README covers installing and running it.

## Pieces

| Piece | Where | Runs on |
|---|---|---|
| Reporter | `report.sh` | Every machine that runs Claude Code, from the status line and two hooks |
| Status-line wrapper | `statusline-wrapper.sh`, installed as `~/.claude/statusgumbo-statusline.sh` | Same machines; runs the reporter, then the user's own status line |
| Reporter installer | `install-reporter.sh` (`--check`, `--uninstall`) | Same machines, once |
| Collector | `collector/server.py`, `collector/store.py`, `collector/usage.py` | One machine, or the Docker image (`Dockerfile`) |
| Cloud poller | `collector/cloud.py` | Inside the collector, only with `--cloud` |
| Page | `collector/index.html` | The phone's browser |
| Tunnel | `contrib/coder/tunnel.sh`, `contrib/coder/statusgumbo-tunnel.service` | Optional; the collector's machine |

The collector is Python 3 standard library only. The reporter is POSIX `sh` and needs `curl` and `jq`. There are no other dependencies, by design.

## Data flow

```
machine (laptop, vm-1, ...)                 collector                     phone
Claude Code
  statusLine --> wrapper --> report.sh --POST /ingest--> SessionStore <--GET /api/sessions-- page
             \-> your own status line                    CloudPoller --GET--> api.anthropic.com
  hooks ------------------> report.sh --wait                (only with --cloud)
```

1. Claude Code runs the status-line command on events and every `refreshInterval` seconds, with a JSON payload on stdin.
2. The wrapper passes that payload to `~/.claude/statusgumbo-report.sh`, then to the user's saved command, so the terminal draws what it drew before.
3. The reporter wraps the payload in an envelope `{host, branch, payload}`, plus `where` (the machine's place) and `bridge` (a Remote Control id) when known, and POSTs it to `$STATUSGUMBO_URL/ingest`. It reads the URL and token from `~/.config/statusgumbo/url` and `~/.config/statusgumbo/token` when they are not in the environment.
4. The collector keeps one in-memory record per `(host, session_id)`, one record per host, and one account-wide rate-limit snapshot. There is no database: history covers minutes, and a restart repopulates within one tick.
5. The page polls `GET /api/sessions` every 5 seconds (`REFRESH_MS`) and renders it. The response carries `now`, `rate_limits`, `rate_limits_stale_as_of`, `hosts`, `sessions`, `tunnel` and `cloud`.

`branch` comes from the reporter running `git rev-parse` in the payload's `workspace.current_dir`, because the payload has no branch and the collector cannot run git on a remote machine. Rate limits are stored once, not per session, because they belong to the account: storing them per session would let two hosts disagree on screen. A payload carries the snapshot its session last received, so an idle session keeps re-sending an old one. The store therefore orders snapshots by `(resets_at, used_percentage)` and ignores one that is older than what it holds, so an idle session can't drag the page back to yesterday's window.

Cards are ordered by host, then project, then `session_id`, never by context use. A card must stay where it was between 5-second refreshes, or it slides out from under a thumb.

## Why a status-line hook, not a fork

The status line is the only place Claude Code publishes this data, and it is also something the user sees all day. The rule every reporter decision follows is: **the status line must never get slower or break because of StatusGumbo.**

- **Never replace the user's status line.** An earlier version shipped its own `statusline.sh`, forked from one kept in a dotfiles repo. Within a day the two had diverged, and the fork kept drifting, which its own golden test did not catch. The installer now wraps the existing command: it saves the command to `~/.config/statusgumbo/statusline-command`, points `statusLine` at the wrapper and never edits the user's script. A status line that already calls the reporter is left alone.
- **Prints nothing.** The reporter's stdout is the rendered line, and a stray byte corrupts it.
- **Detached and fully redirected.** The CLI reads the status line until EOF, so a background child holding stdout would stall the draw. The POST runs in a braced, backgrounded group with every descriptor redirected. Without the braces, dash forks a backgrounded function before applying its redirections, and the line waited for the whole curl timeout (about 1 s instead of 8 ms).
- **`setsid`.** Claude Code cancels the running status-line script when the next update fires. A plain child shares its process group and dies mid-POST.
- **`curl -m 1`** bounds a wedged socket.
- **No-op without configuration.** No URL, no `curl` or no `jq` means `exit 0`. A machine without StatusGumbo behaves exactly as before.
- **Configuration lives in files, not the shell.** Claude Code inherits the environment of whatever launched it, and an export in `~/.bashrc` reaches only interactive bash. Launched from fish, a desktop launcher or a unit, the session would draw perfectly and report nothing. So the URL, token and place are read from `~/.config/statusgumbo/`.
- **`refreshInterval: 10` is required.** Without it the status line runs only on events, and those go quiet while a session idles at the prompt, so a live session leaves the page. The installer sets it when unset. `install-reporter.sh --check` fails if it is missing or above 10, because some tools rewrite `settings.json` and drop the key.
- **Not a plugin.** Plugins can ship hooks but not `statusLine` or `refreshInterval`.

The installer merges `~/.claude/settings.json` with `jq`, checks that the result is a JSON object before replacing anything, leaves a dated backup, and writes through a symlink instead of over it. A project's own `.claude/settings.json` `statusLine` overrides the user's in that directory, and `--check` reports it.

## The heartbeat and the waiting hooks

Claude Code hides the status line while a dialog is open: a question, a plan approval, a permission prompt. `refreshInterval` doesn't override that. So a session waiting on you sends no ticks and would leave the page after `ACTIVE_SECS`. That session is the one the page most needs to show. Before this fix, a session waiting on a question vanished from the page and came back seconds after the answer.

The fix is in the reporter only. The collector still receives ticks and nothing else.

1. Every tick caches its payload at `<state>/sessions/<session_id>.json`, written to a temporary file and renamed. `<state>` is `$STATUSGUMBO_STATE_DIR`, else `$XDG_RUNTIME_DIR/statusgumbo`, else `/tmp/statusgumbo-<uid>`. Only a `[A-Za-z0-9-]` session id becomes a file name.
2. Two hooks call `report.sh --wait` as a wait begins: `PreToolUse` matching `AskUserQuestion|ExitPlanMode`, and `PermissionRequest`. The hook returns at once, prints nothing, and starts one detached `--heartbeat` loop per session, claimed with a noclobber pidfile so two dialogs still give one loop.
3. The loop re-sends the cached tick every `STATUSGUMBO_HEARTBEAT_SECS` (default 10). A cache younger than two intervals means real ticks are arriving, so it sends nothing. After three such cycles in a row it exits.
4. The loop watches the Claude Code process and stops when it exits, so a killed terminal leaves no ghost card. It finds the process by name (`claude`, or `node` for an npm install) among the hook's nearest three ancestors. A deeper search could find some other `claude` a nested shell runs under. With no ancestor found, no loop starts. A one-day cap is a backstop only.

Re-sending an old payload is honest. Nothing about the session changes while it waits, and the card claims only that it is alive. The collector already refuses to let an older rate-limit snapshot replace a newer one.

Rejected: a `Notification` hook, because none fires for a question and the ones that exist fire once. A "waiting" state in the collector, because it receives ticks, not sessions. A longer `ACTIVE_SECS`, because a wait has no bound and every real exit would linger. Not covered: dialogs without a hook, such as an MCP elicitation form.

## Say only what is known

The collector receives ticks. It does not receive sessions. A live session that has stopped ticking and no session at all produce exactly the same thing: nothing. Every line on the page must be supported by something the collector observed.

- **A quiet host says `not reporting · last tick HH:MM`,** never "no sessions". Both readings are possible, and the collector can't tell which. A host listed as having no sessions once had four live ones whose status lines had stopped firing. Someone who knows they left a session running now sees a line that disagrees with them, which prompts them to look. The time is a clock, not a relative age, so the line doesn't change while you read it.
- **The collector's own host is always listed.** It is the one name the process can vouch for without a tick, so it is exempt from `DROP_HOST_SECS` and appears with `no ticks yet` before it has ever reported. Otherwise a restart, or an hour of quiet, silently removes the machine serving the page. `local_host_name()` must derive the name exactly as `report.sh` does (`STATUSGUMBO_HOST`, else `hostname -s`), or one machine gets two headings. `--no-local-host` turns this off, and the Docker image uses it because its hostname is a container id.
- **Stale usage figures are withheld, not shown.** Pace is recomputed against a live clock, so a frozen "58% used" would drift from "21% behind" to "42% behind" and eventually show a reset time in the past. Past `RATE_LIMITS_MAX_AGE_SECS` (15 min) `rate_limits` is `null`, and `rate_limits_stale_as_of` carries the withheld figure's date. The page then says `nothing since HH:MM` instead of `not reported yet`. A window whose `resets_at` has passed is dropped from the view.
- **Missing numbers are blanks, never zeros.** Rate limits are absent for some accounts and before a session's first API response. A non-numeric (or boolean) `used_percentage` is stored as `None`.
- **Only the tunnel may call silence a fault.** The `tunnel down` card comes from asking systemd, not from interpreting a quiet host (see Tunnel).
- **Failures say what failed.** The page separates `collector unreachable` (fetch threw), `collector returned HTTP n`, an unreadable response and its own render error, instead of blaming the network for all four. A broken cloud poll shows its error (`HTTP 503`, `response has no session list`), never an empty list.
- **Old copies date themselves.** The collector writes `updated <date time>` into the HTML it serves and sends `Cache-Control: no-store`, because a browser's offline copy runs no script and would otherwise pass old cards off as live.
- **Cost is not shown.** The API carries `cost_usd`, but on a subscription plan a dollar figure reads as a bill that doesn't exist.

## Expiry and dropping

All times are measured from a record's last tick, against an injected clock in tests.

| Constant (`collector/store.py`) | Value | Meaning |
|---|---|---|
| `ACTIVE_SECS` | 45 s | A session is served only this long after its last tick: about four missed 10-second ticks. |
| `DROP_SESSION_SECS` | 15 min | The record, its `first_seen` and its history are kept this long, but never shown past `ACTIVE_SECS`. |
| `DROP_HOST_SECS` | 1 h | A host heading stays this long after its last tick, except the collector's own host. |
| `RATE_LIMITS_MAX_AGE_SECS` | 15 min | The usage windows are withheld after this. |
| `HISTORY_SLOTS` / `HISTORY_MIN_INTERVAL_SECS` | 180 / 10 s | The sparkline ring covers at least 30 minutes, and more when ticks are slower. The page captions each line with the span it actually covers. |

Visibility and retention are separate on purpose. Setting `DROP_SESSION_SECS` to `ACTIVE_SECS` would draw the same page, but a laptop asleep for fifty seconds would lose its sparkline and come back as a new session. A quiet session is not shown greyed out: a card that outlives its session by minutes answers a question nobody asked. There is no `state` field, since only live sessions are served. Adding a "stale" tier back would also reintroduce the bug where "not reporting" sat above a greyed card from the same host.

Distinguishing a clean exit from silence was rejected. A `Stop` hook tombstone can't fire for a killed terminal, so the timeout would still be needed, and it would save 45 seconds.

Cloud sessions follow the same rule: the last good list is served for `MAX_AGE_SECS` (5 min) in `collector/cloud.py`, then withheld and dated.

## Security model

The page shows project names, branches and session titles. By default only you can reach it.

- **Bind rules.** With no `--bind` or `STATUSGUMBO_BIND`, the collector listens on `127.0.0.1` plus this machine's Tailscale IPv4 (from `tailscale ip -4`), or loopback alone with a warning. Loopback and Tailscale ranges (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) need no token. Any other address, `0.0.0.0` and `::` included, makes the collector refuse to start unless a token is set. Only literal IPs are accepted, so DNS never decides what is exposed.
- **Token.** Set with `--token-file`, then `STATUSGUMBO_TOKEN_FILE`, then `STATUSGUMBO_TOKEN`. It must be at least 16 characters of `A-Z a-z 0-9 . _ ~ -`, so it survives a URL and a cookie without quoting. A malformed token, or a named file that can't be read, stops the collector from starting. A typo must never fall back to an open collector. Comparison uses `hmac.compare_digest`.
- **Reporter side.** The token is read from `~/.config/statusgumbo/token` (mode 600) and reaches curl through a config on file descriptor 3, never the command line, where `ps` would show it. A token with any other character is dropped, not sent.
- **`/ingest` takes only `Authorization: Bearer`.** A cookie is never accepted there, or any page the phone visits could post sessions. The token is checked before the body is read.
- **`/` and `/api/sessions`** accept the bearer header or the cookie. Opening `/?t=<token>` once answers `303` to `/` with a `statusgumbo_token` cookie: `HttpOnly`, `SameSite=Strict`, a year's `Max-Age`, so the token doesn't stay in the address bar or history. Rotating the token is how access is withdrawn. There is no `Secure` flag, because the usual setup is plain HTTP on a tailnet. Put HTTPS in front of a collector reachable from the internet.
- **`/healthz` is open.** It answers `ok` and says only that the process serves HTTP, so a container health check needs no token.
- **Input limits.** Bodies over 256 KiB (`MAX_BODY_BYTES`) are refused. Any error response closes the connection, so unread bytes can't desynchronise a keep-alive stream. Idle connections time out after 60 s (`IDLE_TIMEOUT_SECS`); without that, a phone that dropped off the network held its thread forever. Any exception while ingesting costs one sample (`400`, traceback to stderr), never the service.
- **Labels are validated, not cleaned.** `where` must match `^[a-z0-9-]{1,24}$` or it is dropped. A label nobody could vouch for is not shown. The page escapes every value it inserts.
- **No wire-supplied URL goes into an `href`.** The reporter sends only a `bridge` id. The collector builds `https://claude.ai/code/session_…` from it only if it matches `cse_[A-Za-z0-9]+`, and cloud links are built from the session id. A card with no collector-built URL has no link.

## Cloud sessions

A cloud session (claude.ai/code) has no status line of ours and can't reach a private collector, so nothing reports it. With `--cloud`, the collector polls `GET https://api.anthropic.com/v1/code/sessions` instead, and shows each non-archived `anthropic_cloud` session under a `cloud` heading, with its state, repo, branch, context and model.

It is off by default for three reasons. The endpoint is undocumented: it is what Claude Code itself calls, and it may change without notice. It uses the claude.ai login of whoever runs the collector (`~/.claude/.credentials.json` and `~/.claude.json`), and shows only that account. A collector run for other people must never offer it, because it would mean holding their tokens. The Docker image passes `--no-cloud`.

Behaviour that follows from building on an undocumented API:

- Every field is read defensively. A response without a `data` list is an error, not "no sessions". Remote Control entries for local sessions are filtered out, since those sessions already report themselves.
- The login files are read, never written. An expired token shows `login expired`, and any local `claude` session refreshes it. The collector doesn't run its own OAuth refresh and race Claude Code for the file.
- One list call a minute (`POLL_SECS`), doubling after failures up to 10 minutes. At most 3 pages, stopping at sessions quiet for 14 days.
- Sessions started by a routine (a scheduled or one-off trigger) are missing from that list. They're found from `GET /v1/code/triggers` (needs the `anthropic-beta: ccr-triggers-2026-01-30` header), whose entries name their `last_run.session_id`, then `GET /v1/code/sessions?trigger_id=…`. Only triggers that fired in the last 14 days are looked up, and a session once seen archived is never asked for again while the collector runs. If the trigger list fails, the whole poll fails, so routine cards never vanish without an error line. A recurring routine shows only its latest run.
- Cloud cards are ordered by creation time, which never changes.

## Tunnel (optional)

A machine that can reach the collector's URL just posts to it. `contrib/coder/` is for a remote machine that can't, such as a Coder workspace with no route back. The collector's machine runs `ssh -N -R 4747:127.0.0.1:4747 $CODER_HOST`, so the remote's loopback port lands on the collector, and the remote's reporter posts to `http://127.0.0.1:4747`. The laptop starts the connection, so the remote needs no inbound access, and `-R` binds only the remote's loopback.

- **Retry lives in `tunnel.sh`, not systemd.** systemd's restart backoff is derived from `NRestarts`, which a long healthy run never resets. After one bad night the tunnel sat at its ten-minute ceiling for good, so every laptop resume cost minutes of a blind page. The script doubles its delay from 10 s to 600 s and resets only after a connection lasted 15 minutes (`STATUSGUMBO_TUNNEL_HEALTHY_SECS`): failing attempts can last for minutes, too. The unit's `Restart=always` is only a backstop for the script dying.
- **`ControlMaster=no`, `ControlPath=none`.** With a user-level `ControlMaster auto`, ssh would hand the forward to an existing master and exit at once.
- **Link state is published.** The script writes `up` to `$XDG_RUNTIME_DIR/statusgumbo/tunnel.state` only after ssh has survived 5 s (`SETTLE_SECS`), and `down` otherwise. It's in the runtime directory so a stale `up` can't survive a reboot.
- **The page's alarm.** The collector reads `LoadState` and `ActiveState` of `statusgumbo-tunnel.service` via `systemctl --user show`, plus the state file. A unit that isn't loaded is `unknown` and draws nothing: `systemctl is-active` says `inactive` for a unit that doesn't exist, and treating that as down would raise false alarms on every machine without a tunnel. Loaded, but inactive or with the link `down`, draws the `tunnel down` card. Without systemd (in Docker, say), the card never appears.
- The unit sets `CODER_SSH_DISABLE_AUTOSTART=true` so the tunnel never wakes a stopped workspace.

## Testing

```sh
python3 -m unittest discover -s tests -t . -v
sh tests/test_report.sh
sh tests/test_heartbeat.sh
sh tests/test_tunnel.sh
```

- `test_store.py`, `test_usage.py` and `test_cloud.py` drive the store, pace maths and cloud parser with an injected clock and stubbed HTTP. Nothing sleeps. They include the case that justifies retention: a session quiet past `ACTIVE_SECS` returns with its history and `first_seen` intact.
- `test_server.py` runs real HTTP servers on loopback: auth, bind planning, the cookie swap, error codes and connection handling.
- `test_report.sh` checks that the reporter prints nothing, returns at once when the collector is down, no-ops without `curl` or a URL, and runs the README's status-line snippet as written.
- `test_heartbeat.sh` runs the hook under a copy of `sh` renamed `claude`, so the real ancestor search runs with no test-only override.
- `test_tunnel.sh` runs `tunnel.sh` against a stubbed `ssh` with shortened thresholds. It measures real delays and takes about half a minute.
- `test_install_reporter.py` and `test_install.py` run the installers against a throwaway `$HOME`.
- `test_index_html.py` pins the shape of fixes in the page's source. There is no JavaScript runtime in the suite, which is standard library only, so these are guards against regressions, not rendering tests.

Not automated: loading the page from a phone over the real network.
