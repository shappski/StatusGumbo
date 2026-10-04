"""HTTP surface for StatusGumbo.

By default it binds loopback (the local reporter, and the inbound end of the
SSH tunnel from a remote machine) and the Tailscale address (the phone), and
needs no token: only you can reach either. Any other address, 0.0.0.0
included, would expose client project names, branch names and costs to
whoever else is on that network, so --bind refuses one unless a shared token
is set (plan_binds).
"""

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from collector.cloud import CloudPoller
from collector.store import SessionStore, local_host_name, local_place

MAX_BODY_BYTES = 256 * 1024
IDLE_TIMEOUT_SECS = 60
INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
# What a phone needs to put the page on its home screen. A fixed table, so no
# part of a request path is ever joined onto a filesystem path. Served without
# the token, like /healthz: Chrome fetches the manifest without credentials,
# and none of it says anything about the sessions.
STATIC_FILES = {
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icons/icon-192.png": ("icons/icon-192.png", "image/png"),
    "/icons/icon-512.png": ("icons/icon-512.png", "image/png"),
    "/icons/icon-maskable-512.png": ("icons/icon-maskable-512.png", "image/png"),
}
TUNNEL_UNIT = "statusgumbo-tunnel.service"
STAMP_PLACEHOLDER = "{{served_at}}"
COOKIE_NAME = "statusgumbo_token"
# A year. The phone opens the token link once and should not be asked again;
# rotating the token is how access is withdrawn.
COOKIE_MAX_AGE = 365 * 24 * 3600
# Long enough not to be guessed over HTTP, and restricted to characters that
# survive a cookie and a URL without quoting.
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{16,}$")
# About 128 bits if the characters are random: shorter is accepted, with a
# warning, since the token is the only thing between the network and the page.
TOKEN_WARN_BELOW = 22
# Failed sign-ins one client may make in a minute before it gets 429s.
AUTH_FAILURES_PER_MIN = 20
SCRIPT_RE = re.compile(rb"<script>(.*?)</script>", re.S)
# Spelled out rather than taken from strftime("%b"), which follows the
# process locale. The page's tick() rewrites this stamp in the same shape
# with a fixed list of its own, and the two must not disagree on first poll.
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def stamp_text(epoch):
    """"5 Sep 14:02", in the collector's local time.

    The date is not decoration. The stamp exists for an offline copy seen a
    day later, and a bare 14:02 read at 14:05 the next day looks three
    minutes old.
    """
    t = time.localtime(epoch)
    return "%d %s %02d:%02d" % (t.tm_mday, MONTHS[t.tm_mon - 1], t.tm_hour, t.tm_min)


def _classify_tunnel(stdout, link_state=None):
    """Map `systemctl show` output and the wrapper's link state to up/down/unknown.

    Split out from the subprocess call so the classification — the part with
    the actual judgement in it — is testable without systemd.

    LoadState is checked before ActiveState, and that ordering is the whole
    point: `systemctl is-active` prints "inactive" for a unit that was never
    installed, which is indistinguishable from a configured tunnel that is
    down. Treating "never configured" as a fault would false-alarm on every
    machine without a tunnel — the exact failure this signal exists to avoid.

    `link_state` is needed because ExecStart is no longer ssh. It is a wrapper
    that keeps looping across a dropped connection, so `active` alone would
    report a tunnel that is up while the wrapper is merely sleeping between
    failed attempts. The two are read together, and the unit still holds the
    veto: a wrapper killed outright leaves a stale "up" behind, and systemd
    noticing the unit is gone must beat that file. Absence of the file is not
    a fault — a machine still running the plain-ssh unit has none, and there
    `active` does mean connected.
    """
    lines = [line.strip() for line in (stdout or "").splitlines()]
    if len(lines) < 2:
        return "unknown"
    load_state, active_state = lines[0], lines[1]
    if load_state != "loaded":
        return "unknown"
    if active_state != "active":
        return "down"
    if (link_state or "").strip() == "down":
        return "down"
    return "up"


