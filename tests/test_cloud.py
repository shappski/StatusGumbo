import json
import os
import tempfile
import unittest
import unittest.mock
import urllib.parse

from collector.cloud import (
    LOOKBACK_SECS,
    MAX_AGE_SECS,
    MAX_BACKOFF_SECS,
    POLL_SECS,
    ApiError,
    CloudPoller,
    LoginUnavailable,
    fetch_sessions,
    parse_session,
    read_login,
)

NOW = 1790000000.0  # 2026-09-21


def iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def raw_session(**overrides):
    """One entry shaped like the live response of 2026-09-25."""
    raw = {
        "id": "cse_01abc",
        "title": "Cloud session status",
        "environment_kind": "anthropic_cloud",
        "status": "active",
        "status_bucket": "blocked",
        "worker_status": "idle",
        "created_at": iso(NOW - 3600),
        "last_event_at": iso(NOW - 120),
        "updated_at": iso(NOW - 60),
        "config": {
            "model": "claude-opus-5-5",
            "sources": [{"type": "git_repository", "url": "https://github.com/example/demo-app"}],
        },
        "external_metadata": {
            "context_usage": {"max_tokens": 1000000, "used_tokens": 131373},
            "current_branches": {"": "master"},
            "last_served_model": "claude-opus-5-5",
            "post_turn_summary": {
                "needs_action": "commit V-1 yourself",
                "status_category": "need_input",
                "status_detail": "V-1 changes staged",
            },
            "usage": {"cost_usd": 26.36},
        },
    }
    raw.update(overrides)
    return raw


class TestParseSession(unittest.TestCase):
    def test_a_cloud_session_becomes_a_card(self):
        card = parse_session(raw_session())
        self.assertEqual(card["title"], "Cloud session status")
        self.assertEqual(card["bucket"], "blocked")
        self.assertEqual(card["needs_action"], "commit V-1 yourself")
        self.assertEqual(card["repo"], "demo-app")
        self.assertEqual(card["branch"], "master")
        self.assertEqual(card["model"], "claude-opus-5-5")
        self.assertAlmostEqual(card["ctx_pct"], 13.1373)
        self.assertEqual(card["last_event_at"], NOW - 120)
        self.assertEqual(card["url"], "https://claude.ai/code/cse_01abc")

    def test_remote_control_sessions_are_left_out(self):
        # Local sessions already have cards from their own ticks.
        self.assertIsNone(parse_session(raw_session(environment_kind="bridge")))

    def test_archived_sessions_are_left_out(self):
        self.assertIsNone(parse_session(raw_session(status="archived")))

    def test_cost_never_leaves_the_parser(self):
        self.assertNotIn("26.36", json.dumps(parse_session(raw_session())))

    def test_garbage_leaves_are_unknown_not_figures(self):
        card = parse_session(raw_session(
            title=7,
            last_event_at="yesterday",
            external_metadata={"context_usage": {"max_tokens": 0, "used_tokens": "5"},
                               "current_branches": "master"},
            config=None,
        ))
        self.assertIsNone(card["title"])
        self.assertIsNone(card["ctx_pct"])
        self.assertIsNone(card["ctx_used_tokens"])
        self.assertIsNone(card["branch"])
        self.assertIsNone(card["repo"])
        self.assertEqual(card["last_event_at"], NOW - 60)  # falls back to updated_at

    def test_bool_is_not_a_token_count(self):
        card = parse_session(raw_session(external_metadata={
            "context_usage": {"max_tokens": 100, "used_tokens": True}}))
        self.assertIsNone(card["ctx_pct"])

    def test_non_dict_and_idless_entries_are_skipped(self):
        self.assertIsNone(parse_session("x"))
        self.assertIsNone(parse_session(raw_session(id="")))


class FakeApi:
    def __init__(self, pages, status=200):
        self.pages = list(pages)
        self.status = status
        self.urls = []

    def __call__(self, url, headers):
        self.headers = headers
        if self.status != 200:
            return self.status, b"{}"
        if url.endswith("/triggers"):
            return 200, b'{"data": [], "has_more": false}'
        self.urls.append(url)
        return 200, json.dumps(self.pages.pop(0)).encode()


