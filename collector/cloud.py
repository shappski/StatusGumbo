"""Cloud Claude Code sessions, polled from Anthropic rather than reported.

A cloud session runs in Anthropic's sandbox: it has no status line of ours,
cannot see this machine's ~/.claude, and cannot reach a collector that is
Tailscale-only by design. So instead of waiting for ticks, the collector asks
the service that runs those sessions.

The endpoint is undocumented. `GET /v1/code/sessions` is what Claude Code
itself calls (its `--teleport`, `--cloud` and routine run listings), found by
reading the 2.1.282 binary on 2026-09-25 and verified live the same day. The
user accepted building on it knowing it can change without notice, so every
field here is read defensively and a response that stops making sense turns
the section into an error line, never into an empty list that reads as "no
cloud sessions".

Sessions a routine started are missing from that list, so they come from two
more undocumented calls, found in the 2.1.288 binary and verified live on
2026-10-03: `GET /v1/code/triggers`, which answers only with the
`anthropic-beta: ccr-triggers-2026-01-30` header, and
`GET /v1/code/sessions?trigger_id=…`. Neither's paging or ordering is known.

Authentication is the claude.ai login Claude Code already keeps on disk. It
is read, never written: when Claude Code's access token expires the section
says so and waits for Claude Code to refresh it, rather than this process
running its own OAuth refresh and racing Claude Code for the same file.
"""

import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from collector.store import _dig
from collector.usage import _is_number

API_URL = "https://api.anthropic.com/v1/code/sessions"
POLL_SECS = 60               # One list call a minute. The phone refreshes every
                             # 5s, but a cloud session's state is not a 5s
                             # figure, and this is somebody else's API.
MAX_BACKOFF_SECS = 10 * 60   # Ceiling for the doubling after a failure or 429.
PAGE_SIZE = 100
MAX_PAGES = 3                # Paging stops at LOOKBACK_SECS; this is only the
                             # guard against a cursor that never ends.
LOOKBACK_SECS = 14 * 24 * 3600
                             # Results come newest-activity first, so once a
                             # page reaches sessions this old nothing further
                             # back is worth fetching. Observed 2026-09-25: five
                             # active cloud sessions, the oldest ten days quiet.
MAX_AGE_SECS = 5 * 60        # The same rule as RATE_LIMITS_MAX_AGE_SECS: past
                             # this the last good list is withheld, not served
                             # as though it were current.
EXPIRED_TEXT = "login expired — any local claude session refreshes it"
ROUTINE_UNKNOWN_TEXT = "routine sessions unknown: %s"
CLOUD_KIND = "anthropic_cloud"
                             # The list also carries every Remote Control
                             # ("bridge") session, including local ones this
                             # collector already shows from their own ticks —
                             # 95 of the first 100 entries on 2026-09-25.
TRIGGERS_URL = "https://api.anthropic.com/v1/code/triggers"
TRIGGERS_BETA = "ccr-triggers-2026-01-30"
                             # A session a routine started is left out of the
                             # plain list (2026-10-03: all 385 entries paged,
                             # none of the 13 routine sessions among them), so
                             # it is found from its trigger's last_run. The
                             # path and beta header are the ones in the 2.1.288
                             # binary; without the header the path is a 404.


def credentials_path():
    return os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")


def account_path():
    return os.path.join(os.path.expanduser("~"), ".claude.json")


def _epoch(text):
    """An ISO-8601 timestamp as epoch seconds, or None if it is not one."""
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class ApiError(Exception):
    """The API answered with a status that is not a session list."""


class LoginUnavailable(Exception):
    """No usable claude.ai login on disk. `state` is what the page says."""

    def __init__(self, state, detail):
        super().__init__(detail)
        self.state = state
        self.detail = detail


