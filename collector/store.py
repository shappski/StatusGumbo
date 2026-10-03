"""In-memory session state for StatusGumbo.

One record per (host, session_id), plus a smaller per-host map of when each
host was last heard from. The host map records facts only: a host speaks
solely through its sessions, and Claude Code emits nothing when a session
exits, so a cleanly ended session and a dead tunnel are indistinguishable
from this stream alone. Whether a quiet host is a problem is answered
elsewhere (the server asking systemd about the tunnel), not inferred here.

No persistence by design: history is measured in minutes, and a restart
repopulates within one refreshInterval tick.
"""

import os
import re
import socket
import threading
from collections import deque

from collector.usage import FIVE_HOUR_SECS, SEVEN_DAY_SECS, _is_number, window_pace

HISTORY_MIN_INTERVAL_SECS = 10
                             # The status line fires on events too, debounced
                             # at 300ms, so the tick is not the clock. Without
                             # this floor the ring filled with sub-second
                             # samples and a "30 minute" sparkline could cover
                             # 90 seconds — and since its x-axis is the sample
                             # index, the two were indistinguishable on screen.
HISTORY_SLOTS = 180          # A full ring covers *at least* HISTORY_SLOTS *
                             # HISTORY_MIN_INTERVAL_SECS = 30 minutes, and
                             # more whenever ticks arrive slower than that
                             # floor — which is the ordinary case, not the
                             # exception: two observed sessions ticking every
                             # ~20s and ~10s filled their 180 slots with 46
                             # and 44 minutes respectively.
                             #
                             # An earlier version of this comment claimed a
                             # flat 30 minutes "whatever rate the ticks
                             # arrive at", which is backwards — the throttle
                             # sets a minimum gap, so the tick rate is
                             # precisely what the coverage depends on. The
                             # page therefore captions each line with the
                             # span it measured rather than naming a window
                             # this constant cannot promise.
ACTIVE_SECS = 45             # The visibility cutoff: a card outlives its last
                             # tick by this much and no longer. ~4 missed
                             # ticks — tolerant of a slow tick, quick to
                             # notice a real exit.
DROP_SESSION_SECS = 15 * 60  # Retention, not display. Nothing past
                             # ACTIVE_SECS is served, so this window is never
                             # on screen: it is how long a session that goes
                             # quiet and comes back is still the *same*
                             # session, keeping its history and first_seen.
                             # Setting this to ACTIVE_SECS would draw the same
                             # page for a smaller diff and make a fifty-second
                             # sleep wipe an hour of sparkline.
DROP_HOST_SECS = 60 * 60
BRIDGE_ID = re.compile(r"cse_[A-Za-z0-9]+")
# What a machine may call itself on the page: one short plain word. Anything
# else is dropped, not cleaned up -- a label no one stated is not shown.
PLACE = re.compile(r"[a-z0-9-]{1,24}")
                             # Remote Control's session id as Claude Code
                             # records it; anything else earns no link.
RESETS_AT_SLACK_SECS = 60 * 60
                             # How far past now + its own length a window may
                             # claim to reset. A real window cannot reset later
                             # than that; the hour covers clock skew between
                             # the API and this machine. A snapshot claiming
                             # more is refused whole: nothing later ever ranks
                             # above it, so one such tick froze the usage line.
RATE_LIMITS_MAX_AGE_SECS = 15 * 60
                             # Sessions drop at 15 min and hosts at 1 h, but
                             # the budget figure had no expiry at all — and it
                             # was not merely frozen, it animated, because pace
                             # recomputes elapsed against a live clock. A stale
                             # "58% used" therefore drifted from "21% behind"
                             # to "42% behind" and eventually showed a reset
                             # time in the past. Absent is honest; stale
                             # presented as current is not.


