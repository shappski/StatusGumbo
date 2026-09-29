import copy
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from collector.store import (
    ACTIVE_SECS,
    DROP_SESSION_SECS,
    HISTORY_MIN_INTERVAL_SECS,
    HISTORY_SLOTS,
    DROP_HOST_SECS,
    RATE_LIMITS_MAX_AGE_SECS,
    SessionStore,
    local_host_name,
    local_place,
)

BASE_PAYLOAD = {
    "session_id": "sess-1",
    "session_name": "statusgumbo",
    "model": {"display_name": "Opus"},
    "workspace": {"current_dir": "/home/user/workspace/demo-app"},
    "version": "2.1.220",
    "cost": {"total_cost_usd": 1.5},
    "context_window": {
        "used_percentage": 27,
        "total_input_tokens": 270000,
        "context_window_size": 1000000,
    },
    "effort": {"level": "high"},
    "fast_mode": False,
    "rate_limits": {
        "five_hour": {"used_percentage": 58, "resets_at": 1_785_000_000},
        "seven_day": {"used_percentage": 12, "resets_at": 1_785_400_000},
    },
}


def envelope(host="workstation", branch="master", **overrides):
    payload = copy.deepcopy(BASE_PAYLOAD)
    payload.update(overrides)
    return {"host": host, "branch": branch, "payload": payload}


class TestRateLimitsTakeTheFreshestSnapshot(unittest.TestCase):
    """Found on a phone on 2026-07-30: the figures flickered every few seconds
    between 5h 26% and 5h 59%, both dated "as of" now.

    rate_limits were replaced by whichever ingest arrived last. But a payload
    carries the snapshot the session last got from the API, not a reading taken
    at render time — so an idle session re-reports an old snapshot every 10
    seconds forever. One of the two figures above had `resets_at` of 16:20 the
    *previous day*: a window that had already reset 26 hours earlier, presented
    as current account usage.

    It corrupts the pace figure too, which is `used% − elapsed%` derived from
    resets_at, so "behind" disagreed with the status line on the same machine.

    The payload dates itself, which is what makes this fixable: `resets_at`
    identifies the window, and within one window usage only ever increases. So
    a snapshot is superseded only by one at least as advanced, and a window
    that has passed its own reset time describes nothing current.
    """

    def setUp(self):
        self.store = SessionStore()

    def test_a_snapshot_from_an_earlier_window_does_not_replace_a_later_one(self):
        now = 1_784_990_000
        self.store.ingest(envelope(), now)
        stale = envelope(host="idle-session")
        stale["payload"]["rate_limits"] = {
            "five_hour": {"used_percentage": 59, "resets_at": 1_784_900_000},
            "seven_day": {"used_percentage": 12, "resets_at": 1_785_400_000},
        }
        self.store.ingest(stale, now + 1)
        windows = self.store.snapshot(now + 2)["rate_limits"]["windows"]
        five = next(w for w in windows if w["name"] == "five_hour")
        self.assertEqual(five["used_percentage"], 58)
        self.assertEqual(five["resets_at"], 1_785_000_000)

    def test_a_lower_figure_in_the_same_window_does_not_replace_a_higher_one(self):
        # Usage within a window is monotonic, so the larger figure is the
        # later observation. Equal snapshots are re-confirmation, not staleness.
        now = 1_784_990_000
        self.store.ingest(envelope(), now)
        behind = envelope(host="idle-session")
        behind["payload"]["rate_limits"] = {
            "five_hour": {"used_percentage": 20, "resets_at": 1_785_000_000},
            "seven_day": {"used_percentage": 12, "resets_at": 1_785_400_000},
        }
        self.store.ingest(behind, now + 1)
        windows = self.store.snapshot(now + 2)["rate_limits"]["windows"]
        five = next(w for w in windows if w["name"] == "five_hour")
        self.assertEqual(five["used_percentage"], 58)

    def test_a_new_window_replaces_the_old_one_even_though_usage_drops(self):
        # The one case where a smaller number is the fresher truth: the window
        # reset. resets_at moves forward, which is what distinguishes it.
        now = 1_784_990_000
        self.store.ingest(envelope(), now)
        rolled = envelope(host="idle-session")
        rolled["payload"]["rate_limits"] = {
            "five_hour": {"used_percentage": 3, "resets_at": 1_785_018_000},
            "seven_day": {"used_percentage": 12, "resets_at": 1_785_400_000},
        }
        self.store.ingest(rolled, now + 1)
        windows = self.store.snapshot(now + 2)["rate_limits"]["windows"]
        five = next(w for w in windows if w["name"] == "five_hour")
        self.assertEqual(five["used_percentage"], 3)
        self.assertEqual(five["resets_at"], 1_785_018_000)

    def test_a_window_past_its_own_reset_is_withheld(self):
        # Nothing has reported since the window rolled over, so the stored
        # figure describes a window that no longer exists. `—` is the honest
        # answer; the number would be a confident falsehood and its pace worse.
        # Ingest shortly before the reset and read shortly after it, so the
        # snapshot is well inside RATE_LIMITS_MAX_AGE_SECS and this test is
        # about the rollover rather than about expiry.
        self.store.ingest(envelope(), 1_784_999_800)
        windows = self.store.snapshot(1_785_000_001)["rate_limits"]["windows"]
        names = [w["name"] for w in windows]
        self.assertNotIn("five_hour", names)
        self.assertIn("seven_day", names)