def read_login(now, creds=None, account=None):
    """(access token, organization uuid) from Claude Code's own files.

    Raises LoginUnavailable rather than returning a token that is known to
    be expired: calling with it would only earn a 401 and teach nothing the
    expiry field did not already say.
    """
    try:
        with open(creds or credentials_path()) as handle:
            oauth = json.load(handle).get("claudeAiOauth")
        with open(account or account_path()) as handle:
            org = _dig(json.load(handle), "oauthAccount", "organizationUuid")
    except (OSError, ValueError, AttributeError):
        raise LoginUnavailable("no_login", "no claude.ai login found")
    token = _dig(oauth, "accessToken")
    if not isinstance(token, str) or not token or not isinstance(org, str) or not org:
        raise LoginUnavailable("no_login", "no claude.ai login found")
    expires_ms = _dig(oauth, "expiresAt")
    if _is_number(expires_ms) and expires_ms / 1000 <= now:
        raise LoginUnavailable("login_expired", EXPIRED_TEXT)
    return token, org


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib re-sends every header, Authorization included, to wherever a
    # redirect points. The API never redirects, so one is answered as the
    # status it is rather than followed with the login attached.
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
# What a session id must look like before it goes into a claude.ai link.
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def http_get(url, headers, timeout=15):
    """(status, body bytes). Non-2xx is a status, not an exception."""
    request = urllib.request.Request(url, headers=headers)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()
    except (urllib.error.URLError, OSError) as err:
        # The laptop off the network is routine, not a surprise worth a trace.
        raise ApiError("unreachable: %s" % getattr(err, "reason", err))


