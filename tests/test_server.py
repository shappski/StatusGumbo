import base64
import contextlib
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from collector.server import (
    IDLE_TIMEOUT_SECS,
    INDEX_PATH,
    MAX_BODY_BYTES,
    STAMP_PLACEHOLDER,
    AUTH_FAILURES_PER_MIN,
    _classify_tunnel,
    cookie_value,
    allowed_host_names,
    host_allowed,
    load_token,
    plan_binds,
    make_server,
    read_link_state,
    tailscale_ipv4,
    tls_context,
    tunnel_link_path,
    tunnel_state,
)
from collector.store import SessionStore

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PAYLOAD = {
    "session_id": "sess-1",
    "workspace": {"current_dir": "/home/user/workspace/demo-app"},
    "context_window": {"used_percentage": 27},
}


class TestTunnelClassification(unittest.TestCase):
    """Pure classification, no systemd required."""

    def test_loaded_and_active_is_up(self):
        self.assertEqual(_classify_tunnel("loaded\nactive\n"), "up")

    def test_loaded_but_inactive_is_down(self):
        self.assertEqual(_classify_tunnel("loaded\ninactive\n"), "down")

    def test_loaded_but_failed_is_down(self):
        self.assertEqual(_classify_tunnel("loaded\nfailed\n"), "down")

    def test_unit_never_installed_is_unknown_not_down(self):
        # The critical case. `systemctl is-active` alone prints "inactive"
        # here, identical to a configured tunnel that is genuinely down —
        # which would alarm on every machine that never had a tunnel.
        # LoadState separates them, and this test is what stops that
        # regression.
        self.assertEqual(_classify_tunnel("not-found\ninactive\n"), "unknown")

    def test_empty_output_is_unknown(self):
        self.assertEqual(_classify_tunnel(""), "unknown")

    def test_truncated_output_is_unknown(self):
        self.assertEqual(_classify_tunnel("loaded\n"), "unknown")

    # Once ExecStart is a retry wrapper rather than ssh itself, `active` stops
    # meaning "connected" and starts meaning "the wrapper is looping" — which
    # it also is while it sleeps between failed attempts. Left alone, the one
    # alarm this project exists to raise would go silent for good. The wrapper
    # publishes the link state alongside, and the two are read together.

    def test_active_wrapper_with_link_down_is_down(self):
        self.assertEqual(_classify_tunnel("loaded\nactive\n", "down\n"), "down")

    def test_active_wrapper_with_link_up_is_up(self):
        self.assertEqual(_classify_tunnel("loaded\nactive\n", "up\n"), "up")

    def test_absent_link_file_falls_back_to_the_unit(self):
        # A machine still running the plain-ssh unit has no state file, and
        # there `active` does mean connected. Absence must not read as a fault.
        self.assertEqual(_classify_tunnel("loaded\nactive\n", None), "up")

    def test_link_state_cannot_revive_a_dead_unit(self):
        # A wrapper killed outright leaves its last "up" behind. systemd
        # noticing the unit is gone has to win, or the page reports a tunnel
        # that no process is holding open.
        self.assertEqual(_classify_tunnel("loaded\nfailed\n", "up\n"), "down")

    def test_unrecognised_link_state_falls_back_to_the_unit(self):
        # A half-written or garbled file is not evidence of a fault.
        self.assertEqual(_classify_tunnel("loaded\nactive\n", "wat"), "up")