class TestFetchSessions(unittest.TestCase):
    def test_sends_the_login_and_filters_the_list(self):
        api = FakeApi([{"data": [raw_session(), raw_session(id="b", environment_kind="bridge")],
                        "next_cursor": None}])
        sessions, state, _ = fetch_sessions("tok", "org", NOW, api)
        self.assertEqual(state, "ok")
        self.assertEqual([s["id"] for s in sessions], ["cse_01abc"])
        self.assertEqual(api.headers["Authorization"], "Bearer tok")
        self.assertEqual(api.headers["x-organization-uuid"], "org")

    def test_pages_until_the_lookback_is_passed(self):
        recent = raw_session(id="a", last_event_at=iso(NOW - 60))
        old = raw_session(id="z", created_at=iso(NOW - LOOKBACK_SECS - 7200),
                          last_event_at=iso(NOW - LOOKBACK_SECS - 3600))
        api = FakeApi([
            {"data": [recent], "next_cursor": "c1"},
            {"data": [old], "next_cursor": "c2"},
            {"data": [], "next_cursor": None},
        ])
        sessions, _, _ = fetch_sessions("tok", "org", NOW, api)
        self.assertEqual(len(api.urls), 2)
        self.assertIn("cursor=c1", api.urls[1])
        # Newest first by creation, so the old one comes last.
        self.assertEqual([s["id"] for s in sessions], ["a", "z"])

    def test_a_session_without_a_creation_time_goes_last(self):
        undated = raw_session(id="undated", created_at=None)
        older = raw_session(id="older", created_at=iso(NOW - 7200))
        newer = raw_session(id="newer", created_at=iso(NOW - 60))
        api = FakeApi([{"data": [undated, older, newer], "next_cursor": None}])
        sessions, _, _ = fetch_sessions("tok", "org", NOW, api)
        self.assertEqual([s["id"] for s in sessions], ["newer", "older", "undated"])

    def test_401_is_a_login_problem(self):
        self.assertEqual(fetch_sessions("t", "o", NOW, FakeApi([], status=401)),
                         (None, "login_expired", None))

    def test_other_statuses_raise(self):
        with self.assertRaises(ApiError):
            fetch_sessions("t", "o", NOW, FakeApi([], status=429))

    def test_a_response_without_a_list_is_an_error_not_an_empty_list(self):
        with self.assertRaisesRegex(ValueError, "^response has no session list$"):
            fetch_sessions("t", "o", NOW, FakeApi([{"sessions": []}]))


def raw_trigger(trigger_id="trig_1", session_id="cse_routine", fired=NOW - 600):
    """One trigger shaped like the live /v1/code/triggers response of 2026-10-03."""
    return {
        "id": trigger_id,
        "name": "cloud: some-branch",
        "enabled": False,
        "ended_reason": "run_once_fired",
        "last_fired_at": iso(fired),
        "last_run": {"session_id": session_id, "status": "ROUTINE_RUN_STATUS_PENDING"},
    }


class RoutineApi:
    """Answers the session list, the trigger list and per-trigger lists by URL."""

    def __init__(self, listed=(), triggers=(), by_trigger=None, trigger_status=200):
        self.listed = list(listed)
        self.triggers = list(triggers)
        self.by_trigger = by_trigger or {}
        self.trigger_status = trigger_status
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parts.query)
        if parts.path.endswith("/triggers"):
            if self.trigger_status != 200:
                return self.trigger_status, b"{}"
            return 200, json.dumps({"data": self.triggers, "has_more": False}).encode()
        if "trigger_id" in query:
            return 200, json.dumps({"data": self.by_trigger.get(query["trigger_id"][0], [])}).encode()
        return 200, json.dumps({"data": self.listed, "next_cursor": None}).encode()

    def trigger_lookups(self):
        return [url for url, _ in self.calls if "trigger_id=" in url]