class TestIngestValidation(unittest.TestCase):
    def setUp(self):
        self.store = SessionStore()

    def test_rejects_non_dict_envelope(self):
        with self.assertRaises(ValueError):
            self.store.ingest([], 1000)

    def test_rejects_missing_host(self):
        env = envelope()
        del env["host"]
        with self.assertRaises(ValueError):
            self.store.ingest(env, 1000)

    def test_rejects_missing_session_id(self):
        env = envelope()
        del env["payload"]["session_id"]
        with self.assertRaises(ValueError):
            self.store.ingest(env, 1000)

    def test_rejects_non_dict_payload(self):
        env = envelope()
        env["payload"] = None
        with self.assertRaises(ValueError):
            self.store.ingest(env, 1000)

    def test_rejects_list_payload(self):
        env = envelope()
        env["payload"] = []
        with self.assertRaises(ValueError):
            self.store.ingest(env, 1000)

    def test_ignores_unknown_payload_fields(self):
        # A Claude Code release that adds keys must not break ingest.
        env = envelope(some_future_field={"nested": True})
        self.store.ingest(env, 1000)
        self.assertEqual(len(self.store.snapshot(1000)["sessions"]), 1)


class TestSessionRecord(unittest.TestCase):
    def setUp(self):
        self.store = SessionStore()
        self.store.ingest(envelope(), 1000)

    def test_extracts_the_display_fields(self):
        session = self.store.snapshot(1000)["sessions"][0]
        self.assertEqual(session["host"], "workstation")
        self.assertEqual(session["project"], "demo-app")
        self.assertEqual(session["branch"], "master")
        self.assertEqual(session["model"], "Opus")
        self.assertEqual(session["effort"], "high")
        self.assertEqual(session["ctx_pct"], 27)
        self.assertEqual(session["ctx_used_tokens"], 270000)
        self.assertEqual(session["ctx_window_size"], 1000000)
        self.assertEqual(session["cost_usd"], 1.5)

    def test_same_session_updates_rather_than_duplicates(self):
        self.store.ingest(envelope(), 1010)
        self.assertEqual(len(self.store.snapshot(1010)["sessions"]), 1)

    def test_same_session_id_on_two_hosts_are_distinct(self):
        self.store.ingest(envelope(host="coder-vm"), 1010)
        self.assertEqual(len(self.store.snapshot(1010)["sessions"]), 2)

    def test_history_accumulates_one_sample_per_timer_tick(self):
        # At refreshInterval: 10 every tick clears the throttle, so the
        # heartbeat case is unaffected by the sample-rate cap.
        self.store.ingest(envelope(), 1010)
        self.store.ingest(envelope(), 1020)
        history = self.store.snapshot(1020)["sessions"][0]["history"]
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0], {"t": 1000, "ctx_pct": 27})

    def test_history_is_capped_at_the_ring_size(self):
        for tick in range(HISTORY_SLOTS + 50):
            self.store.ingest(envelope(), 2000 + tick * HISTORY_MIN_INTERVAL_SECS)
        history = self.store.snapshot(
            2000 + (HISTORY_SLOTS + 50) * HISTORY_MIN_INTERVAL_SECS
        )["sessions"][0]["history"]
        self.assertEqual(len(history), HISTORY_SLOTS)


class TestExpiry(unittest.TestCase):
    """Two constants, two jobs. ACTIVE_SECS is the visibility cutoff;
    DROP_SESSION_SECS is retention nobody sees.

    Until 2026-07-31 a quiet session stayed on the page, greyed, for the
    fourteen minutes between them. Read from a phone that is noise: a cleared
    session sat under a live one in the same project on the same host, and the
    page's only job is what is running now.
    """

    def setUp(self):
        self.store = SessionStore()
        self.store.ingest(envelope(), 1000)

    def test_recent_session_is_served(self):
        # Inclusive boundary: at exactly ACTIVE_SECS the session is still on
        # the page. 45s is ~4 missed ticks, and a tick landing exactly on the
        # limit is a live session, not a lost one.
        sessions = self.store.snapshot(1000 + ACTIVE_SECS)["sessions"]
        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        # Belt-and-braces alongside the page-side guard in
        # test_index_html.py: state used to carry "active" or "stale", and
        # with nothing stale ever served it could only ever say "active" --
        # a constant dressed as an observation. The removal is enforced only
        # by a length check elsewhere in this file, so re-adding
        # item["state"] = "active" to _snapshot_locked would break nothing
        # without this.
        self.assertNotIn("state", session)

    def test_quiet_session_leaves_the_page(self):
        self.assertEqual(self.store.snapshot(1000 + ACTIVE_SECS + 1)["sessions"], [])

    def test_a_session_that_returns_keeps_its_history(self):
        # The whole argument for hiding rather than deleting, and the reason
        # this is a test and not a comment. Collapsing the two constants
        # (DROP_SESSION_SECS = ACTIVE_SECS) yields an identical page for a
        # smaller diff -- but deleting the record also discards history and
        # first_seen, so a laptop asleep for fifty seconds would lose an hour
        # of sparkline and return as a stranger. If this fails, that
        # regression has happened and it is invisible on screen.
        self.store.snapshot(1000 + ACTIVE_SECS + 1)  # gone from the page
        self.store.ingest(envelope(), 1060)
        session = self.store.snapshot(1060)["sessions"][0]
        self.assertEqual(session["first_seen"], 1000)
        self.assertEqual(len(session["history"]), 2)

    def test_quiet_session_survives_almost_to_drop_secs(self):
        # test_a_session_that_returns_keeps_its_history only proves the
        # record outlives ACTIVE_SECS at t=1060 -- sixty seconds in, nowhere
        # near the fourteen-minute edge DROP_SESSION_SECS actually promises.
        # The class docstring's "two constants, two jobs" claim only holds if
        # retention reaches all the way to its own boundary, not merely past
        # the visibility one, so this is the other direction.
        self.store.snapshot(1000 + DROP_SESSION_SECS - 1)
        self.assertIn(("workstation", "sess-1"), self.store.sessions)

    def test_long_quiet_session_is_forgotten(self):
        # Asserted against the record, not the served list. The served list is
        # empty from 46s, so the old form of this test would pass here without
        # testing anything.
        self.store.snapshot(1000 + DROP_SESSION_SECS + 1)
        self.assertEqual(self.store.sessions, {})