def _window_order(window):
    """How advanced a usage-window snapshot is, or None if it cannot be read.

    `resets_at` identifies the window: a later one is a later window. Within a
    single window usage only ever increases, so the higher percentage is the
    later observation. Ordering by the pair therefore ranks two snapshots of
    the same account without needing either to say when it was taken — which
    matters, because neither does.
    """
    if not isinstance(window, dict):
        return None
    resets_at = window.get("resets_at")
    used = window.get("used_percentage")
    if not _is_number(resets_at) or not _is_number(used):
        return None
    return (resets_at, used)


def _supersedes(candidate, stored):
    """Is `candidate` at least as advanced as what is already stored?

    Equal snapshots supersede: an unchanging figure re-sent by an active
    session is the same claim confirmed again, and refreshing `as_of` for it is
    honest. Only a strictly *earlier* snapshot is rejected, and rejecting it is
    what stops one idle session dragging the page back a day every few seconds.
    """
    if stored is None:
        return True
    for name in ("five_hour", "seven_day"):
        new = _window_order(candidate.get(name))
        old = _window_order(stored.get(name))
        if new is None or old is None:
            # Nothing to compare on this window; let the others decide rather
            # than discarding a report over a field one side did not send.
            continue
        if new < old:
            return False
    return True


class _Implausible(Exception):
    """A rate_limits snapshot that cannot be a real one."""


def _clean_window(window, window_secs, now):
    """The two figures the page uses from one usage window, checked.

    Returns None for an absent window and raises _Implausible for one that
    sends a value that is not a finite number, or a reset beyond
    now + window_secs + RESETS_AT_SLACK_SECS. Refusing the whole snapshot,
    rather than blanking the bad field, matters: a window with a blank field
    is skipped by _supersedes, so a half-blank snapshot would replace a good
    one.
    """
    if window is None:
        return None
    if not isinstance(window, dict):
        raise _Implausible()
    cleaned = {}
    for name in ("used_percentage", "resets_at"):
        value = window.get(name)
        if value is not None and not _is_number(value):
            raise _Implausible()
        cleaned[name] = value
    resets_at = cleaned["resets_at"]
    if resets_at is not None and resets_at > now + window_secs + RESETS_AT_SLACK_SECS:
        raise _Implausible()
    return cleaned


def _number(value):
    """value if it is a finite number, else None."""
    return value if _is_number(value) else None


def _dig(mapping, *path):
    """Fetch a nested key, returning None rather than raising."""
    current = mapping
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def local_place(environ=os.environ):
    """What the collector's own machine is, or None when it has not said.

    STATUSGUMBO_PLACE, else the ~/.config/statusgumbo/place file the reporter
    also reads -- a file, because a unit or a status line inherits no shell
    rc. A value that is not one plain word is ignored, as on the wire.
    """
    value = environ.get("STATUSGUMBO_PLACE", "").strip()
    if not value:
        base = environ.get("XDG_CONFIG_HOME") or os.path.join(
            environ.get("HOME", ""), ".config"
        )
        try:
            with open(os.path.join(base, "statusgumbo", "place")) as handle:
                value = handle.read().strip()
        except OSError:
            return None
    return value if PLACE.fullmatch(value) else None


def local_host_name():
    """The name this machine reports itself under, derived as report.sh does.

    This must agree with `report.sh` character for character. That line is
    `${STATUSGUMBO_HOST:-$(hostname -s 2>/dev/null || echo unknown)}`, and if
    the two ever disagree the page grows two headings for one machine -- the
    same split the README warns about for STATUSGUMBO_HOST, arrived at from
    the other side. socket.gethostname() can return an FQDN where `hostname -s`
    returns the leaf, so the first label is what is taken.
    """
    name = os.environ.get("STATUSGUMBO_HOST")
    if name:
        return name
    try:
        return socket.gethostname().split(".")[0] or "unknown"
    except OSError:
        return "unknown"