class TestRoutineSessions(unittest.TestCase):
    """A session a routine started is missing from the plain list (observed
    2026-10-03: 385 entries paged, the routine's session not among them), so
    it is found through its trigger instead."""

    def test_a_routine_session_is_shown(self):
        api = RoutineApi(
            listed=[raw_session()],
            triggers=[raw_trigger()],
            by_trigger={"trig_1": [raw_session(id="cse_routine", created_at=iso(NOW - 600))]},
        )
        sessions, state, _ = fetch_sessions("tok", "org", NOW, api)
        self.assertEqual(state, "ok")
        self.assertEqual([s["id"] for s in sessions], ["cse_routine", "cse_01abc"])

    def test_the_trigger_list_is_asked_for_with_its_beta_header(self):
        api = RoutineApi(triggers=[])
        fetch_sessions("tok", "org", NOW, api)
        trigger_calls = [h for url, h in api.calls if url.endswith("/triggers")]
        self.assertEqual(len(trigger_calls), 1)
        self.assertEqual(trigger_calls[0]["anthropic-beta"], "ccr-triggers-2026-01-30")
        self.assertEqual(trigger_calls[0]["Authorization"], "Bearer tok")

    def test_an_archived_routine_session_is_not_asked_for_again(self):
        archived = set()
        api = RoutineApi(
            triggers=[raw_trigger()],
            by_trigger={"trig_1": [raw_session(id="cse_routine", status="archived")]},
        )
        sessions, _, _ = fetch_sessions("tok", "org", NOW, api, archived=archived)
        self.assertEqual(sessions, [])
        self.assertEqual(archived, {"cse_routine"})
        fetch_sessions("tok", "org", NOW + 60, api, archived=archived)
        self.assertEqual(len(api.trigger_lookups()), 1)

    def test_a_live_routine_session_is_asked_for_every_poll(self):
        archived = set()
        api = RoutineApi(
            triggers=[raw_trigger()],
            by_trigger={"trig_1": [raw_session(id="cse_routine", status_bucket="review_ready")]},
        )
        fetch_sessions("tok", "org", NOW, api, archived=archived)
        fetch_sessions("tok", "org", NOW + 60, api, archived=archived)
        self.assertEqual(len(api.trigger_lookups()), 2)
        self.assertEqual(archived, set())

    def test_a_trigger_that_last_fired_before_the_lookback_is_skipped(self):
        api = RoutineApi(triggers=[raw_trigger(fired=NOW - LOOKBACK_SECS - 60)])
        fetch_sessions("tok", "org", NOW, api)
        self.assertEqual(api.trigger_lookups(), [])

    def test_a_trigger_that_never_ran_is_skipped(self):
        never = raw_trigger()
        del never["last_run"]
        api = RoutineApi(triggers=[never])
        fetch_sessions("tok", "org", NOW, api)
        self.assertEqual(api.trigger_lookups(), [])

    def test_a_session_already_listed_is_not_looked_up_or_doubled(self):
        api = RoutineApi(listed=[raw_session(id="cse_routine")],
                         triggers=[raw_trigger()],
                         by_trigger={"trig_1": [raw_session(id="cse_routine")]})
        sessions, _, _ = fetch_sessions("tok", "org", NOW, api)
        self.assertEqual([s["id"] for s in sessions], ["cse_routine"])
        self.assertEqual(api.trigger_lookups(), [])

    def test_only_the_triggers_last_run_is_taken(self):
        # A recurring routine has older runs under the same trigger; only
        # last_run names the one that is current.
        api = RoutineApi(triggers=[raw_trigger()],
                         by_trigger={"trig_1": [raw_session(id="cse_older"),
                                                raw_session(id="cse_routine")]})
        sessions, _, _ = fetch_sessions("tok", "org", NOW, api)
        self.assertEqual([s["id"] for s in sessions], ["cse_routine"])

    def test_a_failing_trigger_list_is_said_beside_the_plain_list(self):
        # Not a quietly shorter list — the routine cards would vanish with no
        # sign of why — and not a blank section either: the plain list does
        # not need the beta endpoint, so a retired beta must not take it down.
        api = RoutineApi(listed=[raw_session()], trigger_status=404)
        with unittest.mock.patch("sys.stderr"):
            sessions, state, routine = fetch_sessions("tok", "org", NOW, api)
        self.assertEqual(state, "ok")
        self.assertEqual([s["id"] for s in sessions], ["cse_01abc"])
        self.assertEqual(routine, "routine sessions unknown: HTTP 404")

    def test_a_401_from_the_trigger_list_is_not_a_lapsed_login(self):
        # The plain list accepted the same token a moment earlier.
        api = RoutineApi(listed=[raw_session()], trigger_status=401)
        with unittest.mock.patch("sys.stderr"):
            sessions, state, routine = fetch_sessions("tok", "org", NOW, api)
        self.assertEqual(state, "ok")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(routine, "routine sessions unknown: HTTP 401")

    def test_a_trigger_list_without_a_list_is_an_error(self):
        def api(url, headers):
            if url.endswith("/triggers"):
                return 200, b'{"triggers": []}'
            return 200, b'{"data": []}'
        with unittest.mock.patch("sys.stderr"):
            _, _, routine = fetch_sessions("tok", "org", NOW, api)
        self.assertEqual(routine, "routine sessions unknown: unexpected response (see the collector's log)")

    def test_the_poller_serves_the_routine_line_with_its_list(self):
        api = RoutineApi(listed=[raw_session()], trigger_status=503)
        poller = CloudPoller(clock=lambda: NOW, get=api, login=lambda now: ("tok", "org"))
        with unittest.mock.patch("sys.stderr"):
            self.assertEqual(poller.poll_once(), POLL_SECS)
        view = poller.view(NOW)
        self.assertEqual(view["state"], "ok")
        self.assertEqual(len(view["sessions"]), 1)
        self.assertEqual(view["routine_detail"], "routine sessions unknown: HTTP 503")
        self.assertIsNone(poller.view(NOW + MAX_AGE_SECS + 1)["routine_detail"])

    def test_the_poller_remembers_archived_routine_sessions(self):
        api = RoutineApi(
            triggers=[raw_trigger()],
            by_trigger={"trig_1": [raw_session(id="cse_routine", status="archived")]},
        )
        poller = CloudPoller(clock=lambda: NOW, get=api, login=lambda now: ("tok", "org"))
        poller.poll_once()
        poller.poll_once()
        self.assertEqual(len(api.trigger_lookups()), 1)