class TestHostFacts(unittest.TestCase):
    """Host records carry facts only.

    Silence cannot be interpreted: a host speaks solely through its sessions,
    and Claude Code emits nothing when a session exits, so a cleanly closed
    session and a dead tunnel produce identical state here. Any "is it down"
    judgement belongs to the server, which asks systemd about the tunnel.
    """

    def test_host_records_last_contact(self):
        store = SessionStore()
        store.ingest(envelope(), 1000)
        host = store.snapshot(1000)["hosts"][0]
        self.assertEqual(host["host"], "workstation")
        self.assertEqual(host["last_seen"], 1000)
        self.assertEqual(host["age_secs"], 0)

    def test_age_grows_with_silence(self):
        store = SessionStore()
        store.ingest(envelope(), 1000)
        self.assertEqual(store.snapshot(1090)["hosts"][0]["age_secs"], 90)

    def test_each_host_tracked_separately(self):
        store = SessionStore()
        store.ingest(envelope(host="workstation"), 1000)
        store.ingest(envelope(host="coder-vm"), 1010)
        hosts = {h["host"]: h for h in store.snapshot(1010)["hosts"]}
        self.assertEqual(hosts["workstation"]["last_seen"], 1000)
        self.assertEqual(hosts["coder-vm"]["last_seen"], 1010)

    def test_store_infers_no_fault_from_silence(self):
        # Guards the design decision itself. An earlier version derived a
        # contact_ok flag here; it was unreachable dead code and would have
        # alarmed on every ordinary session exit.
        store = SessionStore()
        store.ingest(envelope(), 1000)
        host = store.snapshot(1000 + ACTIVE_SECS + 1)["hosts"][0]
        self.assertNotIn("contact_ok", host)
        self.assertNotIn("session_count_at_last_seen", host)


class TestRateLimits(unittest.TestCase):
    def test_stored_once_not_per_session(self):
        store = SessionStore()
        store.ingest(envelope(host="workstation"), 1000)
        store.ingest(envelope(host="coder-vm"), 1001)
        rate_limits = store.snapshot(1001)["rate_limits"]
        self.assertEqual(rate_limits["source_host"], "coder-vm")
        self.assertEqual(len(rate_limits["windows"]), 2)

    def test_pace_is_computed_per_window(self):
        store = SessionStore()
        store.ingest(envelope(), 1_785_000_000 - 9000)
        windows = store.snapshot(1_785_000_000 - 9000)["rate_limits"]["windows"]
        five_hour = [w for w in windows if w["name"] == "five_hour"][0]
        self.assertIsNotNone(five_hour["pace"])

    def test_absent_rate_limits_render_as_none(self):
        # Normal for non-Pro/Max accounts and before the first API response.
        store = SessionStore()
        env = envelope()
        del env["payload"]["rate_limits"]
        store.ingest(env, 1000)
        self.assertIsNone(store.snapshot(1000)["rate_limits"])


class TestIngestNeverWedgesTheStore(unittest.TestCase):
    """FINDING 1. `ingest` inserted the record before the code that could
    raise, so one bad tick left a record with no `last_seen` and every later
    snapshot died with KeyError. `Restart=always` cannot recover that: the
    process is healthy, only the store is wedged, and the page's only symptom
    is a false "collector unreachable".
    """

    # _dig guarantees intermediate nodes are dicts. It guarantees nothing
    # about the leaf, and os.path.basename raises TypeError on any truthy
    # non-string — bool included, since bool is an int subclass.
    HOSTILE = (12345, 3.5, True, [], {}, ("a",))

    def test_non_string_current_dir_is_tolerated_with_an_unknown_project(self):
        for value in self.HOSTILE:
            with self.subTest(current_dir=value):
                store = SessionStore()
                env = envelope()
                env["payload"]["workspace"]["current_dir"] = value
                store.ingest(env, 1000)
                self.assertIsNone(store.snapshot(1000)["sessions"][0]["project"])

    def test_one_bad_tick_does_not_break_every_later_snapshot(self):
        store = SessionStore()
        bad = envelope(host="coder-vm")
        bad["payload"]["session_id"] = "wedge"
        bad["payload"]["workspace"]["current_dir"] = 12345
        try:
            store.ingest(bad, 1000)
        except ValueError:
            pass  # rejecting the tick is fine; wedging the store is not
        store.ingest(envelope(), 1010)  # an unrelated, perfectly good tick
        snapshot = store.snapshot(1010)
        self.assertIn("demo-app", [s["project"] for s in snapshot["sessions"]])

    def test_no_tick_ever_leaves_a_record_without_last_seen(self):
        # The structural guard behind fix 1a: everything that can fail is
        # computed before self.sessions is touched, so a half-built record
        # is not reachable regardless of which leaf turns out to be junk.
        store = SessionStore()
        for value in self.HOSTILE:
            env = envelope()
            env["payload"]["session_id"] = "s-%r" % (value,)
            env["payload"]["workspace"]["current_dir"] = value
            try:
                store.ingest(env, 1000)
            except ValueError:
                pass
        for key, record in store.sessions.items():
            self.assertIn("last_seen", record, key)
        store.snapshot(1000)