class SessionStore:
    """Not thread-safe by accident — thread-safe on purpose.

    main() builds one ThreadingHTTPServer per listening address over one
    instance of this (by default loopback plus the tailnet address). All use
    daemon threads,
    so ingest() and snapshot() genuinely run concurrently. One lock held
    across both bodies is enough at this scale; finer granularity would buy
    nothing measurable and cost the ability to reason about this file.
    """

    def __init__(self, local_host=None, local_place=None):
        # The one host this collector does not have to infer from ticks. Every
        # other name on the page arrived in a payload; this one is the machine
        # the process is running on, so its existence is not a reading of the
        # data and cannot expire with the data. None keeps the old behaviour
        # for callers that do not care -- the tests, mostly.
        self.local_host = local_host
        # What the collector's own machine is, from its own STATUSGUMBO_PLACE.
        # When set it wins over what any tick says about that machine (see
        # _place), and it labels the machine before it has ever reported.
        # None means unlabelled, or whatever that machine's reporter says: the
        # collector's host used to be "laptop" by deployment, which on
        # anyone else's server would be a guess.
        self.local_place = local_place
        self.sessions = {}
        self.hosts = {}
        self.rate_limits = None
        self._lock = threading.Lock()

    def ingest(self, envelope, now):
        """Record one status-line tick. Raises ValueError if unusable."""
        with self._lock:
            self._ingest_locked(envelope, now)

    def _ingest_locked(self, envelope, now):
        if not isinstance(envelope, dict):
            raise ValueError("envelope must be an object")
        host = envelope.get("host")
        payload = envelope.get("payload")
        if not isinstance(host, str) or not host:
            raise ValueError("host must be a non-empty string")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("payload.session_id must be a non-empty string")

        current_dir = _dig(payload, "workspace", "current_dir")

        # Normalised here, not at the sort that consumes it: ctx_pct is
        # arithmetic'd in snapshot() (-(ctx_pct or 0) for the ordering key),
        # and _dig only guarantees the intermediate nodes are dicts -- the
        # leaf is whatever the wire sent. A string sailed straight into the
        # store, snapshot()'s sort blew up on every request thereafter (not
        # just for this session -- one store, one page), and it only healed
        # when the session aged out at DROP_SESSION_SECS. Storing None for
        # anything that is not a real number keeps the bad value out of the
        # store entirely, so every downstream consumer -- the sort, the
        # history append, the page's "--" render -- can keep treating None
        # as the one and only "we do not have this" case, the same fix
        # already applied to rate_limits.used_percentage via this same
        # _is_number helper. bool is excluded on purpose: it is an int
        # subclass, and True would arithmetic as 1%, inventing a confident
        # figure out of what is really a type error.
        raw_ctx_pct = _dig(payload, "context_window", "used_percentage")
        ctx_pct = raw_ctx_pct if _is_number(raw_ctx_pct) else None

        # When the session began, for newest-first ordering. The payload has
        # no start time, but cost.total_duration_ms is wall-clock time since
        # the session started (measured 2026-10-03: tick time minus it gave
        # the same second ten idle minutes apart, and each session after a
        # /clear started where the previous one's last tick ended). Unlike
        # first_seen it survives a collector restart, which would otherwise
        # stamp every session with the same moment.
        raw_duration = _dig(payload, "cost", "total_duration_ms")
        started_at = (
            now - raw_duration / 1000
            if _is_number(raw_duration) and raw_duration >= 0
            else None
        )

        # _dig guarantees the *intermediate* nodes are dicts. It guarantees
        # nothing about the leaf, and os.path.basename raises TypeError on
        # any truthy non-string — so the isinstance check is the guard here,
        # not the truthiness one it replaces.
        project = (
            os.path.basename(current_dir)
            if isinstance(current_dir, str) and current_dir
            else None
        )

        # Everything that can fail is computed before self.sessions is
        # touched, and nothing between the insert below and the update can
        # raise. That ordering is the fix: previously the record went into
        # the map first, so a TypeError from basename() left a record with no
        # `last_seen`, every later snapshot() died with KeyError, and no
        # subsequent valid tick healed it. Restart=always could not recover
        # it either — the process was fine, only the store was wedged, and
        # the page's sole symptom was a false "collector unreachable".
        # A Remote Control session's claude.ai page, from the bridge id
        # report.sh read out of the transcript. The link is built here from a
        # validated id rather than taken from the wire, so the page never
        # puts a URL it was sent into an href. The `session_` form is the
        # one Claude Code itself prints for the same session.
        bridge = envelope.get("bridge")
        url = (
            "https://claude.ai/code/session_" + bridge[len("cse_"):]
            if isinstance(bridge, str) and BRIDGE_ID.fullmatch(bridge)
            else None
        )

        fields = {
            "host": host,
            "url": url,
            "session_id": session_id,
            "session_name": payload.get("session_name"),
            "project": project,
            "branch": envelope.get("branch") or None,
            "worktree": _dig(payload, "workspace", "git_worktree"),
            "model": _dig(payload, "model", "display_name"),
            "effort": _dig(payload, "effort", "level"),
            "fast_mode": payload.get("fast_mode"),
            "version": payload.get("version"),
            "ctx_pct": ctx_pct,
            # Numbers the page does arithmetic on are kept only when finite;
            # an infinity here would reach json.dumps as a bare Infinity,
            # which the page cannot parse.
            "ctx_used_tokens": _number(
                _dig(payload, "context_window", "total_input_tokens")
            ),
            "ctx_window_size": _number(
                _dig(payload, "context_window", "context_window_size")
            ),
            "cost_usd": _number(_dig(payload, "cost", "total_cost_usd")),
            "last_seen": now,
        }

        key = (host, session_id)
        record = self.sessions.get(key)
        if record is None:
            # first_seen and history belong to the session, not the tick, so
            # they are set once here and never carried in `fields`.
            record = {
                "first_seen": now,
                "started_at": None,
                "history": deque(maxlen=HISTORY_SLOTS),
            }
            self.sessions[key] = record
        record.update(fields)
        # Kept from the first tick that carries it, not recomputed: each
        # tick's figure jitters by network latency, and two sessions started
        # within a second of each other must not swap places on a refresh.
        if record["started_at"] is None:
            record["started_at"] = started_at

        if ctx_pct is not None:
            history = record["history"]
            if (
                not history
                or now - history[-1]["t"] >= HISTORY_MIN_INTERVAL_SECS
            ):
                history.append({"t": now, "ctx_pct": ctx_pct})

        rate_limits = payload.get("rate_limits")
        if isinstance(rate_limits, dict) and rate_limits:
            # Account-global, so stored once. Storing per session would let
            # two hosts disagree on screen for no reason.
            #
            # Last-write-wins was wrong, and visibly so: a payload carries the
            # snapshot its session last received from the API, not a reading
            # taken at render time, so an idle session re-publishes a stale one
            # every 10 seconds indefinitely. The page flickered between a
            # current figure and one whose window had reset the day before,
            # each stamped "as of" now.
            try:
                candidate = {
                    "five_hour": _clean_window(
                        rate_limits.get("five_hour"), FIVE_HOUR_SECS, now
                    ),
                    "seven_day": _clean_window(
                        rate_limits.get("seven_day"), SEVEN_DAY_SECS, now
                    ),
                    "source_host": host,
                    "as_of": now,
                }
            except _Implausible:
                # The tick's session is still recorded; only its usage
                # figures are not believed.
                candidate = None
            if candidate is not None and _supersedes(candidate, self.rate_limits):
                self.rate_limits = candidate

        # Facts only. Silence is ambiguous here by nature — a cleanly ended
        # session and a dead tunnel look identical from this stream — so the
        # store records when a host last spoke and infers nothing from it.
        # Tunnel state is answered by the server asking systemd.
        #
        # `where` is the reporter's own statement of what it runs on, kept only
        # when it is one plain word (PLACE): anything else is a word no one
        # can vouch for, and the page would print it as a label. The last
        # stated value sticks for the life of the record: a session started
        # outside the Coder agent's environment (a stray tmux server, say)
        # sends no `where`, and letting that clear it would make the label
        # flicker between ticks of two sessions on the same machine. A new
        # stated value replaces it.
        where = envelope.get("where")
        if not (isinstance(where, str) and PLACE.fullmatch(where)):
            where = None
        previous = self.hosts.get(host)
        self.hosts[host] = {
            "host": host,
            "last_seen": now,
            "where": where or (previous["where"] if previous else None),
        }

    def snapshot(self, now):
        """Prune expired entries and return the view the API serves."""
        with self._lock:
            return self._snapshot_locked(now)

    def _snapshot_locked(self, now):
        for key in [
            key
            for key, record in self.sessions.items()
            if now - record["last_seen"] > DROP_SESSION_SECS
        ]:
            del self.sessions[key]
        for host in [
            host
            for host, record in self.hosts.items()
            # The local host keeps its record past DROP_HOST_SECS so its last
            # tick stays datable. Dropping it would leave the heading below
            # with nothing to say but "no ticks yet", which would be false
            # about a machine that reported all morning.
            if now - record["last_seen"] > DROP_HOST_SECS and host != self.local_host
        ]:
            del self.hosts[host]

        # Only live sessions are served. The record above survives until
        # DROP_SESSION_SECS either way -- what ends at ACTIVE_SECS is its
        # visibility, not its existence.
        #
        # There is no "state" field any more. It carried "active" or "stale",
        # and with nothing stale on the wire it could only ever say "active":
        # a constant dressed as an observation.
        sessions = []
        for record in self.sessions.values():
            age = now - record["last_seen"]
            if age > ACTIVE_SECS:
                continue
            item = {k: v for k, v in record.items() if k != "history"}
            item["age_secs"] = age
            item["history"] = list(record["history"])
            sessions.append(item)
        # Grouped by host, newest session first within the host. The page
        # draws one heading per host and takes that host's sessions in the
        # order they appear here, so this is where the within-group ordering
        # is decided.
        #
        # This replaces a -ctx_pct ordering, and losing it is the point. That
        # key put the busiest session on top, but it also moved every card
        # every time any session's context changed — which on a phone is
        # constantly. A card you had just found slid out from under your
        # thumb, and two cards could swap places between one 5s refresh and
        # the next. Position is what makes a card findable a second time, so
        # the order has to come from things that do not change while you are
        # looking: host, then when the session started. A new session lands
        # on top and pushes the rest down one; nothing else moves a card.
        # (Alphabetical by project came between the two, and was replaced at
        # the user's request on 2026-10-03: the session just started is the
        # one most often looked for.)
        #
        # A session with no started_at (an older Claude Code, or a payload
        # without cost) falls back to first_seen, which is also fixed for the
        # life of the record. session_id breaks the remaining tie so the
        # order is total; without it, equal keys would come out in whatever
        # order dict iteration last gave — stable in practice, guaranteed by
        # nothing.
        #
        # casefold() rather than lower(): hostnames are not all ASCII.
        sessions.sort(
            key=lambda s: (
                s["host"].casefold(),
                -(s["started_at"] if s["started_at"] is not None
                  else s["first_seen"]),
                s["session_id"],
            )
        )

        hosts = [
            {
                "host": record["host"],
                "last_seen": record["last_seen"],
                "age_secs": now - record["last_seen"],
                "place": self._place(record["host"], record["where"]),
            }
            for record in self.hosts.values()
        ]

        # The machine serving this page is always on it.
        #
        # Observed 2026-09-16: the page was fetched from the collector's own
        # machine, and that machine was not on it. It had stopped reporting, the
        # record aged out at DROP_HOST_SECS, and the host vanished -- so the
        # page's set of machines quietly became "machines heard from lately"
        # while still reading as "your machines". An absent heading is not a
        # quieter version of "not reporting"; it is the page declining to
        # mention a machine at all, which is the omission this project exists
        # to prevent. A collector restart does the same thing faster: this
        # store is in memory, so every host record goes with it.
        #
        # Bounded on purpose. This adds exactly one name, the one the process
        # can vouch for locally, and never accumulates the way an unbounded
        # DROP_HOST_SECS would -- a workspace deleted last month must still be
        # allowed to leave.
        if self.local_host is not None and self.local_host not in self.hosts:
            hosts.append(
                {
                    "host": self.local_host,
                    "last_seen": None,
                    "age_secs": None,
                    "place": self._place(self.local_host, None),
                }
            )

        return {
            "now": now,
            "rate_limits": self._rate_limits_view(now),
            "rate_limits_stale_as_of": self._rate_limits_stale_as_of(now),
            "hosts": hosts,
            "sessions": sessions,
        }

    def _place(self, host, where):
        # What kind of machine a heading is, for the page's at-a-glance mark:
        # what the host said, else the collector's own place for its own
        # host. Any other host that said nothing is unlabelled -- a remote
        # machine is not assumed to be a VM.
        #
        # For its own host the collector's own setting wins when it has one.
        # It reads the same place file as that machine's reporter, so the two
        # agree, and any client that can post could otherwise relabel the
        # machine the collector runs on for the life of the process.
        if host == self.local_host and self.local_place:
            return self.local_place
        return where

    def _rate_limits_stale_as_of(self, now):
        # Called with self._lock already held.
        #
        # `rate_limits` is null for two unrelated reasons -- nothing has ever
        # reported one, or one was reported and is now too old to serve -- and
        # a null cannot say which. The page rendered both as "not reported
        # yet", so on a collector that had been serving usage all morning the
        # line claimed the morning had not happened.
        #
        # This carries only the *date* of the withheld figure, never the
        # figure. Which silence it is, is knowable and is now said; the stale
        # percentage stays withheld for the reasons in _rate_limits_view.
        if self.rate_limits is None:
            return None
        if now - self.rate_limits["as_of"] > RATE_LIMITS_MAX_AGE_SECS:
            return self.rate_limits["as_of"]
        return None

    def _rate_limits_view(self, now):
        # Called with self._lock already held.
        if self.rate_limits is None:
            return None
        if now - self.rate_limits["as_of"] > RATE_LIMITS_MAX_AGE_SECS:
            # Withheld rather than served: reporting it anyway would be a
            # confidently-stated, visibly-moving claim about current account
            # usage that is arbitrarily out of date. `rate_limits_stale_as_of`
            # is what tells the page this is a withheld figure rather than an
            # absent one -- keep the two branches in step.
            return None
        view = {
            "source_host": self.rate_limits["source_host"],
            "as_of": self.rate_limits["as_of"],
            "windows": [],
        }
        for name, window_secs in (
            ("five_hour", FIVE_HOUR_SECS),
            ("seven_day", SEVEN_DAY_SECS),
        ):
            window = self.rate_limits.get(name)
            if not isinstance(window, dict):
                continue
            used = window.get("used_percentage")
            resets_at = window.get("resets_at")
            # The window rolled over and nothing has reported since, so this
            # figure describes a window that no longer exists. Its pace is
            # worse than the figure: elapsed is measured against a live clock,
            # so it keeps moving. `—` is the honest answer.
            if _is_number(resets_at) and resets_at <= now:
                continue
            view["windows"].append(
                {
                    "name": name,
                    "used_percentage": used,
                    "resets_at": resets_at,
                    "pace": window_pace(used, resets_at, window_secs, now),
                }
            )
        return view