class TestReadLogin(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.creds = os.path.join(self.dir, "creds.json")
        self.account = os.path.join(self.dir, "account.json")
        with open(self.account, "w") as handle:
            json.dump({"oauthAccount": {"organizationUuid": "org-1"}}, handle)

    def write_creds(self, expires_at):
        with open(self.creds, "w") as handle:
            json.dump({"claudeAiOauth": {"accessToken": "tok", "expiresAt": expires_at}}, handle)

    def test_reads_token_and_org(self):
        self.write_creds((NOW + 3600) * 1000)
        self.assertEqual(read_login(NOW, self.creds, self.account), ("tok", "org-1"))

    def test_an_expired_token_is_not_sent(self):
        self.write_creds((NOW - 1) * 1000)
        with self.assertRaises(LoginUnavailable) as caught:
            read_login(NOW, self.creds, self.account)
        self.assertEqual(caught.exception.state, "login_expired")

    def test_no_file_is_no_login(self):
        with self.assertRaises(LoginUnavailable) as caught:
            read_login(NOW, self.creds, self.account)
        self.assertEqual(caught.exception.state, "no_login")


class TestPoller(unittest.TestCase):
    def make(self, api, login=lambda now: ("tok", "org")):
        self.now = NOW
        return CloudPoller(clock=lambda: self.now, get=api, login=login)

    def test_nothing_is_claimed_before_the_first_poll(self):
        view = self.make(FakeApi([])).view(NOW)
        self.assertEqual(view["state"], "starting")
        self.assertIsNone(view["sessions"])

    def test_a_good_poll_is_served_with_ages(self):
        poller = self.make(FakeApi([{"data": [raw_session()]}]))
        self.assertEqual(poller.poll_once(), POLL_SECS)
        view = poller.view(NOW + 10)
        self.assertEqual(view["state"], "ok")
        self.assertEqual(view["sessions"][0]["age_secs"], 130)
        self.assertEqual(view["as_of"], NOW)

    def test_a_failure_keeps_the_last_list_but_says_so(self):
        api = FakeApi([{"data": [raw_session()]}])
        poller = self.make(api)
        poller.poll_once()
        api.status = 500
        self.now = NOW + 60
        with unittest.mock.patch("sys.stderr"):
            wait = poller.poll_once()
        self.assertGreater(wait, POLL_SECS)
        view = poller.view(NOW + 60)
        self.assertEqual(view["state"], "error")
        self.assertEqual(len(view["sessions"]), 1)
        self.assertEqual(view["as_of"], NOW)

    def test_an_old_list_is_withheld_and_dated(self):
        poller = self.make(FakeApi([{"data": [raw_session()]}]))
        poller.poll_once()
        view = poller.view(NOW + MAX_AGE_SECS + 1)
        self.assertIsNone(view["sessions"])
        self.assertEqual(view["stale_as_of"], NOW)

    def test_backoff_is_capped(self):
        poller = self.make(FakeApi([], status=429))
        with unittest.mock.patch("sys.stderr"):
            waits = [poller.poll_once() for _ in range(10)]
        self.assertEqual(max(waits), MAX_BACKOFF_SECS)

    def test_a_lapsed_login_does_not_back_off(self):
        def expired(now):
            raise LoginUnavailable("login_expired", "expired")
        poller = self.make(FakeApi([]), login=expired)
        self.assertEqual(poller.poll_once(), POLL_SECS)
        self.assertEqual(poller.view(NOW)["state"], "login_expired")

    def ctx_as_of(self, poller, at):
        s = poller.view(at)["sessions"][0]
        return s["ctx_as_of"], s["ctx_as_of_exact"]

    def used(self, tokens, **overrides):
        meta = dict(raw_session()["external_metadata"])
        meta["context_usage"] = {"max_tokens": 1000000, "used_tokens": tokens}
        return {"data": [raw_session(external_metadata=meta, **overrides)]}

    def test_the_context_figure_is_dated_by_when_a_poll_first_saw_it(self):
        api = FakeApi([self.used(100), self.used(100), self.used(200)])
        poller = self.make(api)
        poller.poll_once()
        # Seen on the first poll: it may be older than that, and says so.
        self.assertEqual(self.ctx_as_of(poller, NOW), (NOW, False))
        self.now = NOW + POLL_SECS
        poller.poll_once()
        self.assertEqual(self.ctx_as_of(poller, self.now), (NOW, False))
        self.now = NOW + 2 * POLL_SECS
        poller.poll_once()
        self.assertEqual(self.ctx_as_of(poller, self.now), (self.now, True))

    def test_a_session_started_since_the_last_poll_is_dated_exactly(self):
        api = FakeApi([{"data": []}, self.used(100, created_at=iso(NOW + 30))])
        poller = self.make(api)
        poller.poll_once()
        self.now = NOW + POLL_SECS
        poller.poll_once()
        self.assertEqual(self.ctx_as_of(poller, self.now), (self.now, True))

    def test_a_change_seen_after_a_gap_in_polling_is_not_dated_exactly(self):
        api = FakeApi([self.used(100), self.used(200)])
        poller = self.make(api)
        poller.poll_once()
        self.now = NOW + 3 * POLL_SECS
        poller.poll_once()
        self.assertEqual(self.ctx_as_of(poller, self.now), (self.now, False))

    def test_no_figure_has_no_date(self):
        meta = dict(raw_session()["external_metadata"])
        del meta["context_usage"]
        poller = self.make(FakeApi([{"data": [raw_session(external_metadata=meta)]}]))
        poller.poll_once()
        self.assertEqual(self.ctx_as_of(poller, NOW), (None, False))

    def test_an_unexpected_exception_is_contained(self):
        def boom(url, headers):
            raise TypeError("surprise")
        poller = self.make(boom)
        with unittest.mock.patch("sys.stderr"):
            poller.poll_once()
        self.assertEqual(poller.view(NOW)["state"], "error")


if __name__ == "__main__":
    unittest.main()


class TestUntrustedApiValues(unittest.TestCase):
    """The API is not an attacker, but it is somebody else's undocumented
    schema, and what it says goes into an href and onto the page."""

    def test_an_id_that_is_not_a_plain_token_is_not_linked(self):
        for bad in ("..", "cse_01/../x", "cse 01", "", "a" * 200):
            with self.subTest(id=bad):
                self.assertIsNone(parse_session(raw_session(id=bad)))

    def test_context_that_overflows_is_dropped(self):
        raw = raw_session()
        raw["external_metadata"]["context_usage"] = {"max_tokens": 1e-300, "used_tokens": 1e300}
        self.assertIsNone(parse_session(raw)["ctx_pct"])

    def test_an_unexpected_failure_shows_fixed_text(self):
        def api(url, headers):
            raise RuntimeError("<b>whatever the response held</b>")
        poller = CloudPoller(clock=lambda: NOW, get=api, login=lambda now: ("tok", "org"))
        with unittest.mock.patch("traceback.print_exc"):
            poller.poll_once()
        detail = poller.view(NOW)["detail"]
        self.assertNotIn("whatever", detail)

    def test_a_redirect_is_not_followed_with_the_login(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from collector.cloud import http_get

        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", "/elsewhere")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        status, _ = http_get("http://127.0.0.1:%d/start" % server.server_address[1],
                             {"Authorization": "Bearer secret"}, timeout=5)
        self.assertEqual(status, 302)
        self.assertEqual([p for p, _ in seen], ["/start"])