class TestRateLimitFreshness(unittest.TestCase):
    """FINDING 2. rate_limits were written on ingest and never expired, while
    the pace figure recomputed elapsed against a live clock — so a frozen
    "58% used" rendered as a steadily moving "21% behind -> 42% behind", and
    eventually a reset time in the past. Absent is honest; stale presented as
    current is not.
    """

    def test_rate_limits_survive_up_to_the_max_age(self):
        store = SessionStore()
        store.ingest(envelope(), 1000)
        fresh = store.snapshot(1000 + RATE_LIMITS_MAX_AGE_SECS)["rate_limits"]
        self.assertIsNotNone(fresh)

    def test_rate_limits_past_the_max_age_are_withheld(self):
        store = SessionStore()
        store.ingest(envelope(), 1000)
        stale = store.snapshot(1000 + RATE_LIMITS_MAX_AGE_SECS + 1)["rate_limits"]
        self.assertIsNone(stale)

    def test_a_fresh_tick_brings_them_back(self):
        store = SessionStore()
        store.ingest(envelope(), 1000)
        later = 1000 + RATE_LIMITS_MAX_AGE_SECS + 500
        store.ingest(envelope(), later)
        self.assertIsNotNone(store.snapshot(later)["rate_limits"])

    def test_a_served_view_reports_nothing_withheld(self):
        store = SessionStore()
        store.ingest(envelope(), 1000)
        snap = store.snapshot(1000 + RATE_LIMITS_MAX_AGE_SECS)
        self.assertIsNotNone(snap["rate_limits"])
        self.assertIsNone(snap["rate_limits_stale_as_of"])

    def test_a_withheld_view_says_when_the_figure_was_from(self):
        # The two silences the page could not tell apart. Withholding the
        # stale figure is unchanged; what is new is that the page can say
        # "nothing since 12:58" rather than "not reported yet" about an
        # account that reported all morning.
        store = SessionStore()
        store.ingest(envelope(), 1000)
        snap = store.snapshot(1000 + RATE_LIMITS_MAX_AGE_SECS + 1)
        self.assertIsNone(snap["rate_limits"])
        self.assertEqual(snap["rate_limits_stale_as_of"], 1000)

    def test_never_reported_is_not_reported_as_withheld(self):
        # The other silence, and the one the old wording was written for.
        store = SessionStore()
        env = envelope()
        del env["payload"]["rate_limits"]
        store.ingest(env, 1000)
        snap = store.snapshot(1000)
        self.assertIsNone(snap["rate_limits"])
        self.assertIsNone(snap["rate_limits_stale_as_of"])

    def test_a_fresh_tick_clears_the_withheld_marker(self):
        store = SessionStore()
        store.ingest(envelope(), 1000)
        later = 1000 + RATE_LIMITS_MAX_AGE_SECS + 500
        store.ingest(envelope(), later)
        self.assertIsNone(store.snapshot(later)["rate_limits_stale_as_of"])

    def test_the_view_is_attributable_to_a_moment(self):
        # The page renders this, so the figure is never an undated claim.
        store = SessionStore()
        store.ingest(envelope(), 1000)
        self.assertEqual(store.snapshot(1000)["rate_limits"]["as_of"], 1000)

    def test_non_numeric_values_do_not_break_the_snapshot(self):
        # FINDING 7, at the store level: this tick returned 204 and then
        # every later snapshot raised TypeError inside window_pace.
        store = SessionStore()
        env = envelope()
        env["payload"]["rate_limits"] = {
            "five_hour": {"used_percentage": "58", "resets_at": 1_785_000_000}
        }
        store.ingest(env, 1000)
        windows = store.snapshot(1000)["rate_limits"]["windows"]
        self.assertIsNone(windows[0]["pace"])