def tunnel_link_path():
    """Where the tunnel wrapper publishes whether ssh is currently connected.

    The runtime dir, not the home dir, because the answer is worthless once
    the session it describes has ended: a stale "up" surviving a reboot would
    silence the alarm exactly when it is most needed. tmpfs forgets for us.
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return os.path.join(base, "statusgumbo", "tunnel.state")


def read_link_state():
    """The wrapper's last published link state, or None if it has not said."""
    try:
        with open(tunnel_link_path()) as handle:
            return handle.read()
    except OSError:
        return None


def tunnel_state():
    """Whether the reverse tunnel to the remote machine is up.

    A host's silence cannot answer this: a cleanly ended session and a dead
    tunnel are identical in the status-line stream. systemd actually knows.
    """
    try:
        result = subprocess.run(
            [
                "systemctl", "--user", "show",
                "-p", "LoadState", "-p", "ActiveState",
                "--value", TUNNEL_UNIT,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return {"state": "unknown", "unit": TUNNEL_UNIT}
    return {
        "state": _classify_tunnel(result.stdout, read_link_state()),
        "unit": TUNNEL_UNIT,
    }


def tailscale_ipv4():
    """This node's Tailscale IPv4, or None if Tailscale is unavailable."""
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return lines[0] if lines else None


def tailscale_names():
    """This node's Tailscale machine name and MagicDNS name, or nothing."""
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        own = json.loads(result.stdout).get("Self") or {}
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return set()
    return {
        name for name in (own.get("HostName"), own.get("DNSName"))
        if isinstance(name, str) and name
    }


def _clean_name(name):
    return name.strip().rstrip(".").lower()


def allowed_host_names(extra=(), tailnet=None):
    """The names the page and the reporters may use to reach this collector.

    Used only without a token, where the bind is loopback and the tailnet.
    An IP literal is always accepted (see host_allowed); these are the names:
    localhost, this machine's hostname and its first label, its Tailscale
    names, and whatever the operator adds with --allow-host.
    """
    names = {"localhost"}
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = ""
    if hostname:
        names.update((hostname, hostname.split(".")[0]))
    names.update(tailscale_names() if tailnet is None else tailnet)
    names.update(extra)
    return frozenset(_clean_name(name) for name in names if name and name.strip())


def host_allowed(header, allowed):
    """Is a request's Host header one this collector answers to?

    The defence against DNS rebinding. A page on an attacker's domain can
    have that domain re-resolve to 127.0.0.1 or the tailnet address, and the
    browser then treats this collector as the attacker's own origin and lets
    the page read /api/sessions. Its Host header still names the attacker's
    domain, which is what this checks. An IP literal cannot be rebound, so
    any is accepted. A request with no Host at all (HTTP/1.0) is not from a
    browser, and is accepted too.
    """
    if header is None:
        return True
    text = header.strip()
    if text.startswith("["):
        name = text[1:text.find("]")] if "]" in text else text[1:]
    elif text.count(":") == 1:
        name = text.split(":")[0]
    else:
        name = text
    name = _clean_name(name)
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return name in allowed


def _refuse_constant(name):
    # json.loads accepts the bare words NaN, Infinity and -Infinity, which no
    # reporter sends and which the page's JSON.parse could not read back.
    raise ValueError("non-finite number %s" % name)


def load_token(environ, path):
    """The shared token, or None when none is configured.

    An explicit file (--token-file) wins, then STATUSGUMBO_TOKEN_FILE, then
    STATUSGUMBO_TOKEN. A file that was named but cannot be read is an error,
    not "no token": falling back to an open collector because of a typo in a
    path is the one outcome this must never have.
    """
    path = path or environ.get("STATUSGUMBO_TOKEN_FILE")
    if path:
        try:
            with open(path) as handle:
                token = handle.read().strip()
        except OSError as error:
            raise ValueError("cannot read token file %s: %s" % (path, error))
    else:
        token = environ.get("STATUSGUMBO_TOKEN", "").strip()
        if not token:
            return None
    if not TOKEN_PATTERN.match(token):
        raise ValueError(
            "token must be at least 16 characters of A-Z a-z 0-9 . _ ~ -"
        )
    if path:
        try:
            if os.stat(path).st_mode & 0o077:
                print("warning: %s is readable by other users; chmod 600 it" % path,
                      file=sys.stderr)
        except OSError:
            pass
    if len(token) < TOKEN_WARN_BELOW:
        print("warning: the token is %d characters; use at least %d random ones"
              % (len(token), TOKEN_WARN_BELOW), file=sys.stderr)
    return token


# Tailscale hands out addresses from the CGNAT range and its own IPv6 ULA.
# Only tailnet members can reach them, which is what lets the default setup
# run without a token.
TAILNET_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)


def is_private_bind(text):
    """Loopback or tailnet: only the owner can reach it. ValueError if not an IP."""
    address = ipaddress.ip_address(text)
    return address.is_loopback or any(
        address in network for network in TAILNET_NETWORKS
        if network.version == address.version
    )


def tls_context(cert, key):
    """A server TLS context from a PEM certificate chain and key."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return context


def plan_binds(requested, tailnet_ip, token):
    """The addresses to listen on, or ValueError if that would be unsafe.

    Nothing requested means the long-standing default: loopback, plus the
    tailnet address when there is one. A requested address that is neither
    loopback nor on the tailnet -- 0.0.0.0 included -- is reachable by people
    who are not the owner, so it needs a token. Literal IPs only: letting DNS
    decide what gets exposed is not a security decision.
    """
    if not requested:
        requested = ["127.0.0.1"] + ([tailnet_ip] if tailnet_ip else [])
    plan = []
    for text in requested:
        try:
            private = is_private_bind(text)
        except ValueError:
            raise ValueError("--bind takes an IP address, not %r" % text)
        if not private and token is None:
            raise ValueError(
                "binding %s exposes the collector beyond loopback and the "
                "tailnet; set a token first (see README, Shared token)" % text
            )
        if text not in plan:
            plan.append(text)
    return plan


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    tls = None
    handshake_timeout = IDLE_TIMEOUT_SECS

    def finish_request(self, request, client_address):
        # The TLS handshake runs here, in the request's own thread, so a
        # client that stalls in it holds only that thread, never accept().
        if self.tls is None:
            super().finish_request(request, client_address)
            return
        request.settimeout(self.handshake_timeout)
        try:
            wrapped = self.tls.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            # A plain-HTTP client, a scanner, a dropped connection: nothing
            # worth a traceback in the journal.
            return
        try:
            super().finish_request(wrapped, client_address)
        finally:
            wrapped.close()


class _Ipv6Server(_Server):
    address_family = socket.AF_INET6


def cookie_value(token):
    """What the page's cookie holds: derived from the token, never the token.

    The cookie lives in a browser for a year. Holding a derived value, it
    can open the page but never post a tick, which needs the token itself.
    """
    return hmac.new(token.encode(), b"statusgumbo page cookie", hashlib.sha256).hexdigest()


def content_security_policy(body):
    """A CSP for the page that allows exactly its own inline script."""
    hashes = " ".join(
        "'sha256-%s'" % base64.b64encode(hashlib.sha256(script).digest()).decode()
        for script in SCRIPT_RE.findall(body)
    )
    # Styles must allow inline: the cards' bars are style attributes.
    return ("default-src 'none'; script-src %s; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; manifest-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'" % (hashes or "'none'"))


def make_server(address, store, clock=time.time, idle_timeout=IDLE_TIMEOUT_SECS,
                cloud=None, token=None, allowed_hosts=None, tls=None):
    def matches(candidate, secret=token):
        # Constant-time, so response timing does not leak how much of a guess
        # was right.
        return candidate is not None and hmac.compare_digest(
            candidate.encode(), secret.encode()
        )

    cookie = cookie_value(token) if token is not None else None

    # Per client address: failed sign-ins this minute, and when each kind of
    # refusal was last logged. Bounded by clearing; losing the counts only
    # forgives a guesser for a minute.
    failures = {}
    logged = {}
    guard = threading.Lock()

    def note(kind, client, detail=""):
        # One journal line per kind of refusal per client per minute: enough
        # to notice a guesser or a forged host, not enough to fill the disk.
        now = time.monotonic()
        with guard:
            if len(logged) > 1024:
                logged.clear()
            if now - logged.get((kind, client), -60) < 60:
                return
            logged[(kind, client)] = now
        print("%s from %s%s" % (kind, client, detail), file=sys.stderr)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        # A socket read timeout, so a keep-alive connection that goes quiet
        # releases its thread. A phone that sleeps or drops off Tailscale
        # abandons its socket without a FIN, and with no timeout the thread
        # waiting on it never returns — the live collector was found holding
        # 81 of them, the oldest idle for 159h. It applies per wait, so a page
        # polling every REFRESH_MS keeps its connection.
        timeout = idle_timeout

        def _respond(self, code, body=b"", content_type="text/plain; charset=utf-8",
                     headers=()):
            # Some error paths reject a request without having read its body:
            # an unknown path, or a Content-Length over the cap. Under
            # HTTP/1.1 keep-alive those unread bytes stay in the socket and
            # the next request on that connection parses them as garbage.
            # Closing is the correct and cheap answer — it avoids reading a
            # huge body purely to discard it, and the reporter opens a fresh
            # connection per tick regardless. Same desync class as the 204
            # rule below, on the request side rather than the response side.
            if code >= 400:
                self.close_connection = True
            self.send_response(code)
            if self.close_connection:
                # Setting close_connection alone only stops the *server's*
                # serve loop from reusing the socket; the client has no way
                # to know that unless we say so on the wire.
                self.send_header("Connection", "close")
            # A 204 must carry neither Content-Length nor a body (RFC 7230).
            # Sending them under HTTP/1.1 keep-alive can desynchronise the
            # connection, and /ingest is the hottest route here.
            if code != 204:
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
            # Every response is exactly the type it says.
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            if body and code != 204:
                self.wfile.write(body)

        def _bearer(self):
            header = self.headers.get("Authorization") or ""
            scheme, _, value = header.partition(" ")
            return value.strip() if scheme.lower() == "bearer" else None

        def _cookie(self):
            try:
                cookie = SimpleCookie(self.headers.get("Cookie") or "")
            except CookieError:
                return None
            morsel = cookie.get(COOKIE_NAME)
            return morsel.value if morsel else None

        def _client(self):
            return self.client_address[0] if self.client_address else "?"

        def _throttled(self):
            """True, having answered 429, if this client has failed too often."""
            now = time.monotonic()
            with guard:
                start, count = failures.get(self._client(), (now, 0))
            if now - start < 60 and count >= AUTH_FAILURES_PER_MIN:
                note("429 too many failed sign-ins", self._client())
                self._respond(429, b"too many failed attempts; wait a minute",
                              headers=[("Retry-After", "60")])
                return True
            return False

        def _refuse(self):
            now = time.monotonic()
            with guard:
                if len(failures) > 1024:
                    failures.clear()
                start, count = failures.get(self._client(), (now, 0))
                if now - start >= 60:
                    start, count = now, 0
                failures[self._client()] = (start, count + 1)
            note("401 wrong or missing token", self._client())
            self._respond(
                401,
                b"unauthorized: open this page once as /?t=<token>",
                headers=[("WWW-Authenticate", 'Bearer realm="statusgumbo"')],
            )

        def _host_refused(self):
            # None means no Host check: with a token, a rebound page has
            # neither the bearer nor the cookie (which is scoped to the real
            # name), so it can read and write nothing.
            if allowed_hosts is None or host_allowed(self.headers.get("Host"), allowed_hosts):
                return False
            note("421 unknown Host", self._client(), ": %r" % self.headers.get("Host"))
            self._respond(421, b"misdirected request: unknown Host")
            return True

        def do_POST(self):
            if self.path != "/ingest":
                self._respond(404, b"not found")
                return
            if self._host_refused():
                return
            # Reporters are curl and send neither header; a browser sends
            # Origin on every POST. Without this, any page open in a browser
            # on this machine could post ticks to a collector with no token:
            # a text/plain body is a "simple" request, sent without a CORS
            # preflight. The page itself never posts.
            site = self.headers.get("Sec-Fetch-Site")
            if self.headers.get("Origin") is not None or site not in (None, "none"):
                note("403 browser post", self._client(), ": Origin %r" % self.headers.get("Origin"))
                self._respond(403, b"forbidden: browsers may not post here")
                return
            # Reporters authenticate with the header only. A cookie here would
            # let any page the phone happens to visit post sessions.
            # Checked before the body is read; the 401 closes the connection,
            # so the unread body cannot desynchronise it.
            if token is not None and self._throttled():
                return
            if token is not None and not matches(self._bearer()):
                self._refuse()
                return
            # Every reporter sends JSON as JSON. A browser cannot send this
            # type cross-site without a preflight, which this server never
            # answers, so it is a second lock on the same door.
            content_type = (self.headers.get("Content-Type") or "").split(";")[0]
            if content_type.strip().lower() != "application/json":
                note("415 not JSON", self._client())
                self._respond(415, b"unsupported media type: send application/json")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._respond(400, b"bad request")
                return
            if length <= 0 or length > MAX_BODY_BYTES:
                self._respond(400, b"bad request")
                return
            raw = self.rfile.read(length)
            try:
                envelope = json.loads(raw, parse_constant=_refuse_constant)
                new_host = store.ingest(envelope, clock())
            except (ValueError, UnicodeDecodeError, RecursionError):
                # RecursionError is JSON nested too deep to parse: malformed,
                # not a surprise worth a traceback.
                # Malformed input is dropped without disturbing state.
                self._respond(400, b"bad request")
                return
            except Exception:
                # Deliberately broad. The store is fed by an upstream schema
                # nobody here controls, and enumerating the exceptions we
                # expect from it is precisely how one bad tick took the whole
                # page down: a TypeError escaped this handler as a dropped
                # connection with no status at all. An unexpected payload
                # must cost one dropped sample, never the service.
                #
                # log_message is silenced, so stderr is the only route to the
                # journal — and an unexplained 400 would be worse than the
                # crash it replaces.
                traceback.print_exc(file=sys.stderr)
                self._respond(400, b"bad request")
                return
            if new_host:
                # repr: the name came off the wire, and a newline in it must
                # not forge a second journal line.
                print("new host %r from %s" % (envelope.get("host"), self._client()),
                      file=sys.stderr)
            self._respond(204)

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            path = url.path
            # Open even with a token: a container HEALTHCHECK has none, and
            # this says only that the process serves HTTP.
            if path == "/healthz":
                self._respond(200, b"ok")
                return
            if self._host_refused():
                return
            if token is not None and path in ("/", "/api/sessions"):
                if self._throttled():
                    return
                offered = urllib.parse.parse_qs(url.query).get("t", [None])[0]
                if path == "/" and offered is not None:
                    if not matches(offered):
                        self._refuse()
                        return
                    # Swap the link for a cookie and reload clean, so the
                    # token does not stay in the address bar or history.
                    # Secure only under TLS: the collector is usually plain
                    # HTTP on a tailnet, where Secure would drop the cookie.
                    self._respond(303, b"", headers=[
                        ("Location", "/"),
                        ("Set-Cookie", "%s=%s; Path=/; Max-Age=%d; HttpOnly; "
                         "SameSite=Strict%s" % (COOKIE_NAME, cookie, COOKIE_MAX_AGE,
                                                "; Secure" if tls else "")),
                    ])
                    return
                if not (matches(self._bearer()) or matches(self._cookie(), cookie)):
                    self._refuse()
                    return
            if path == "/api/sessions":
                try:
                    snapshot = store.snapshot(clock())
                    # Added here, not in the store: the store deals in facts
                    # about what hosts reported, and cannot know whether the
                    # tunnel is up. Only the machine running the collector
                    # can answer that.
                    snapshot["tunnel"] = tunnel_state()
                    # Same reasoning: cloud sessions are not reported by any
                    # host, they are polled. None when polling is off, which
                    # the page renders as nothing at all.
                    snapshot["cloud"] = cloud.view(clock()) if cloud else None
                    body = json.dumps(snapshot).encode()
                except Exception:
                    # Same reasoning as /ingest. Without this the connection
                    # simply dropped, and the page reported "collector
                    # unreachable" — blaming the network for a server bug.
                    traceback.print_exc(file=sys.stderr)
                    self._respond(500, b"internal error")
                    return
                self._respond(200, body, "application/json; charset=utf-8")
                return
            if path == "/":
                try:
                    with open(INDEX_PATH, "rb") as handle:
                        body = handle.read()
                except OSError:
                    self._respond(500, b"index.html missing")
                    return
                # When the phone is off Tailscale, Chrome shows a saved
                # offline copy of this page. That copy runs no JavaScript, so
                # tick() never says "collector unreachable" and old cards
                # read as live. Text written here, into the markup, is the
                # only thing such a copy can still show about its own age.
                body = body.replace(
                    STAMP_PLACEHOLDER.encode(), stamp_text(clock()).encode()
                )
                # And ask for no copy to be kept at all. Whether Chrome's
                # offline copy honours this could not be verified from the
                # laptop, so the stamp above is the part that must work
                # without it.
                self._respond(
                    200, body, "text/html; charset=utf-8",
                    headers=[("Cache-Control", "no-store"),
                             ("Content-Security-Policy", content_security_policy(body)),
                             ("Referrer-Policy", "no-referrer")],
                )
                return
            if path in STATIC_FILES:
                name, content_type = STATIC_FILES[path]
                try:
                    with open(os.path.join(os.path.dirname(INDEX_PATH), name), "rb") as handle:
                        body = handle.read()
                except OSError:
                    self._respond(404, b"not found")
                    return
                self._respond(200, body, content_type,
                              headers=[("Cache-Control", "max-age=86400")])
                return
            self._respond(404, b"not found")

        def log_message(self, fmt, *args):
            # Silent: at a 10s tick across several sessions, per-request
            # logging is pure noise in the journal.
            pass

    server_class = _Ipv6Server if ":" in address[0] else _Server
    server = server_class(address, Handler)
    server.tls = tls
    server.handshake_timeout = idle_timeout
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="StatusGumbo collector")
    parser.add_argument("--port", type=int, default=4747)
    parser.add_argument(
        "--bind", action="append", default=[],
        help="address to listen on (repeatable); also STATUSGUMBO_BIND, "
             "comma-separated. Default: loopback plus the Tailscale address. "
             "Anything else needs a token.",
    )
    parser.add_argument(
        "--token-file",
        help="file holding the shared token; also STATUSGUMBO_TOKEN_FILE "
             "or STATUSGUMBO_TOKEN. Without one, nothing is required.",
    )
    # Off unless asked for: it calls an undocumented endpoint with the
    # claude.ai login of whoever runs the collector (see README, Cloud
    # sessions). --no-cloud stays accepted, since existing units pass it.
    cloud_flag = parser.add_mutually_exclusive_group()
    cloud_flag.add_argument(
        "--cloud", dest="cloud", action="store_true",
        help="poll Anthropic for this account's cloud Claude Code sessions "
             "(undocumented endpoint; uses the local claude.ai login)",
    )
    cloud_flag.add_argument(
        "--no-cloud", dest="cloud", action="store_false",
        help="do not poll for cloud sessions (the default)",
    )
    parser.add_argument(
        "--allow-host", action="append", default=[],
        help="a name the page or a reporter uses to reach this collector "
             "(repeatable); also STATUSGUMBO_ALLOW_HOSTS, comma-separated. "
             "Without a token, requests naming any other host are refused. "
             "localhost, this machine's hostname, its Tailscale names and "
             "any IP address are always allowed.",
    )
    parser.add_argument(
        "--tls-cert", default=os.environ.get("STATUSGUMBO_TLS_CERT"),
        help="PEM certificate chain to serve HTTPS with; also "
             "STATUSGUMBO_TLS_CERT. Needs --tls-key.",
    )
    parser.add_argument(
        "--tls-key", default=os.environ.get("STATUSGUMBO_TLS_KEY"),
        help="PEM private key for --tls-cert; also STATUSGUMBO_TLS_KEY.",
    )
    parser.add_argument(
        "--behind-tls-proxy", action="store_true",
        default=os.environ.get("STATUSGUMBO_BEHIND_TLS_PROXY") == "1",
        help="say that HTTPS is terminated in front of this collector, which "
             "silences the plain-HTTP warning for a non-private bind",
    )
    parser.add_argument(
        "--no-local-host", action="store_true",
        help="do not list this machine on the page; for a collector that "
             "runs somewhere with no reporter of its own, such as a container",
    )
    args = parser.parse_args(argv)
    try:
        token = load_token(os.environ, args.token_file)
    except ValueError as error:
        print("error: %s" % error, file=sys.stderr)
        return 2

    requested = args.bind or [
        part.strip() for part in os.environ.get("STATUSGUMBO_BIND", "").split(",")
        if part.strip()
    ]
    tailnet_ip = None if requested else tailscale_ipv4()
    if not requested and not tailnet_ip:
        print(
            "warning: no Tailscale address; binding loopback only "
            "(the phone will not be able to reach this)",
            file=sys.stderr,
        )
    try:
        addresses = plan_binds(requested, tailnet_ip, token)
    except ValueError as error:
        print("error: %s" % error, file=sys.stderr)
        return 2

    if bool(args.tls_cert) != bool(args.tls_key):
        print("error: --tls-cert and --tls-key go together", file=sys.stderr)
        return 2
    tls = None
    if args.tls_cert:
        try:
            tls = tls_context(args.tls_cert, args.tls_key)
        except (OSError, ssl.SSLError) as error:
            print("error: cannot load the TLS certificate: %s" % error, file=sys.stderr)
            return 2
    # Beyond loopback and the tailnet the token rides every tick and every
    # page poll, so plain HTTP hands it to anyone on the path.
    if tls is None and not args.behind_tls_proxy:
        for address in addresses:
            if not is_private_bind(address):
                print(
                    "warning: serving plain HTTP on %s; the token crosses the "
                    "network unencrypted. Use --tls-cert/--tls-key, or put "
                    "HTTPS in front and pass --behind-tls-proxy." % address,
                    file=sys.stderr,
                )

    if args.no_local_host:
        store = SessionStore()
    else:
        store = SessionStore(local_host=local_host_name(), local_place=local_place())
    cloud = None
    if args.cloud:
        print(
            "warning: --cloud sends your Claude Code login token to an "
            "undocumented Anthropic endpoint; it is unofficial and may break "
            "or be disallowed (see README, Cloud sessions)",
            file=sys.stderr,
        )
        cloud = CloudPoller()
        threading.Thread(target=cloud.run_forever, daemon=True).start()

    allowed_hosts = None
    if token is None:
        allowed_hosts = allowed_host_names(args.allow_host or [
            part.strip()
            for part in os.environ.get("STATUSGUMBO_ALLOW_HOSTS", "").split(",")
            if part.strip()
        ])

    servers = []
    for address in addresses:
        server = make_server((address, args.port), store, cloud=cloud, token=token,
                             allowed_hosts=allowed_hosts, tls=tls)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print("listening on %s://%s:%d%s" % (
            "https" if tls else "http",
            "[%s]" % address if ":" in address else address, args.port, " (token required)" if token else ""
        ), file=sys.stderr)

    # As PID 1 in a container, Python ignores SIGTERM unless it has a handler,
    # so `docker stop` would wait out its timeout and then kill. Treated like
    # Ctrl-C; systemd's stop gets the same clean exit.
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        for server in servers:
            server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