class TestLinkStateFile(unittest.TestCase):
    """Reading the file the wrapper publishes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_path_lives_under_the_runtime_dir(self):
        self.assertEqual(
            tunnel_link_path(),
            os.path.join(self.tmp, "statusgumbo", "tunnel.state"),
        )

    def test_reads_what_the_wrapper_wrote(self):
        os.makedirs(os.path.join(self.tmp, "statusgumbo"))
        with open(tunnel_link_path(), "w") as handle:
            handle.write("down\n")
        self.assertEqual(read_link_state(), "down\n")

    def test_absent_file_reads_as_none(self):
        self.assertIsNone(read_link_state())

    def test_unreadable_file_reads_as_none(self):
        # Never let a permissions problem on a hint file take the collector
        # down; the unit's own state is still a usable answer.
        os.makedirs(os.path.join(self.tmp, "statusgumbo"))
        os.mkdir(tunnel_link_path())  # a directory where a file was expected
        self.assertIsNone(read_link_state())


class ServerTestCase(unittest.TestCase):
    STORE_FACTORY = SessionStore
    SERVER_KWARGS = {}

    def setUp(self):
        self.store = self.STORE_FACTORY()
        self.now = [1000.0]
        self.server = make_server(
            ("127.0.0.1", 0),
            self.store,
            clock=lambda: self.now[0],
            **self.SERVER_KWARGS
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def post(self, path, body):
        request = urllib.request.Request(
            self.url(path),
            data=body if isinstance(body, bytes) else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=5)


class TestIngest(ServerTestCase):
    def test_valid_envelope_returns_204_and_is_stored(self):
        response = self.post(
            "/ingest", {"host": "workstation", "branch": "master", "payload": PAYLOAD}
        )
        self.assertEqual(response.status, 204)
        self.assertEqual(len(self.store.snapshot(1000.0)["sessions"]), 1)

    def test_malformed_json_returns_400_and_leaves_state_alone(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/ingest", b"{not json")
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(self.store.snapshot(1000.0)["sessions"], [])

    def test_envelope_missing_session_id_returns_400(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/ingest", {"host": "h", "payload": {"no": "session id"}})
        self.assertEqual(caught.exception.code, 400)

    def test_unknown_post_path_returns_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/nope", {"host": "h", "payload": PAYLOAD})
        self.assertEqual(caught.exception.code, 404)


class TestErrorPathsCloseConnection(ServerTestCase):
    """An error path can reject a request without reading its body. Under
    HTTP/1.1 keep-alive those unread bytes would be parsed as the next
    request, so the server must close the connection rather than reuse it.
    `urllib` opens a fresh connection per request and so cannot see this;
    `http.client` keeps one open, which is why these tests use it.
    """

    def test_oversized_body_is_rejected_and_connection_closed(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(
                "POST",
                "/ingest",
                body=b"x" * (MAX_BODY_BYTES + 1),
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            self.assertTrue(response.will_close)
            response.read()
        finally:
            conn.close()

    def test_unknown_post_path_closes_connection(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(
                "POST",
                "/nope",
                body=b'{"a": 1}',
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            self.assertEqual(response.status, 404)
            self.assertTrue(response.will_close)
            response.read()
        finally:
            conn.close()


class TestIdleConnectionsAreReleased(ServerTestCase):
    """Every connection holds a handler thread for as long as it stays open.
    A phone that sleeps or drops off Tailscale abandons its keep-alive socket
    without a FIN, so without a read timeout that thread waits forever. The
    live collector was found holding 81 such connections, up to 159h idle.
    """

    SERVER_KWARGS = {"idle_timeout": 0.2}

    def assertClosedByServer(self, sock):
        sock.settimeout(5)
        started = time.monotonic()
        self.assertEqual(sock.recv(1), b"")
        self.assertLess(time.monotonic() - started, 4)

    def test_a_connection_that_never_sends_a_request_is_closed(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            self.assertClosedByServer(sock)

    def test_a_keep_alive_connection_left_idle_is_closed(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/api/sessions")
            response = conn.getresponse()
            self.assertFalse(response.will_close)
            response.read()
            self.assertClosedByServer(conn.sock)
        finally:
            conn.close()

    def test_a_connection_in_use_is_kept_alive(self):
        # The timeout is per wait, not per connection: a page that keeps
        # polling must keep reusing its socket.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/api/sessions")
            response = conn.getresponse()
            response.read()
            first_sock = conn.sock
            for _ in range(3):
                time.sleep(0.1)
                conn.request("GET", "/api/sessions")
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
            self.assertIs(conn.sock, first_sock)
        finally:
            conn.close()


class TestDefaultIdleTimeout(unittest.TestCase):
    def test_outlasts_the_page_refresh_interval(self):
        # Shorter than the page's poll and every tick would pay for a new
        # connection; the page would still work, which is why only a test
        # will notice.
        with open(INDEX_PATH, encoding="utf-8") as handle:
            refresh_ms = int(
                re.search(r"const REFRESH_MS = (\d+);", handle.read()).group(1)
            )
        self.assertGreater(IDLE_TIMEOUT_SECS, 2 * refresh_ms / 1000)


class TestApi(ServerTestCase):
    def test_sessions_endpoint_returns_the_snapshot(self):
        self.post(
            "/ingest", {"host": "workstation", "branch": "master", "payload": PAYLOAD}
        )
        with urllib.request.urlopen(self.url("/api/sessions"), timeout=5) as response:
            self.assertEqual(response.status, 200)
            body = json.loads(response.read())
        self.assertEqual(body["sessions"][0]["project"], "demo-app")
        self.assertIn("hosts", body)
        self.assertIn("now", body)

    def test_sessions_endpoint_reports_tunnel_state(self):
        with urllib.request.urlopen(self.url("/api/sessions"), timeout=5) as response:
            body = json.loads(response.read())
        self.assertIn("tunnel", body)
        self.assertIn(body["tunnel"]["state"], ("up", "down", "unknown"))

    def test_root_serves_html(self):
        with urllib.request.urlopen(self.url("/"), timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers.get("Content-Type", ""))
            self.assertIn(b"StatusGumbo", response.read())

    def test_unknown_get_path_returns_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.url("/nope"), timeout=5)
        self.assertEqual(caught.exception.code, 404)


class TestMachinesWithoutSystemdOrTailscale(unittest.TestCase):
    """The tunnel and the tailnet are optional extras. A container has neither
    systemctl nor tailscale, and a machine with systemd but no user session
    has systemctl with no bus to talk to. None of these may be an error: the
    tunnel is "unknown" (which the page renders as nothing) and there is
    simply no tailnet address.
    """

    def run_raises(self, error):
        return mock.patch("collector.server.subprocess.run", side_effect=error)

    def test_missing_systemctl_means_no_tunnel(self):
        with self.run_raises(FileNotFoundError("systemctl")):
            self.assertEqual(tunnel_state()["state"], "unknown")

    def test_systemctl_without_a_user_bus_means_no_tunnel(self):
        failed = mock.Mock(returncode=1, stdout="",
                           stderr="Failed to connect to bus: No medium found\n")
        with mock.patch("collector.server.subprocess.run", return_value=failed):
            self.assertEqual(tunnel_state()["state"], "unknown")

    def test_missing_tailscale_means_no_tailnet_address(self):
        with self.run_raises(FileNotFoundError("tailscale")):
            self.assertIsNone(tailscale_ipv4())

    def test_neither_probe_lets_the_tool_write_to_our_stderr(self):
        # A container's logs are the collector's stderr. A probe that let
        # "Failed to connect to bus" through would print it on every poll.
        with mock.patch("collector.server.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="")
            tunnel_state()
            tailscale_ipv4()
        for call in run.call_args_list:
            self.assertTrue(call.kwargs.get("capture_output"))


class TestAStaleCopyOfThePageIsRecognisable(ServerTestCase):
    """When the phone drops off Tailscale, Chrome shows its saved offline copy
    of the page. That copy runs no JavaScript, so tick() never gets to say
    "collector unreachable", and yesterday's cards read as live. The only
    thing that can date such a copy is text the server wrote into the HTML.
    """

    # 2026-09-05 14:02:00 UTC. A single-digit day, so the test also pins the
    # day as unpadded — the page's own tick() writes getDate(), and the two
    # must not flicker between "05" and "5" on the first poll.
    SERVED_AT = 1788616920.0

    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.addCleanup(self._restore_tz)
        super().setUp()
        self.now[0] = self.SERVED_AT

    def _restore_tz(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def fetch_root(self):
        with urllib.request.urlopen(self.url("/"), timeout=5) as response:
            return response, response.read().decode("utf-8")

    def stamp(self, body):
        """The text of the stamp element, or None — never the whole page, so a
        failure says what the stamp read rather than dumping 20KB of HTML."""
        match = re.search(r'<[^>]*id="stamp"[^>]*>([^<]*)<', body)
        return match.group(1) if match else None

    def test_the_html_itself_says_when_it_was_served(self):
        _, body = self.fetch_root()
        self.assertEqual(self.stamp(body), "updated 5 Sep 14:02")

    def test_the_stamp_is_readable_without_javascript(self):
        # In the markup, ahead of the page's only <script>, so it is text the
        # parser renders rather than something a script has to produce.
        _, body = self.fetch_root()
        stamp_at = body.find('id="stamp"')
        self.assertNotEqual(stamp_at, -1, "no stamp element")
        self.assertLess(stamp_at, body.index("<script>"))

    def test_the_date_is_part_of_the_stamp(self):
        # A bare HH:MM is exactly what fails here: a copy saved yesterday at
        # 14:02, read today at 14:05, says "14:02" and looks three minutes old.
        _, body = self.fetch_root()
        self.assertIn("5 Sep", self.stamp(body) or "")

    def test_no_placeholder_reaches_the_phone(self):
        _, body = self.fetch_root()
        self.assertFalse(STAMP_PLACEHOLDER in body, "placeholder left in the page")

    def test_each_request_is_stamped_with_its_own_time(self):
        self.fetch_root()
        self.now[0] = self.SERVED_AT + 3600
        _, body = self.fetch_root()
        self.assertEqual(self.stamp(body), "updated 5 Sep 15:02")

    def test_the_page_asks_not_to_be_stored(self):
        response, _ = self.fetch_root()
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))


class TestHostilePayloadOverHttp(ServerTestCase):
    """FINDING 1, at the wire. A non-string current_dir raised TypeError,
    which do_POST did not catch, so the client got a dropped connection with
    no status line at all and the store was left wedged for good.
    """

    def test_non_string_current_dir_gets_a_response_not_a_dropped_socket(self):
        response = self.post(
            "/ingest",
            {
                "host": "coder-vm",
                "branch": "master",
                "payload": {
                    "session_id": "wedge",
                    "workspace": {"current_dir": 12345},
                },
            },
        )
        self.assertEqual(response.status, 204)

    def test_the_service_survives_it(self):
        try:
            self.post(
                "/ingest",
                {
                    "host": "coder-vm",
                    "payload": {
                        "session_id": "wedge",
                        "workspace": {"current_dir": 12345},
                    },
                },
            )
        except Exception:  # noqa: BLE001 - RED state drops the connection
            pass
        self.post(
            "/ingest", {"host": "workstation", "branch": "m", "payload": PAYLOAD}
        )
        with urllib.request.urlopen(self.url("/api/sessions"), timeout=5) as response:
            self.assertEqual(response.status, 200)
            body = json.loads(response.read())
        self.assertIn("demo-app", [s["project"] for s in body["sessions"]])


class ExplodingIngestStore(SessionStore):
    def ingest(self, envelope, now):
        raise TypeError("upstream schema surprise")


class ExplodingSnapshotStore(SessionStore):
    def snapshot(self, now):
        raise TypeError("upstream schema surprise")


class TestIngestErrorBoundary(ServerTestCase):
    """FINDING 1c. do_POST caught only (ValueError, UnicodeDecodeError). The
    store is fed by an upstream schema nobody here controls, and enumerating
    the exceptions we expect from it is exactly how one bad tick took the
    whole page down.
    """

    STORE_FACTORY = ExplodingIngestStore

    def test_an_unexpected_store_error_is_a_400_with_a_logged_traceback(self):
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.post(
                    "/ingest",
                    {"host": "h", "branch": "b", "payload": PAYLOAD},
                )
        self.assertEqual(caught.exception.code, 400)
        # log_message is silenced, so stderr is the only route to the journal.
        self.assertIn("Traceback", captured.getvalue())


class TestSessionsErrorBoundary(ServerTestCase):
    """FINDING 6a. do_GET called store.snapshot() with no try, so any
    store-side error became a dropped connection — which the page then
    reported as "collector unreachable", blaming the network for its own bug.
    """

    STORE_FACTORY = ExplodingSnapshotStore

    def test_a_store_error_is_a_500_not_a_dropped_connection(self):
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(self.url("/api/sessions"), timeout=5)
        self.assertEqual(caught.exception.code, 500)
        self.assertIn("Traceback", captured.getvalue())


TOKEN = "correct-horse-battery-staple"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class TestTokenAuth(ServerTestCase):
    """With a token configured, every route needs it. This is what makes it
    safe to bind anything other than loopback and the tailnet: those
    addresses were the only protection, and a stranger's network is not a
    tailnet."""

    SERVER_KWARGS = {"token": TOKEN}
    ENVELOPE = {"host": "laptop", "branch": "main", "payload": PAYLOAD}

    def request(self, path, method="GET", body=None, headers=None):
        request = urllib.request.Request(
            self.url(path),
            data=json.dumps(body).encode() if body is not None else None,
            headers=dict({"Content-Type": "application/json"}, **(headers or {})),
            method=method,
        )
        opener = urllib.request.build_opener(NoRedirect)
        try:
            response = opener.open(request, timeout=5)
            return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def bearer(self, token=TOKEN):
        return {"Authorization": "Bearer " + token}

    def test_ingest_without_the_token_is_refused_and_not_stored(self):
        status, headers, _ = self.request("/ingest", "POST", self.ENVELOPE)
        self.assertEqual(status, 401)
        self.assertIn("Bearer", headers.get("WWW-Authenticate", ""))
        self.assertEqual(self.store.snapshot(1000.0)["sessions"], [])

    def test_ingest_with_a_wrong_token_is_refused(self):
        status, _, _ = self.request(
            "/ingest", "POST", self.ENVELOPE, self.bearer("wrong-token-wrong-token")
        )
        self.assertEqual(status, 401)
        self.assertEqual(self.store.snapshot(1000.0)["sessions"], [])

    def test_ingest_with_the_token_is_stored(self):
        status, _, _ = self.request("/ingest", "POST", self.ENVELOPE, self.bearer())
        self.assertEqual(status, 204)
        self.assertEqual(len(self.store.snapshot(1000.0)["sessions"]), 1)

    def test_the_api_needs_the_token(self):
        self.assertEqual(self.request("/api/sessions")[0], 401)
        self.assertEqual(self.request("/api/sessions", headers=self.bearer())[0], 200)

    def test_the_page_needs_the_token(self):
        status, _, body = self.request("/")
        self.assertEqual(status, 401)
        self.assertNotIn(b"<script", body)

    def test_a_token_link_sets_a_cookie_and_drops_the_token_from_the_url(self):
        # The phone opens /?t=<token> once. The token must not stay in the
        # address bar (screenshots, history), so it is swapped for a cookie
        # and the page is reloaded clean.
        status, headers, _ = self.request("/?t=" + TOKEN)
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/")
        cookie = headers.get("Set-Cookie", "")
        # The cookie holds a value derived from the token, never the token.
        self.assertIn("statusgumbo_token=" + cookie_value(TOKEN), cookie)
        self.assertNotIn(TOKEN, cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Max-Age=", cookie)

    def test_a_wrong_token_link_sets_no_cookie(self):
        status, headers, _ = self.request("/?t=wrong-token-wrong-token")
        self.assertEqual(status, 401)
        self.assertIsNone(headers.get("Set-Cookie"))

    def test_the_cookie_opens_the_page_and_the_api(self):
        cookie = {"Cookie": "other=1; statusgumbo_token=" + cookie_value(TOKEN)}
        status, _, body = self.request("/", headers=cookie)
        self.assertEqual(status, 200)
        self.assertIn(b"StatusGumbo", body)
        self.assertEqual(self.request("/api/sessions", headers=cookie)[0], 200)

    def test_the_raw_token_is_no_longer_a_cookie(self):
        cookie = {"Cookie": "statusgumbo_token=" + TOKEN}
        self.assertEqual(self.request("/api/sessions", headers=cookie)[0], 401)

    def test_the_cookie_value_is_no_bearer(self):
        # A cookie lifted from a browser can open the page, never post.
        status, _, _ = self.request("/ingest", "POST", self.ENVELOPE,
                                    self.bearer(cookie_value(TOKEN)))
        self.assertEqual(status, 401)

    def test_a_guesser_is_throttled(self):
        for _ in range(AUTH_FAILURES_PER_MIN):
            self.request("/api/sessions", headers=self.bearer("wrong-token-wrong-token"))
        self.assertEqual(
            self.request("/api/sessions", headers=self.bearer("wrong-token-wrong-token"))[0], 429
        )
        # Throttled means throttled: even the right token waits out the minute.
        self.assertEqual(self.request("/api/sessions", headers=self.bearer())[0], 429)

    def test_the_page_carries_a_csp_that_allows_its_own_script(self):
        status, headers, body = self.request("/", headers=self.bearer())
        csp = headers.get("Content-Security-Policy", "")
        self.assertIn("frame-ancestors 'none'", csp)
        script = re.search(rb"<script>(.*?)</script>", body, re.S).group(1)
        digest = base64.b64encode(hashlib.sha256(script).digest()).decode()
        self.assertIn("'sha256-%s'" % digest, csp)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

    def test_json_nested_too_deep_is_a_plain_400(self):
        body = b'{"host":"h","payload":' + b"[" * 100000 + b"]" * 100000 + b"}"
        request = urllib.request.Request(
            self.url("/ingest"), data=body, method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN},
        )
        with mock.patch("traceback.print_exc") as printed:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 400)
        printed.assert_not_called()

    def test_a_wrong_cookie_is_refused(self):
        cookie = {"Cookie": "statusgumbo_token=wrong-token-wrong-token"}
        self.assertEqual(self.request("/api/sessions", headers=cookie)[0], 401)

    def test_a_cookie_does_not_authorise_ingest(self):
        # Reporters send the header. Accepting the cookie on a POST would let
        # any page the phone visits post fake sessions (SameSite aside).
        cookie = {"Cookie": "statusgumbo_token=" + TOKEN}
        self.assertEqual(self.request("/ingest", "POST", self.ENVELOPE, cookie)[0], 401)


class TestNoTokenKeepsTodaysBehaviour(ServerTestCase):
    def test_everything_is_open_without_a_token(self):
        self.post("/ingest", {"host": "laptop", "branch": "main", "payload": PAYLOAD})
        with urllib.request.urlopen(self.url("/api/sessions"), timeout=5) as response:
            self.assertEqual(response.status, 200)

    def test_a_query_string_does_not_hide_the_page(self):
        with urllib.request.urlopen(self.url("/?t=anything"), timeout=5) as response:
            self.assertEqual(response.status, 200)


class TestHealthz(ServerTestCase):
    """A container's HEALTHCHECK has no token and should not need one. The
    probe answers only "the process serves HTTP" and carries nothing a
    stranger could use, so it stays open when every other route is locked."""

    SERVER_KWARGS = {"token": TOKEN}

    def test_healthz_answers_without_the_token(self):
        with urllib.request.urlopen(self.url("/healthz"), timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"ok")

    def test_healthz_says_nothing_about_sessions(self):
        self.store.ingest({"host": "laptop", "branch": "main", "payload": PAYLOAD}, 1000.0)
        with urllib.request.urlopen(self.url("/healthz"), timeout=5) as response:
            self.assertNotIn(b"laptop", response.read())


class TestMainAsAContainerRunsIt(unittest.TestCase):
    """The real entry point, the way the Docker image starts it."""

    def start(self, *extra):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        # An empty HOME: with --cloud the poller must find no claude.ai login,
        # never the real one on the machine running the tests.
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home)
        env = dict(os.environ, STATUSGUMBO_HOST="some-container-id", HOME=home)
        env.pop("STATUSGUMBO_TOKEN", None)
        env.pop("STATUSGUMBO_TOKEN_FILE", None)
        process = subprocess.Popen(
            [sys.executable, "-m", "collector.server", "--port", str(port),
             "--bind", "127.0.0.1", *extra],
            cwd=REPO_ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        url = "http://127.0.0.1:%d" % port
        deadline = time.time() + 10
        while True:
            try:
                urllib.request.urlopen(url + "/healthz", timeout=1).read()
                return process, url
            except OSError:
                if time.time() > deadline:
                    self.fail("collector did not start")
                time.sleep(0.05)

    def sessions(self, url):
        with urllib.request.urlopen(url + "/api/sessions", timeout=5) as response:
            return json.loads(response.read())

    def hosts(self, url):
        return [h["host"] for h in self.sessions(url)["hosts"]]

    def test_cloud_sessions_are_off_by_default(self):
        # They use an undocumented endpoint with the user's own claude.ai
        # login. That is a choice to opt into, not a default.
        _, url = self.start()
        self.assertIsNone(self.sessions(url)["cloud"])

    def test_cloud_turns_the_poll_on(self):
        _, url = self.start("--cloud")
        self.assertIsNotNone(self.sessions(url)["cloud"])

    def test_no_cloud_is_still_accepted(self):
        # Existing units and the Dockerfile pass it.
        _, url = self.start("--no-cloud")
        self.assertIsNone(self.sessions(url)["cloud"])

    def test_by_default_its_own_machine_is_listed(self):
        _, url = self.start()
        self.assertEqual(self.hosts(url), ["some-container-id"])

    def test_no_local_host_lists_no_machine_of_its_own(self):
        # A container's hostname is a random id, and nothing inside it runs
        # Claude Code: listed, it would read "not reporting" forever.
        _, url = self.start("--no-local-host")
        self.assertEqual(self.hosts(url), [])

    def test_sigterm_stops_it_promptly_and_cleanly(self):
        # As PID 1 in a container, Python ignores SIGTERM unless it installs
        # a handler, and `docker stop` then waits out its timeout and kills.
        process, _ = self.start()
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=5), 0)


class TestLoadToken(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def write(self, text):
        path = os.path.join(self.dir, "token")
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def test_no_token_anywhere_is_none(self):
        self.assertIsNone(load_token({}, None))

    def test_from_the_environment(self):
        self.assertEqual(load_token({"STATUSGUMBO_TOKEN": TOKEN}, None), TOKEN)

    def test_from_a_file_with_a_trailing_newline(self):
        self.assertEqual(load_token({}, self.write(TOKEN + "\n")), TOKEN)

    def test_the_file_env_var_names_a_file(self):
        path = self.write(TOKEN)
        self.assertEqual(load_token({"STATUSGUMBO_TOKEN_FILE": path}, None), TOKEN)

    def test_an_explicit_file_wins_over_the_environment(self):
        path = self.write(TOKEN)
        self.assertEqual(load_token({"STATUSGUMBO_TOKEN": "x" * 20}, path), TOKEN)

    def test_a_short_token_is_refused(self):
        with self.assertRaises(ValueError):
            load_token({"STATUSGUMBO_TOKEN": "short"}, None)

    def test_a_token_that_cannot_be_a_cookie_is_refused(self):
        with self.assertRaises(ValueError):
            load_token({"STATUSGUMBO_TOKEN": "has space; and=semicolon-xx"}, None)

    def test_a_missing_file_is_an_error_not_an_open_collector(self):
        with self.assertRaises(ValueError):
            load_token({}, os.path.join(self.dir, "absent"))


class TestPlanBinds(unittest.TestCase):
    """Which addresses the collector listens on. Loopback and the tailnet
    need no token -- that pair was the whole security model and still is by
    default. Anything else is reachable by people who are not you, so it
    needs one, and the collector refuses to start rather than guess."""

    def test_default_is_loopback_plus_the_tailnet(self):
        self.assertEqual(plan_binds([], "100.101.102.103", None),
                         ["127.0.0.1", "100.101.102.103"])

    def test_default_without_tailscale_is_loopback_only(self):
        self.assertEqual(plan_binds([], None, None), ["127.0.0.1"])

    def test_requested_addresses_replace_the_default(self):
        self.assertEqual(plan_binds(["127.0.0.1"], "100.101.102.103", None),
                         ["127.0.0.1"])

    def test_tailnet_and_loopback_need_no_token(self):
        for address in ("127.0.0.1", "::1", "100.64.0.1", "100.127.255.254",
                        "fd7a:115c:a1e0::1"):
            with self.subTest(address=address):
                self.assertEqual(plan_binds([address], None, None), [address])

    def test_anything_else_needs_a_token(self):
        for address in ("0.0.0.0", "::", "192.168.1.20", "10.0.0.5",
                        "100.128.0.1", "203.0.113.9"):
            with self.subTest(address=address):
                with self.assertRaises(ValueError):
                    plan_binds([address], None, None)
                self.assertEqual(plan_binds([address], None, "t" * 16), [address])

    def test_duplicates_are_bound_once(self):
        self.assertEqual(plan_binds(["127.0.0.1", "127.0.0.1"], None, None),
                         ["127.0.0.1"])

    def test_a_hostname_is_refused(self):
        # Resolving it would make the security decision depend on DNS.
        with self.assertRaises(ValueError):
            plan_binds(["localhost"], None, "t" * 16)


class TestIpv6Bind(unittest.TestCase):
    def test_make_server_listens_on_ipv6_loopback(self):
        if not socket.has_ipv6:
            self.skipTest("no IPv6")
        try:
            server = make_server(("::1", 0), SessionStore())
        except OSError:
            self.skipTest("::1 not configured")
        self.addCleanup(server.server_close)
        self.assertEqual(server.socket.family, socket.AF_INET6)


if __name__ == "__main__":
    unittest.main()


class RawRequests(ServerTestCase):
    """http.client, so every header -- Host included -- is the test's choice."""

    def raw(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            headers = dict(headers or {})
            headers.setdefault("Host", "127.0.0.1:%d" % self.port)
            if body is not None:
                headers.setdefault("Content-Length", str(len(body)))
            for name, value in headers.items():
                conn.putheader(name, value)
            conn.endheaders(body)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def ingest(self, headers=None, body=None):
        if body is None:
            body = json.dumps({"host": "workstation", "payload": PAYLOAD}).encode()
        return self.raw("POST", "/ingest", body, dict(
            {"Content-Type": "application/json"}, **(headers or {})
        ))[0]


class TestBrowsersCannotPost(RawRequests):
    """A collector with no token took a tick from any page open in a browser
    on the same machine: a text/plain POST is a "simple" request, sent
    cross-site without a preflight. Reporters are curl, which sends neither
    Origin nor Sec-Fetch-Site and always sends application/json."""

    def assert_nothing_stored(self):
        self.assertEqual(self.store.snapshot(1000.0)["sessions"], [])

    def test_a_reporter_still_posts(self):
        self.assertEqual(self.ingest(), 204)

    def test_a_charset_parameter_is_fine(self):
        self.assertEqual(
            self.ingest({"Content-Type": "application/json; charset=utf-8"}), 204
        )

    def test_a_post_with_an_origin_is_refused(self):
        for origin in ("https://evil.example", "http://127.0.0.1:%d" % self.port, "null"):
            with self.subTest(origin=origin):
                self.assertEqual(self.ingest({"Origin": origin}), 403)
        self.assert_nothing_stored()

    def test_a_cross_site_fetch_is_refused(self):
        for site in ("cross-site", "same-site", "same-origin"):
            with self.subTest(site=site):
                self.assertEqual(self.ingest({"Sec-Fetch-Site": site}), 403)
        self.assert_nothing_stored()

    def test_a_body_that_is_not_declared_json_is_refused(self):
        for content_type in ("text/plain", "application/x-www-form-urlencoded",
                             "multipart/form-data; boundary=x", None):
            with self.subTest(content_type=content_type):
                headers = {} if content_type is None else {"Content-Type": content_type}
                status, _ = self.raw(
                    "POST", "/ingest",
                    json.dumps({"host": "workstation", "payload": PAYLOAD}).encode(),
                    headers,
                )
                self.assertEqual(status, 415)
        self.assert_nothing_stored()

    def test_bare_nan_and_infinity_are_refused(self):
        for word in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(word=word):
                body = json.dumps({"host": "workstation", "payload": PAYLOAD})
                body = body.replace('"payload": {', '"payload": {"cost": {"total_cost_usd": %s}, ' % word, 1)
                self.assertEqual(self.ingest(body=body.encode()), 400)
        self.assert_nothing_stored()


class TestUnknownHostIsRefused(RawRequests):
    """DNS rebinding: a page on an attacker's domain re-resolves it to
    127.0.0.1, and the browser then lets it read /api/sessions as its own
    origin. The Host header still names the attacker's domain."""

    SERVER_KWARGS = {"allowed_hosts": frozenset({"localhost", "workstation",
                                                 "workstation.tail1.ts.net"})}

    def test_a_foreign_name_cannot_read_or_write(self):
        for host in ("evil.example", "evil.example:4747", "workstation.evil.example"):
            with self.subTest(host=host):
                self.assertEqual(self.raw("GET", "/api/sessions", headers={"Host": host})[0], 421)
                self.assertEqual(self.raw("GET", "/", headers={"Host": host})[0], 421)
                self.assertEqual(self.ingest({"Host": host}), 421)
        self.assertEqual(self.store.snapshot(1000.0)["sessions"], [])

    def test_the_names_this_machine_goes_by_are_served(self):
        for host in ("localhost", "LOCALHOST:4747", "workstation:4747",
                     "workstation.tail1.ts.net.", "127.0.0.1:4747",
                     "[::1]:4747", "100.101.102.103", "[fd7a:115c:a1e0::1]"):
            with self.subTest(host=host):
                self.assertEqual(self.raw("GET", "/api/sessions", headers={"Host": host})[0], 200)

    def test_healthz_answers_any_name(self):
        self.assertEqual(self.raw("GET", "/healthz", headers={"Host": "evil.example"})[0], 200)


class TestTokenModeDoesNotCheckHost(RawRequests):
    """With a token the bearer and the host-scoped cookie are the defence, and
    a container is reached by whatever name its owner gives it."""

    SERVER_KWARGS = {"token": TOKEN}

    def test_any_name_with_the_bearer(self):
        status, _ = self.raw("GET", "/api/sessions", headers={
            "Host": "statusgumbo.example", "Authorization": "Bearer " + TOKEN,
        })
        self.assertEqual(status, 200)


class TestAllowedHostNames(unittest.TestCase):
    def test_hostname_its_first_label_tailnet_and_extras(self):
        with mock.patch("socket.gethostname", return_value="Box.Example.com"):
            names = allowed_host_names(["Phone-Alias."], tailnet={"box", "box.tail1.ts.net."})
        self.assertEqual(
            names,
            {"localhost", "box.example.com", "box", "box.tail1.ts.net", "phone-alias"},
        )

    def test_host_header_parsing(self):
        allowed = frozenset({"box"})
        self.assertTrue(host_allowed(None, allowed))
        self.assertTrue(host_allowed("box:4747", allowed))
        self.assertTrue(host_allowed("[::1]", allowed))
        self.assertTrue(host_allowed("::1", allowed))
        self.assertFalse(host_allowed("", allowed))
        self.assertFalse(host_allowed("box.evil.example", allowed))
        self.assertFalse(host_allowed("[evil]:4747", allowed))


def _self_signed(directory):
    """A throwaway localhost certificate, or None without openssl."""
    cert = os.path.join(directory, "cert.pem")
    key = os.path.join(directory, "key.pem")
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", key, "-out", cert, "-days", "1", "-subj", "/CN=localhost"],
            check=True, capture_output=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return cert, key


class TestTls(unittest.TestCase):
    """Beyond loopback and the tailnet, plain HTTP hands the token to anyone
    on the path, on every tick and every page poll."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.pair = _self_signed(cls.dir)
        if cls.pair is None:
            raise unittest.SkipTest("openssl is not available")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        import ssl
        self.server = make_server(
            ("127.0.0.1", 0), SessionStore(), token=TOKEN,
            tls=tls_context(*self.pair),
        )
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = ssl.create_default_context(cafile=self.pair[0])
        self.client.check_hostname = False

    def https(self, path):
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, timeout=5,
                                           context=self.client)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            return response.status, response.getheader("Set-Cookie"), response.read()
        finally:
            conn.close()

    def test_it_serves_https(self):
        self.assertEqual(self.https("/healthz")[0], 200)

    def test_the_cookie_is_secure_under_tls(self):
        status, cookie, _ = self.https("/?t=" + TOKEN)
        self.assertEqual(status, 303)
        self.assertIn("; Secure", cookie)

    def test_a_plain_http_client_does_not_stop_it(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as raw:
            raw.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
            try:
                raw.recv(100)
            except OSError:
                pass
        self.assertEqual(self.https("/healthz")[0], 200)

    def test_a_plain_http_cookie_is_not_secure(self):
        server = make_server(("127.0.0.1", 0), SessionStore(), token=TOKEN)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("GET", "/?t=" + TOKEN)
        self.assertNotIn("Secure", conn.getresponse().getheader("Set-Cookie"))
        conn.close()


class TestPlainHttpWarning(unittest.TestCase):
    def run_main(self, *args):
        env = dict(os.environ, STATUSGUMBO_TOKEN=TOKEN, HOME=tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, env["HOME"])
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, "-m", "collector.server", "--port", str(port),
             "--no-local-host", *args],
            cwd=REPO_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        time.sleep(1.5)
        process.terminate()
        return process.communicate(timeout=10)[1].decode()

    def test_a_public_bind_without_tls_warns(self):
        self.assertIn("unencrypted", self.run_main("--bind", "0.0.0.0"))

    def test_behind_a_tls_proxy_it_does_not(self):
        self.assertNotIn("unencrypted", self.run_main("--bind", "0.0.0.0", "--behind-tls-proxy"))

    def test_loopback_does_not_warn(self):
        self.assertNotIn("unencrypted", self.run_main("--bind", "127.0.0.1"))

    def test_cert_and_key_go_together(self):
        self.assertIn("go together", self.run_main("--bind", "127.0.0.1", "--tls-cert", "/x"))