class TestCtxPctTypeGuard(unittest.TestCase):
    """The same reasoning as TestRateLimitFreshness's non-numeric guard,
    applied to context_window.used_percentage: unguarded, a string (or a
    bool, which is an int subclass) sailed into ctx_pct as a raw wire value
    and blew up the sort key in snapshot() on every request thereafter --
    not just for that session but for the whole page, since one store
    serves every host. It only healed at DROP_SESSION_SECS, and a valid
    tick from another host did not heal it early.
    """

    def test_non_numeric_used_percentage_is_accepted_and_stored_as_none(self):
        store = SessionStore()
        env = envelope()
        env["payload"]["context_window"]["used_percentage"] = "58"
        store.ingest(env, 1000)  # must not raise -- this is the 204 path
        session = store.snapshot(1000)["sessions"][0]
        self.assertIsNone(session["ctx_pct"])

    def test_a_bad_tick_does_not_wedge_other_hosts_sessions(self):
        # This is the "still dark after a valid tick from another host"
        # symptom: a later snapshot must succeed and must still list a
        # session from a different host.
        store = SessionStore()
        bad = envelope(host="laptop")
        bad["payload"]["context_window"]["used_percentage"] = "58"
        store.ingest(bad, 1000)
        good = envelope(host="workstation")
        store.ingest(good, 1010)
        hosts = [s["host"] for s in store.snapshot(1010)["sessions"]]
        self.assertEqual(sorted(hosts), ["laptop", "workstation"])

    def test_bool_used_percentage_is_not_treated_as_numeric(self):
        store = SessionStore()
        env = envelope()
        env["payload"]["context_window"]["used_percentage"] = True
        store.ingest(env, 1000)
        session = store.snapshot(1000)["sessions"][0]
        self.assertIsNone(session["ctx_pct"])

    def test_a_genuine_zero_is_preserved_not_treated_as_absent(self):
        store = SessionStore()
        env = envelope()
        env["payload"]["context_window"]["used_percentage"] = 0
        store.ingest(env, 1000)
        session = store.snapshot(1000)["sessions"][0]
        self.assertEqual(session["ctx_pct"], 0)


class TestHistorySampleRate(unittest.TestCase):
    """FINDING 4. HISTORY_SLOTS claimed "30 minutes at a 10s tick", but the
    status line also fires on events debounced at 300ms and ingest appended
    one sample per tick. During active work the same 180-point sparkline
    covered under two minutes, and its x-axis is index rather than time — so
    a steep climb looked identical whether it happened over 30 minutes or 90
    seconds, defeating the point of showing trajectory.
    """

    def test_bursty_event_ticks_are_throttled(self):
        store = SessionStore()
        for i in range(100):  # 100 event ticks over ~30s, 300ms apart
            store.ingest(envelope(), 1000 + i * 0.3)
        history = store.snapshot(1030)["sessions"][0]["history"]
        self.assertLessEqual(len(history), 4)

    def test_samples_are_never_closer_than_the_minimum_interval(self):
        store = SessionStore()
        for i in range(200):
            store.ingest(envelope(), 1000 + i * 0.3)
        history = store.snapshot(1060)["sessions"][0]["history"]
        gaps = [
            b["t"] - a["t"] for a, b in zip(history, history[1:])
        ]
        self.assertTrue(gaps)
        for gap in gaps:
            self.assertGreaterEqual(gap, HISTORY_MIN_INTERVAL_SECS)

    def test_a_full_ring_spans_at_least_the_advertised_window(self):
        # The floor, and the only case in which it is also the ceiling: when
        # ticks arrive faster than the throttle, the throttle sets the gap.
        store = SessionStore()
        ticks = HISTORY_SLOTS * HISTORY_MIN_INTERVAL_SECS * 2
        for i in range(ticks):
            store.ingest(envelope(), 1000 + i)  # 1s ticks: far faster than 10s
        history = store.snapshot(1000 + ticks)["sessions"][0]["history"]
        self.assertEqual(len(history), HISTORY_SLOTS)
        span = history[-1]["t"] - history[0]["t"]
        self.assertEqual(span, (HISTORY_SLOTS - 1) * HISTORY_MIN_INTERVAL_SECS)

    def test_a_slow_tick_makes_the_ring_cover_more_than_the_advertised_window(self):
        # The comment on HISTORY_SLOTS used to claim a flat 30 minutes
        # "whatever rate the ticks arrive at". That is backwards: the throttle
        # sets a *minimum* gap, so a session ticking slower than it fills the
        # same 180 slots with proportionally more wall clock. Two live
        # sessions ticking every ~20s and ~10s were observed covering 46 and
        # 44 minutes, not 30.
        #
        # This is why the page captions each sparkline with the span it
        # measured: the window is a property of the traffic, not a constant
        # any reader could infer from the picture.
        store = SessionStore()
        gap = HISTORY_MIN_INTERVAL_SECS * 2
        for i in range(HISTORY_SLOTS + 10):
            store.ingest(envelope(), 1000 + i * gap)
        now = 1000 + (HISTORY_SLOTS + 10) * gap
        history = store.snapshot(now)["sessions"][0]["history"]
        self.assertEqual(len(history), HISTORY_SLOTS)
        span = history[-1]["t"] - history[0]["t"]
        self.assertEqual(span, (HISTORY_SLOTS - 1) * gap)
        self.assertGreater(span, HISTORY_SLOTS * HISTORY_MIN_INTERVAL_SECS)