def parse_session(raw):
    """One card's worth of fields, or None if this entry is not shown.

    Shown means: a cloud session, not archived. Everything else in the raw
    record — usage.cost_usd included — is left behind here, so the page
    cannot display what it never receives.
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("environment_kind") != CLOUD_KIND or raw.get("status") == "archived":
        return None
    session_id = raw.get("id")
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        return None
    meta = raw.get("external_metadata")
    meta = meta if isinstance(meta, dict) else {}

    used = _dig(meta, "context_usage", "used_tokens")
    size = _dig(meta, "context_usage", "max_tokens")
    used = used if _is_number(used) else None
    size = size if _is_number(size) and size > 0 else None
    ctx_pct = used * 100.0 / size if used is not None and size is not None else None
    # Finite inputs can still overflow here, and an infinity would reach the
    # page as a bare Infinity it cannot parse.
    if not _is_number(ctx_pct):
        ctx_pct = None

    repo_url = None
    sources = _dig(raw, "config", "sources")
    if isinstance(sources, list) and sources and isinstance(sources[0], dict):
        repo_url = sources[0].get("url")
    repo = (
        repo_url.rstrip("/").rsplit("/", 1)[-1]
        if isinstance(repo_url, str) and repo_url
        else None
    )

    # Keyed by worktree path; the main checkout is "". Only a single branch
    # is a statement about the session — several are listed, not chosen from.
    branches = meta.get("current_branches")
    branch = None
    if isinstance(branches, dict):
        names = [b for b in branches.values() if isinstance(b, str) and b]
        branch = ", ".join(names) or None

    summary = meta.get("post_turn_summary")
    summary = summary if isinstance(summary, dict) else {}

    def text(value):
        return value if isinstance(value, str) and value else None

    return {
        "id": session_id,
        "title": text(raw.get("title")),
        # working / blocked / review_ready / completed as observed. Passed
        # through unmapped: the page phrases the four it knows and shows any
        # other value verbatim rather than guessing what it means.
        "bucket": text(raw.get("status_bucket")),
        "needs_action": text(summary.get("needs_action")),
        "status_detail": text(summary.get("status_detail")),
        "repo": repo,
        "branch": branch,
        "model": text(meta.get("last_served_model")) or text(_dig(raw, "config", "model")),
        "ctx_pct": ctx_pct,
        "ctx_used_tokens": used,
        "ctx_window_size": size,
        "created_at": _epoch(raw.get("created_at")),
        "last_event_at": _epoch(raw.get("last_event_at")) or _epoch(raw.get("updated_at")),
        "url": "https://claude.ai/code/" + session_id,
    }


class _Expired(Exception):
    """A 401: the token on disk was refused."""


def _get_page(get, url, headers, noun):
    """(page dict, its data list), or raises. `noun` names the list in errors."""
    status, body = get(url, headers)
    if status == 401:
        raise _Expired()
    if status != 200:
        raise ApiError("HTTP %d" % status)
    page = json.loads(body)
    data = page.get("data") if isinstance(page, dict) else None
    # The one shape check that matters: without a list here, "no
    # sessions" would be a guess, so it is an error instead.
    if not isinstance(data, list):
        raise ValueError("response has no %s list" % noun)
    return page, data


def _routine_sessions(headers, now, skip, archived, get):
    """Shown sessions started by a routine, from each trigger's last run.

    Sessions in `skip` or `archived` are not asked for; an archived one
    found here is added to `archived`, since archiving is for good.
    """
    _, triggers = _get_page(get, TRIGGERS_URL, dict(headers, **{"anthropic-beta": TRIGGERS_BETA}), "trigger")
    sessions = []
    for trigger in triggers:
        if not isinstance(trigger, dict):
            continue
        trigger_id = trigger.get("id")
        session_id = _dig(trigger, "last_run", "session_id")
        fired = _epoch(trigger.get("last_fired_at"))
        if not (isinstance(trigger_id, str) and trigger_id and isinstance(session_id, str) and session_id):
            continue
        if session_id in skip or session_id in archived or fired is None or now - fired > LOOKBACK_SECS:
            continue
        query = urllib.parse.urlencode({"trigger_id": trigger_id, "limit": PAGE_SIZE})
        _, data = _get_page(get, API_URL + "?" + query, headers, "session")
        for raw in data:
            if isinstance(raw, dict) and raw.get("id") == session_id:
                if raw.get("status") == "archived":
                    archived.add(session_id)
                else:
                    parsed = parse_session(raw)
                    if parsed is not None:
                        sessions.append(parsed)
                break
    return sessions


def fetch_sessions(token, org, now, get=http_get, archived=None):
    """Every shown cloud session, or raises. Returns (sessions, state,
    routine_detail).

    state is "ok", or "login_expired" for a 401 — the file said the token
    was good and the server disagreed, which is still a login problem and
    not a fault of the page.

    routine_detail is None, or the words for why routine sessions are
    missing from an otherwise good list. The routine side rests on a beta
    endpoint the plain list does not need, so its failure is said beside the
    plain cards instead of taking them down with it.

    `archived` is the caller's set of routine sessions already seen archived,
    kept across polls so they are not asked for again; it is added to here.
    """
    headers = {
        "Authorization": "Bearer " + token,
        "x-organization-uuid": org,
        "anthropic-version": "2023-06-01",
        "Accept": "application/json",
    }
    archived = set() if archived is None else archived
    sessions = []
    cursor = None
    try:
        for _ in range(MAX_PAGES):
            query = {"limit": PAGE_SIZE}
            if cursor:
                query["cursor"] = cursor
            page, data = _get_page(get, API_URL + "?" + urllib.parse.urlencode(query), headers, "session")
            for raw in data:
                parsed = parse_session(raw)
                if parsed is not None:
                    sessions.append(parsed)
            cursor = page.get("next_cursor")
            oldest = _epoch(data[-1].get("last_event_at")) if data and isinstance(data[-1], dict) else None
            if not cursor or oldest is None or now - oldest > LOOKBACK_SECS:
                break
        listed = {s["id"] for s in sessions}
        routine_detail = None
        try:
            sessions.extend(_routine_sessions(headers, now, listed, archived, get))
        except _Expired:
            # The plain list just took this token, so a 401 here is the beta
            # or its scope refused, not a lapsed login: saying "login
            # expired" would send the reader to refresh a login that works.
            print("cloud poll: routines: HTTP 401", file=sys.stderr)
            routine_detail = ROUTINE_UNKNOWN_TEXT % "HTTP 401"
        except ApiError as err:
            print("cloud poll: routines: %s" % err, file=sys.stderr)
            routine_detail = ROUTINE_UNKNOWN_TEXT % err
        except Exception as err:
            # As broad as poll_once's catch, for the same undocumented schema.
            traceback.print_exc(file=sys.stderr)
            routine_detail = ROUTINE_UNKNOWN_TEXT % "unexpected response (see the collector's log)"
    except _Expired:
        return None, "login_expired", None
    # Position must not move while the page is read, so the order comes from
    # when a session was created, which never changes — not from its activity.
    # Newest first, like the local hosts; one with no creation time goes last.
    sessions.sort(key=lambda s: (
        s["created_at"] is None, -(s["created_at"] or 0), s["id"],
    ))
    return sessions, "ok", routine_detail


class CloudPoller:
    """Background poll, one lock, the same shape as SessionStore.

    view() answers what the page may say right now. The last good list is
    kept across a failure so a single bad minute does not blank the section,
    but only for MAX_AGE_SECS; after that its date is said and it is not.
    """

    def __init__(self, clock=time.time, get=http_get, login=read_login):
        self._clock = clock
        self._get = get
        self._login = login
        self._lock = threading.Lock()
        self.sessions = None
        self.as_of = None
        self.state = "starting"
        self.detail = None
        self.routine_detail = None   # belongs to self.sessions, set with it
        self.failures = 0
        self._archived = set()   # routine sessions seen archived; poll thread only
        self._ctx_seen = {}      # id -> (used_tokens, as_of, exact); poll thread only

    def poll_once(self):
        """One poll. Returns the seconds to wait before the next."""
        now = self._clock()
        try:
            token, org = self._login(now)
            sessions, state, routine_detail = fetch_sessions(token, org, now, self._get, self._archived)
        except LoginUnavailable as err:
            sessions, state, detail = None, err.state, err.detail
        except ApiError as err:
            # Expected now and then (a 429, a 5xx), so one line, not a trace.
            print("cloud poll: %s" % err, file=sys.stderr)
            sessions, state, detail = None, "error", str(err)
        except Exception as err:
            # Deliberately broad, for the reason /ingest is: the schema is
            # somebody else's and undocumented. A surprise costs this section
            # an error line, never the collector.
            # The page gets fixed text; the exception's own words, which can
            # carry whatever the response held, go only to the journal.
            traceback.print_exc(file=sys.stderr)
            sessions, state, detail = None, "error", "unexpected response (see the collector's log)"
        else:
            detail = None if state == "ok" else EXPIRED_TEXT
        with self._lock:
            self.state, self.detail = state, detail
            if state == "ok":
                sessions = self._date_context(sessions, now)
                self.sessions, self.as_of, self.failures = sessions, now, 0
                self.routine_detail = routine_detail
                return POLL_SECS
            if state == "error":
                self.failures += 1
                return min(POLL_SECS * 2 ** self.failures, MAX_BACKOFF_SECS)
            # A login problem is not the API failing, and backing off from it
            # would only delay noticing that Claude Code has refreshed it.
            return POLL_SECS

    def _date_context(self, sessions, now):
        """Each session's context figure, dated by the poll that first saw it.

        The API's context_usage carries no time of its own, and it lags: on
        2026-09-30 a session's figure held for 20+ minutes after a /clear
        while last_event_at kept advancing. Undated, that figure reads as
        current. A poll that sees a new value knows it appeared within the
        last poll interval, so it is dated exactly. A value seen for the
        first time, or after a gap in polling, may be older than the poll
        that found it, and is marked so the page says "or earlier".
        """
        previous = self.as_of
        steady = previous is not None and now - previous <= 2 * POLL_SECS
        seen, dated = {}, []
        for s in sessions:
            used = s["ctx_used_tokens"]
            before = self._ctx_seen.get(s["id"])
            if used is None:
                as_of, exact = None, False
            elif before is not None and before[0] == used:
                as_of, exact = before[1], before[2]
            else:
                created = s["created_at"]
                new_session = before is None and created is not None and previous is not None and created > previous
                as_of, exact = now, steady and (before is not None or new_session)
            seen[s["id"]] = (used, as_of, exact)
            dated.append(dict(s, ctx_as_of=as_of, ctx_as_of_exact=exact))
        self._ctx_seen = seen
        return dated

    def view(self, now):
        with self._lock:
            fresh = self.as_of is not None and now - self.as_of <= MAX_AGE_SECS
            sessions = []
            if fresh:
                for s in self.sessions:
                    item = dict(s)
                    last = s["last_event_at"]
                    item["age_secs"] = now - last if last is not None else None
                    sessions.append(item)
            return {
                "state": self.state,
                "detail": self.detail,
                "as_of": self.as_of if fresh else None,
                # Which silence it is, as for rate limits: a list withheld as
                # too old is dated, one never fetched is not.
                "stale_as_of": self.as_of if self.as_of is not None and not fresh else None,
                "sessions": sessions if fresh else None,
                "routine_detail": self.routine_detail if fresh else None,
            }

    def run_forever(self):
        while True:
            time.sleep(self.poll_once())
