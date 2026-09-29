"""Pace maths for the account-wide usage windows.

Done here, not in the page, so it can be unit-tested with an injected clock
and the browser page stays dumb. "Pace" is how far ahead or
behind linear burn you are: used% minus elapsed%, where elapsed is derived
from the reset time and the window length.
"""

FIVE_HOUR_SECS = 5 * 60 * 60
SEVEN_DAY_SECS = 7 * 24 * 60 * 60


def _is_number(value):
    """True for a value this module can safely do arithmetic on.

    bool is a subclass of int in Python and is excluded deliberately: True
    would arithmetic as 1% used, inventing a confident figure out of what is
    really a type error.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def window_pace(used_percentage, resets_at, window_secs, now):
    """Percentage points ahead (+) or behind (-) linear burn.

    Returns None when either input is missing or is not a number, which is
    normal: rate_limits is absent for non-Pro/Max accounts and before a
    session's first API response. Callers render that as an honest blank,
    never as 0%.

    The type check is not defensive padding. Ingest validates rate_limits
    only as far as `isinstance(dict)`, so a tick carrying
    {"five_hour": {"used_percentage": "58"}} was accepted with a 204 and
    then raised TypeError from here on every later snapshot — one bad tick
    taking down the whole page. None is already the established "cannot be
    computed" answer, so it is both the safe result and the correct one.

    window_secs is not checked: it only ever comes from the two module
    constants above.
    """
    if not _is_number(used_percentage) or not _is_number(resets_at):
        return None
    start = resets_at - window_secs
    elapsed = (now - start) / window_secs * 100.0
    elapsed = max(0.0, min(100.0, elapsed))
    return used_percentage - elapsed