class TestConcurrency(unittest.TestCase):
    """FINDING 3. main() builds two ThreadingHTTPServers over one store by
    construction — loopback for the reporter and the tunnel, the tailnet
    address for the phone — each with daemon_threads. Nothing in SessionStore
    locked, so snapshot() iterated self.sessions while ingest() inserted, and
    two concurrent snapshots could both `del` the same key.
    """

    def test_concurrent_writers_and_readers_raise_nothing(self):
        store = SessionStore()
        errors = []
        iterations = 300

        def writer(index):
            try:
                for i in range(iterations):
                    env = envelope(host="host-%d" % index)
                    env["payload"]["session_id"] = "s-%d-%d" % (index, i % 40)
                    # Advancing 60s a step keeps sessions crossing the drop
                    # threshold, so snapshot() is deleting while ingest()
                    # inserts. Without that churn the race barely shows.
                    store.ingest(env, 1000 + i * 60)
            except Exception as exc:  # noqa: BLE001 - the test IS the report
                errors.append("writer-%d: %r" % (index, exc))

        def reader(index):
            try:
                for i in range(iterations):
                    store.snapshot(1000 + i * 60)
            except Exception as exc:  # noqa: BLE001
                errors.append("reader-%d: %r" % (index, exc))

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(3)]
        threads += [threading.Thread(target=reader, args=(i,)) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(), "a worker thread hung")
        self.assertEqual(errors, [])

    def test_a_rejected_tick_still_releases_the_lock(self):
        # ingest raises ValueError from inside the locked region; a `with`
        # block releases regardless, but a regression here would deadlock
        # the whole collector rather than fail a request.
        store = SessionStore()
        with self.assertRaises(ValueError):
            store.ingest({"host": "h"}, 1000)
        store.ingest(envelope(), 1000)
        self.assertEqual(len(store.snapshot(1000)["sessions"]), 1)


class TestOrdering(unittest.TestCase):
    """Ordered by host, then project, then session_id.

    This replaced a -ctx_pct ordering. The old key put the busiest session
    first, but every card moved whenever any session's context changed, which
    on a phone refreshing every 5s is continuous: a card slid out from under
    your thumb, and two could swap places between one refresh and the next.
    Position is what makes a card findable a second time, so the key is built
    from things that do not change while you are looking at them.
    """

    def _order(self, store, now=1000):
        return [s["session_id"] for s in store.snapshot(now)["sessions"]]

    def _session(self, session_id, host="host-a", current_dir="/home/u/proj"):
        env = envelope()
        env["host"] = host
        env["payload"]["session_id"] = session_id
        env["payload"]["workspace"]["current_dir"] = current_dir
        return env

    def test_sessions_group_by_host(self):
        store = SessionStore()
        for env in (
            self._session("b1", host="beta"),
            self._session("a1", host="alpha"),
            self._session("b2", host="beta"),
            self._session("a2", host="alpha"),
        ):
            store.ingest(env, 1000)
        order = self._order(store)
        self.assertEqual(order.index("a1") + 1, order.index("a2"))
        self.assertEqual(order.index("b1") + 1, order.index("b2"))
        self.assertLess(order.index("a2"), order.index("b1"))

    def test_projects_are_alphabetical_within_a_host(self):
        store = SessionStore()
        for name in ("zeta", "alpha", "mid"):
            store.ingest(
                self._session(name, current_dir="/home/u/" + name), 1000
            )
        self.assertEqual(self._order(store), ["alpha", "mid", "zeta"])

    def test_context_no_longer_moves_a_card(self):
        # The regression this ordering exists to prevent.
        store = SessionStore()
        quiet = self._session("quiet", current_dir="/home/u/alpha")
        quiet["payload"]["context_window"]["used_percentage"] = 5
        busy = self._session("busy", current_dir="/home/u/beta")
        busy["payload"]["context_window"]["used_percentage"] = 95
        store.ingest(quiet, 1000)
        store.ingest(busy, 1000)
        before = self._order(store)
        busy["payload"]["context_window"]["used_percentage"] = 5
        quiet["payload"]["context_window"]["used_percentage"] = 95
        store.ingest(busy, 1001)
        store.ingest(quiet, 1001)
        self.assertEqual(self._order(store, 1001), before)

    def test_missing_project_sorts_without_crashing(self):
        # project is None when the payload carries no workspace.current_dir,
        # and None has no ordering against a str.
        store = SessionStore()
        known = self._session("known", current_dir="/home/u/alpha")
        unknown = self._session("unknown")
        del unknown["payload"]["workspace"]["current_dir"]
        store.ingest(unknown, 1000)
        store.ingest(known, 1000)
        self.assertEqual(sorted(self._order(store)), ["known", "unknown"])

    def test_two_sessions_in_one_directory_have_a_total_order(self):
        # Otherwise their relative order is whatever dict iteration happens
        # to give: stable in practice, guaranteed by nothing.
        store = SessionStore()
        store.ingest(self._session("second"), 1000)
        store.ingest(self._session("first"), 1000)
        self.assertEqual(self._order(store), ["first", "second"])


if __name__ == "__main__":
    unittest.main()


class TestTheLocalHostIsNeverAbsent(unittest.TestCase):
    """2026-09-16. The page was fetched from workstation:4747 and
    workstation was not on it: the laptop had stopped reporting, its record
    aged out at DROP_HOST_SECS, and the heading went with it. A collector
    restart produces the same silence sooner, this store being in memory.

    Omitting a machine is not a quieter way of saying "not reporting". It
    narrows the page's set of machines to "heard from lately" while it still
    reads as "yours" — which is the omission this project exists to prevent.
    """

    def hosts(self, snapshot):
        return {h["host"]: h for h in snapshot["hosts"]}

    def test_it_is_listed_before_it_has_ever_reported(self):
        store = SessionStore(local_host="workstation")
        hosts = self.hosts(store.snapshot(1000))
        self.assertIn("workstation", hosts)
        self.assertIsNone(hosts["workstation"]["last_seen"])

    def test_it_survives_the_drop_that_removes_any_other_host(self):
        store = SessionStore(local_host="workstation")
        store.ingest(envelope(host="workstation"), 1000)
        store.ingest(envelope(host="devbox"), 1000)
        hosts = self.hosts(store.snapshot(1000 + DROP_HOST_SECS + 1))
        self.assertIn("workstation", hosts)
        self.assertNotIn("devbox", hosts)

    def test_surviving_keeps_the_date_of_its_last_tick(self):
        # The point of exempting the record rather than re-adding the name:
        # a bare name could only say "no ticks yet", which would be false
        # about a machine that reported all morning.
        store = SessionStore(local_host="workstation")
        store.ingest(envelope(host="workstation"), 1000)
        hosts = self.hosts(store.snapshot(1000 + DROP_HOST_SECS + 1))
        self.assertEqual(hosts["workstation"]["last_seen"], 1000)

    def test_a_store_with_no_local_host_is_unchanged(self):
        store = SessionStore()
        store.ingest(envelope(host="devbox"), 1000)
        self.assertEqual(self.hosts(store.snapshot(1000 + DROP_HOST_SECS + 1)), {})

    def test_it_is_not_listed_twice_once_it_reports(self):
        # The injected entry and the real record are the same name, so the
        # page must not grow two headings for one machine.
        store = SessionStore(local_host="workstation")
        store.ingest(envelope(host="workstation"), 1000)
        names = [h["host"] for h in store.snapshot(1000)["hosts"]]
        self.assertEqual(names.count("workstation"), 1)


class TestLocalHostNameMatchesTheReporter(unittest.TestCase):
    """report.sh:49 is the other half of this and cannot import from here:

        host=${STATUSGUMBO_HOST:-$(hostname -s 2>/dev/null || echo unknown)}

    If the two derivations drift the page grows two headings for one machine.
    """

    def test_the_override_wins_exactly_as_it_does_in_the_shell(self):
        previous = os.environ.get("STATUSGUMBO_HOST")
        os.environ["STATUSGUMBO_HOST"] = "coder-vm"
        try:
            self.assertEqual(local_host_name(), "coder-vm")
        finally:
            if previous is None:
                del os.environ["STATUSGUMBO_HOST"]
            else:
                os.environ["STATUSGUMBO_HOST"] = previous

    def test_an_fqdn_is_reduced_to_the_leaf_that_hostname_s_prints(self):
        previous = os.environ.pop("STATUSGUMBO_HOST", None)
        try:
            with mock.patch("socket.gethostname", return_value="host.tail1234.ts.net"):
                self.assertEqual(local_host_name(), "host")
        finally:
            if previous is not None:
                os.environ["STATUSGUMBO_HOST"] = previous

    def test_a_box_with_no_usable_hostname_says_unknown(self):
        previous = os.environ.pop("STATUSGUMBO_HOST", None)
        try:
            with mock.patch("socket.gethostname", side_effect=OSError):
                self.assertEqual(local_host_name(), "unknown")
        finally:
            if previous is not None:
                os.environ["STATUSGUMBO_HOST"] = previous


class TestEachHostSaysWhatKindOfMachineItIs(unittest.TestCase):
    """2026-09-26. The page is read to tell at a glance whether a session is
    on the laptop, a Coder VM or in the cloud. Only the first two are hosts;
    the label must come from something known, never from a host merely being
    remote.
    """

    def hosts(self, snapshot):
        return {h["host"]: h for h in snapshot["hosts"]}

    def test_a_host_that_says_it_is_coder_is_labelled_coder(self):
        store = SessionStore(local_host="workstation")
        tick = envelope(host="devbox")
        tick["where"] = "coder"
        store.ingest(tick, 1000)
        self.assertEqual(self.hosts(store.snapshot(1000))["devbox"]["place"], "coder")

    def test_the_collectors_own_host_takes_the_collectors_place(self):
        store = SessionStore(local_host="workstation", local_place="laptop")
        store.ingest(envelope(host="workstation"), 1000)
        self.assertEqual(self.hosts(store.snapshot(1000))["workstation"]["place"], "laptop")

    def test_the_collectors_host_is_labelled_before_it_has_ever_reported(self):
        store = SessionStore(local_host="workstation", local_place="laptop")
        self.assertEqual(self.hosts(store.snapshot(1000))["workstation"]["place"], "laptop")

    def test_the_collectors_host_is_not_assumed_to_be_a_laptop(self):
        # 2026-09-29, open-source plan step 3. It used to be "laptop" by
        # deployment. On someone else's server that is a guess.
        store = SessionStore(local_host="server-1")
        store.ingest(envelope(host="server-1"), 1000)
        self.assertIsNone(self.hosts(store.snapshot(1000))["server-1"]["place"])

    def test_what_a_host_says_wins_over_the_collectors_place(self):
        store = SessionStore(local_host="workstation", local_place="laptop")
        tick = envelope(host="workstation")
        tick["where"] = "desktop"
        store.ingest(tick, 1000)
        self.assertEqual(self.hosts(store.snapshot(1000))["workstation"]["place"], "desktop")

    def test_a_remote_host_that_says_nothing_is_not_assumed_to_be_a_vm(self):
        # The old reporter, still vendored on a VM until dotfiles catch up,
        # sends no `where`. Remote is not the same fact as Coder.
        store = SessionStore(local_host="workstation")
        store.ingest(envelope(host="devbox"), 1000)
        self.assertIsNone(self.hosts(store.snapshot(1000))["devbox"]["place"])

    def test_any_short_plain_label_is_kept(self):
        for word in ("desktop", "vm-1", "ci", "a" * 24):
            with self.subTest(word=word):
                store = SessionStore(local_host="workstation")
                tick = envelope(host="box")
                tick["where"] = word
                store.ingest(tick, 1000)
                self.assertEqual(self.hosts(store.snapshot(1000))["box"]["place"], word)

    def test_a_label_that_is_not_plain_is_dropped(self):
        for word in ("<b>mainframe</b>", "Desktop", "a" * 25, "", "two words", 7):
            with self.subTest(word=word):
                store = SessionStore(local_host="workstation")
                tick = envelope(host="box")
                tick["where"] = word
                store.ingest(tick, 1000)
                self.assertIsNone(self.hosts(store.snapshot(1000))["box"]["place"])

    def test_a_host_can_change_what_it_says(self):
        store = SessionStore(local_host="workstation")
        tick = envelope(host="box")
        tick["where"] = "vm"
        store.ingest(tick, 1000)
        tick["where"] = "desktop"
        store.ingest(tick, 1010)
        self.assertEqual(self.hosts(store.snapshot(1010))["box"]["place"], "desktop")

    def test_a_session_that_says_nothing_does_not_unlabel_the_host(self):
        # Two sessions on one VM, one outside the agent's environment: the
        # label must not flicker with whichever ticked last.
        store = SessionStore(local_host="workstation")
        tick = envelope(host="devbox")
        tick["where"] = "coder"
        store.ingest(tick, 1000)
        store.ingest(envelope(host="devbox"), 1010)
        self.assertEqual(self.hosts(store.snapshot(1010))["devbox"]["place"], "coder")


class TestTheCollectorsOwnPlace(unittest.TestCase):
    """STATUSGUMBO_PLACE, else the same ~/.config/statusgumbo/place file the
    reporter reads, so one file labels the machine on both paths."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def write(self, text):
        os.makedirs(os.path.join(self.dir, "statusgumbo"), exist_ok=True)
        with open(os.path.join(self.dir, "statusgumbo", "place"), "w") as handle:
            handle.write(text)

    def test_nothing_configured_is_none(self):
        self.assertIsNone(local_place({"XDG_CONFIG_HOME": self.dir}))

    def test_from_the_environment(self):
        self.assertEqual(local_place({"STATUSGUMBO_PLACE": "laptop"}), "laptop")

    def test_from_the_file(self):
        self.write("laptop\n")
        self.assertEqual(local_place({"XDG_CONFIG_HOME": self.dir}), "laptop")

    def test_the_environment_wins(self):
        self.write("laptop\n")
        self.assertEqual(
            local_place({"XDG_CONFIG_HOME": self.dir, "STATUSGUMBO_PLACE": "server"}),
            "server",
        )

    def test_a_value_that_is_not_plain_is_none(self):
        self.assertIsNone(local_place({"STATUSGUMBO_PLACE": "My Laptop"}))


class TestARemoteControlSessionLinksToClaudeAi(unittest.TestCase):
    """2026-09-28. A local session with Remote Control on is also a session
    on claude.ai, and tapping its card should open it there — in the Claude
    app on the phone. report.sh sends the bridge id Claude Code recorded in
    the transcript; the URL is built here, so no link on the page is ever
    one the wire chose.
    """

    def url(self, tick):
        store = SessionStore(local_host="workstation")
        store.ingest(tick, 1000)
        return store.snapshot(1000)["sessions"][0]["url"]

    def test_a_bridge_id_becomes_the_link_claude_code_itself_prints(self):
        tick = envelope()
        tick["bridge"] = "cse_01ExampleRemoteControl0"
        self.assertEqual(
            self.url(tick), "https://claude.ai/code/session_01ExampleRemoteControl0"
        )

    def test_no_bridge_no_link(self):
        self.assertIsNone(self.url(envelope()))

    def test_anything_but_a_plain_bridge_id_is_not_linked(self):
        for bad in ["javascript:alert(1)", "cse_", "cse_a/../b", "session_01abc", 7, None]:
            tick = envelope()
            tick["bridge"] = bad
            self.assertIsNone(self.url(tick), bad)

    def test_a_tick_without_it_clears_the_link(self):
        # The link is a fact about the latest tick, like every other field
        # on the card: nothing about Remote Control is remembered here.
        store = SessionStore(local_host="workstation")
        tick = envelope()
        tick["bridge"] = "cse_01abc"
        store.ingest(tick, 1000)
        store.ingest(envelope(), 1010)
        self.assertIsNone(store.snapshot(1010)["sessions"][0]["url"])
