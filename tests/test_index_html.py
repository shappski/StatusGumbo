"""Source-level guards on the viewer page.

There is no JavaScript runtime this suite may use. The design records that
there is no system Node (the only `node` on PATH belongs to an unrelated
work repo, and depending on it would make this suite unrunnable anywhere
else), and the project is Python 3 standard library only. So these are not
execution tests: each one pins the *shape* of a fix whose behaviour was
verified by reading, and each fails against the code as it stood before this
wave. They exist to stop the exact defects below being reintroduced, not to
prove the page renders.

Every assertion here is scoped to a single function's source so a match
elsewhere in the file cannot make one vacuously pass.
"""

import os
import re
import unittest

INDEX_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "collector",
    "index.html",
)

with open(INDEX_PATH, encoding="utf-8") as _handle:
    SOURCE = _handle.read()


def function_source(name):
    """The *code* of one top-level function in index.html, comments removed.

    Functions there are declared at column zero and closed by a `}` at
    column zero, so this slice is exact rather than approximate.

    Comment lines are stripped because otherwise every assertion here is
    unreliable in both directions: an assertIn could pass on prose that
    merely mentions the construct, and an assertNotIn could fail on a
    comment explaining why the construct was avoided. Only whole-line `//`
    comments are dropped, so a `//` inside a string literal is safe.
    """
    marker = "function %s(" % name
    start = SOURCE.index(marker)
    end = SOURCE.index("\n}\n", start)
    body = SOURCE[start:end]
    return "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("//")
    )


class TestFunctionSourceHelper(unittest.TestCase):
    """The helper is load-bearing: if it silently returned the whole file,
    every assertNotIn below would become vacuous."""

    def test_it_slices_one_function_not_the_file(self):
        body = function_source("tone")
        self.assertIn("green", body)
        self.assertNotIn("renderSession", body)
        self.assertLess(len(body), len(SOURCE) / 4)

    def test_a_missing_function_is_an_error_not_an_empty_string(self):
        with self.assertRaises(ValueError):
            function_source("noSuchFunction")

    def test_comments_are_stripped_so_no_assertion_matches_prose(self):
        # plottable's comments name `typeof` precisely to explain why the
        # code does not use it. Without stripping, that prose would defeat
        # the assertion that the code is free of it.
        body = function_source("plottable")
        self.assertNotIn("//", body)
        self.assertIn("Number.isFinite", body)
        self.assertNotIn("typeof", body)

    def test_code_survives_the_stripping(self):
        self.assertIn("getPropertyValue", function_source("toneColor"))


class TestThePageSaysOnlyWhatIsKnown(unittest.TestCase):
    """2026-09-16. Two lines stated things the collector cannot establish.

    The quiet-host line said "no sessions". What reaches the collector are
    ticks, not sessions, so a session whose status line has stopped firing
    and no session at all are indistinguishable — both send nothing. Read
    from a phone with four live sessions across two hosts, the line was
    false, which is the failure `index.html` itself calls its worst.

    The budget line said "not reported yet" for a figure that had been
    reported and was merely being withheld as stale.
    """

    def test_a_quiet_host_is_not_called_empty(self):
        body = function_source("renderHostGroups")
        self.assertNotIn("no sessions", body)
        self.assertIn("not reporting", body)

    def test_the_quiet_host_line_still_dates_itself(self):
        # The one fact there is. A clock, not a relative age: the 2026-07-31
        # design keeps this line still while it is being read, and `age()`
        # would tick upward under the reader's eye.
        body = function_source("renderHostGroups")
        self.assertIn("last tick", body)
        self.assertIn("lastSeen.get(host)", body)
        self.assertIn("clock(seen)", body)
        self.assertNotIn("age(", body)

    def test_a_host_that_never_ticked_says_so_in_words(self):
        # Reachable only since the collector started listing its own host
        # whether or not it has reported. clock(null) renders "—", which is a
        # dash standing in for a sentence.
        body = function_source("renderHostGroups")
        self.assertIn("no ticks yet", body)
        self.assertIn("num(seen) !== null", body)

    def test_the_budget_card_separates_never_from_withheld(self):
        body = function_source("renderBudget")
        self.assertIn("not reported yet", body)
        self.assertIn("nothing since", body)
        # Both must be reachable: a branch on the collector's own marker is
        # what tells them apart, and hardcoding either sentence would make
        # this class's assertions pass while the page went on guessing.
        self.assertIn("staleAsOf", body)

    def test_the_marker_is_threaded_from_the_payload(self):
        # renderBudget cannot branch on what render() never passes it. A
        # pure-function guard on the branch alone would hold while the call
        # site stayed unwired.
        self.assertIn(
            "renderBudget(data.rate_limits, data.rate_limits_stale_as_of)",
            function_source("render"),
        )


class TestHostsAreNotContradicted(unittest.TestCase):
    """FINDING 5. The host group's "no sessions" line computed its live set
    from sessions with state === 'active' only, so a host whose sessions had
    all gone quiet rendered "no sessions" directly above a greyed card for one
    of its own sessions.

    The collector now serves live sessions only, so the contradiction has no
    material to work with. These still hold: they forbid the page from
    re-deriving liveness itself, which is what would bring the bug back.
    """

    def test_the_live_set_is_not_restricted_to_active_sessions(self):
        body = function_source("renderHostGroups")
        self.assertNotIn("'active'", body)

    def test_it_is_still_derived_from_the_session_list(self):
        body = function_source("renderHostGroups")
        self.assertIn("sessions", body)
        self.assertIn(".host", body)

    def test_no_session_the_collector_serves_can_be_dropped_by_the_page(self):
        # The page groups by host, so it needs a list of hosts to draw
        # headings for. Taking that list from `hosts` alone would silently
        # discard any session whose host record had been pruned -- a running
        # session, still in the payload, invisible while you look at it. The
        # union is what forbids that.
        body = function_source("hostNames")
        self.assertIn("hosts", body)
        self.assertIn("sessions", body)


class TestCardsReadAsSeparateBoxes(unittest.TestCase):
    """Seven cards on a phone read as one wall of text. Three boundary cues
    were switched off at once: the gutter between cards (8px) sat inside the
    range of a card's own internal gaps (4-7px), the fill was 1.10:1 against
    the page, and the border was 1.23:1 against the fill.

    These pin the relationships rather than the hex values, so a future
    palette change has to keep the reasoning rather than merely keep the
    colours.
    """

    def test_the_gutter_between_cards_exceeds_every_gap_inside_one(self):
        gutter = int(
            re.search(r"\.card \{.*?margin-bottom: (\d+)px", SOURCE, re.S).group(1)
        )
        # Every internal gap declared in the card's own rules.
        internal = [
            int(m)
            for m in re.findall(
                r"\.(?:meta|bar|row) \{[^}]*?margin[^:]*: ([\d ]+?)px", SOURCE
            )
        ]
        internal += [
            int(m) for m in re.findall(r"\.spark \{[^}]*?margin-top: (\d+)px", SOURCE)
        ]
        self.assertTrue(internal, "found no internal gaps to compare against")
        self.assertGreaterEqual(gutter, 2 * max(internal))

    def test_the_card_outline_does_not_share_the_variable_with_the_bar_track(self):
        # --line is the unfilled track of the context bar. Raising it to give
        # the card a visible edge would have brightened the empty half of
        # every progress bar, making a 20% bar read as fuller than 20% -- a
        # number overstated by styling, on the page whose one rule is that it
        # never overstates a number.
        card_rule = re.search(r"\.card \{.*?\}", SOURCE, re.S).group(0)
        self.assertIn("var(--edge)", card_rule)
        self.assertNotIn("var(--line)", card_rule)
        self.assertIn("background: var(--line)", SOURCE)  # the bar track

    def test_no_card_is_dimmed_for_having_gone_quiet(self):
        # This replaces a test that pinned *how* a stale card was dimmed: the
        # rule had to sit on the children, because dimming the card faded its
        # border too and a stale session lost its outline at 0.45 -- exactly
        # when the screen holds the most cards, which is the case the outline
        # is for. There is no stale card to dim now; the collector stopped
        # serving sessions past ACTIVE_SECS.
        #
        # Both halves are asserted together on purpose. The branch without the
        # rule is dead code; the rule without the branch is a trap primed for
        # whoever reintroduces a state field.
        self.assertNotIn(".card.stale", SOURCE)
        self.assertNotIn("stale", function_source("renderSession"))

    def test_the_sparkline_height_does_not_depend_on_the_viewport(self):
        # The svg is emitted width="100%" with preserveAspectRatio="none" and
        # no height, so without a CSS height the browser scaled the height
        # from the viewBox ratio and the *viewport width*: 28px at the 240px
        # it was drawn for, ~45px on a phone, ~180px on a laptop, where it
        # opened a void inside the card taller than the card's own content.
        #
        # Equal to the viewBox height, not merely present: that is what makes
        # the vertical scale 1:1, so a 10% reading sits at a tenth of the
        # line's height rather than at some ratio of the window.
        view_box_height = int(
            re.search(r"const w = \d+, h = (\d+);", function_source("sparkline")).group(1)
        )
        css_height = int(re.search(r"svg \{[^}]*?height: (\d+)px", SOURCE).group(1))
        self.assertEqual(css_height, view_box_height)

    def test_hosts_are_separated_more_than_the_sessions_within_one(self):
        gutter = int(
            re.search(r"\.card \{.*?margin-bottom: (\d+)px", SOURCE, re.S).group(1)
        )
        above_host = int(re.search(r"\.host \{.*?margin: (\d+)px", SOURCE, re.S).group(1))
        self.assertGreater(above_host, gutter)


class TestBudgetCardIsAttributable(unittest.TestCase):
    """FINDING 2b. The store already returned as_of and the page ignored it,
    so the budget figure was an undated claim about current account usage."""

    def test_the_card_renders_the_as_of_time(self):
        body = function_source("renderBudget")
        self.assertIn("as_of", body)
        self.assertIn("clock(", body)


class TestTheWeeklyResetIsDated(unittest.TestCase):
    """Both usage windows printed their reset through `clock`, which renders
    the time and nothing else. For 5h that is unambiguous — the reset is
    hours away. For 7d it is up to a week away, so "↻09:00" does not say
    which day it means.

    A weekday alone, which is what the status line shows, does not settle it
    on this page: a seven-day window resets on the same weekday it started,
    so "Thu 09:00" read on a Thursday is either ten minutes or a week away.
    That ambiguity is worst immediately after a reset, when the figure beside
    it has just dropped to near zero and most invites a wrong read.
    """

    def test_the_weekly_reset_is_not_rendered_by_the_bare_clock(self):
        body = function_source("renderBudget")
        self.assertIn("datedClock", body)

    def test_the_dated_clock_names_the_day_and_the_month(self):
        body = function_source("datedClock")
        self.assertIn("day:", body)
        self.assertIn("month:", body)

    def test_the_dated_clock_still_carries_the_time(self):
        body = function_source("datedClock")
        self.assertIn("hour:", body)
        self.assertIn("minute:", body)

    def test_the_dated_clock_is_guarded(self):
        # new Date(NaN) renders "Invalid Date" here exactly as in clock().
        self.assertIn("num(", function_source("datedClock"))


class TestFailureModesAreDistinguishable(unittest.TestCase):
    """FINDING 6b. One catch rendered "collector unreachable" for a network
    failure, an HTTP error status, a store exception and a bug thrown inside
    renderSession alike. A page whose invariant is honesty must not
    attribute its own bug to the network.
    """

    def test_each_failure_mode_has_its_own_message(self):
        body = function_source("tick")
        for message in (
            "collector unreachable",
            "collector returned HTTP ",
            "unreadable response from the collector",
            "this page failed to render that update",
        ):
            self.assertIn(message, body, message)

    def test_the_http_status_code_is_shown(self):
        body = function_source("tick")
        self.assertIn("response.status", body)

    def test_the_render_error_message_is_escaped(self):
        # It reaches innerHTML like everything else on this page.
        body = function_source("tick")
        self.assertIn("esc(", body)


class TestTheCostIsNotDisplayed(unittest.TestCase):
    """Superseding the earlier requirement that a genuine $0.00 be shown
    rather than dropped. On a Max plan nothing here is billed per session,
    so the figure was noise — and a number with a $ on it reads as a bill
    before it reads as a token estimate, which is the wrong first glance on
    a page meant to be scanned.

    A display decision only: store.py still records cost_usd and the API
    still serves it, so restoring the line is a one-line change.
    """

    def test_the_session_card_states_no_cost(self):
        self.assertNotIn("cost", function_source("renderSession"))

    def test_no_money_figure_is_formatted_anywhere_on_the_page(self):
        # Scoped to the whole file, not one function: the point is that the
        # page shows no dollar amount, wherever it might be assembled.
        self.assertNotIn("$", SOURCE)


class TestNoConfidentZeroes(unittest.TestCase):
    """The page's core rule: `—` beats a figure it cannot stand behind."""

    def test_sparkline_refuses_a_null_sample(self):
        # Unreachable today only because store.py declines to append a
        # sample with a null ctx_pct. An unguarded null plots as a
        # confident 0%, and neither side can see that coupling.
        body = function_source("plottable")
        self.assertIn("ctx_pct", body)
        self.assertIn("Number.isFinite", body)


class TestNonNumbersNeverRenderAsFigures(unittest.TestCase):
    """The residual from finding 7, ruled in scope by the invariant.

    window_pace returns None for a non-numeric used_percentage, but the raw
    value still reaches the page. Math.round("abc") is NaN, so the budget
    card printed "NaN%" — a confident falsehood on the page whose one rule is
    never to state one. tokens("5000") silently coerced to "5k", and
    ("58").toFixed would have thrown.

    A numeric-looking string renders `—` too, deliberately: it is malformed
    input, and `—` is the honest answer for "no valid number here". Coercing
    it would launder bad data into a confident figure.
    """

    def test_there_is_one_finite_number_guard(self):
        self.assertIn("Number.isFinite", function_source("num"))

    def test_tone_treats_a_non_number_as_unknown(self):
        # typeof NaN === 'number' and NaN < 50 is false, so an unguarded NaN
        # fell through every band to 'red' — an alarm invented from garbage.
        self.assertIn("num(", function_source("tone"))

    def test_the_budget_percentage_is_guarded(self):
        self.assertIn("num(w.used_percentage)", function_source("renderBudget"))

    def test_the_budget_times_are_guarded(self):
        body = function_source("renderBudget")
        self.assertIn("num(w.resets_at)", body)
        self.assertIn("num(rateLimits.as_of)", body)

    def test_the_session_percentage_is_guarded(self):
        self.assertIn("num(s.ctx_pct)", function_source("renderSession"))

    def test_token_counts_are_guarded(self):
        # tokens("5000") coerced through >= and / to a confident "5k".
        self.assertIn("num(", function_source("tokens"))

    def test_the_age_is_guarded(self):
        # The guard moved into `duration`, which `age` now delegates to and is
        # the only route into it. Both halves are asserted, so moving the
        # guard out again without moving the call cannot pass.
        self.assertIn("num(", function_source("duration"))
        self.assertIn("duration(secs)", function_source("age"))

    def test_the_clock_is_guarded(self):
        # new Date(NaN).toLocaleTimeString() is "Invalid Date".
        self.assertIn("num(", function_source("clock"))

    def test_sparkline_rejects_nan_not_merely_non_numbers(self):
        # typeof NaN === 'number', so a typeof test would let NaN through
        # and plot it at y = h — a confident 0% nobody reported. The guard now
        # lives in plottable, which is the only way samples reach sparkline.
        body = function_source("plottable")
        self.assertIn("Number.isFinite", body)
        self.assertNotIn("typeof", body)

    def test_sparkline_draws_only_what_plottable_admits(self):
        # The guard is only worth anything if it cannot be bypassed: sparkline
        # must take the vetted set, never a raw history array.
        self.assertIn("sparkline(plottable(", function_source("renderSession"))
        self.assertNotIn("history", function_source("sparkline"))


class TestTheSparklineSaysWhatItCovers(unittest.TestCase):
    """The x-axis is the sample index and the line is stretched to the card's
    full width whatever the count, so two samples twenty seconds apart drew
    exactly the same edge-to-edge line as a full thirty-minute ring. The
    picture could not distinguish them and neither could the reader.

    Autoscaling the y-axis was the other candidate and was rejected: sessions
    here sit at 5-30% of a 1M window, so fitting the line to its own range
    would render a 10%-to-11% drift as a dramatic climb. Inventing alarm from
    noise is the failure this page is built to avoid. The scale stays 0-100
    and comparable between cards; what changed is that the line now says how
    much time it covers, and declines to appear before it covers enough.
    """

    def test_a_line_too_short_to_show_a_trend_is_withheld(self):
        body = function_source("plottable")
        self.assertIn("SPARKLINE_MIN_SPAN_SECS", body)
        self.assertIn("return null", body)

    def test_the_threshold_is_several_samples_not_merely_two(self):
        # At one sample per HISTORY_MIN_INTERVAL_SECS a threshold below about
        # a minute would admit a handful of readings — the shape this guard
        # exists to suppress.
        threshold = int(
            re.search(r"const SPARKLINE_MIN_SPAN_SECS = (\d+);", SOURCE).group(1)
        )
        self.assertGreaterEqual(threshold, 60)

    def test_a_nan_span_is_rejected_by_the_true_branch(self):
        # Every comparison against NaN is false, so `span < MIN` would have
        # returned false for a NaN span and let it through to be captioned
        # "NaNm". The test is written on the passing condition instead.
        body = function_source("plottable")
        self.assertIn("!(span >= SPARKLINE_MIN_SPAN_SECS)", body)

    def test_a_window_in_which_nothing_moved_draws_no_line(self):
        # A constant plots as a horizontal stroke drawn edge to edge in the
        # tone colour, which reads as a trend when it is the absence of one --
        # it restates the percentage above it in a form that looks like it
        # says more. Observed: two sessions holding 10% and 14% without
        # moving for three quarters of an hour, both drawing a confident
        # full-width line. The line's presence is now itself the signal.
        body = function_source("plottable")
        self.assertIn("lo === hi", body)

    def test_the_range_is_not_taken_by_spreading_the_ring(self):
        # Math.min(...samples) spreads HISTORY_SLOTS arguments onto the stack
        # and breaks quietly once the ring is grown.
        body = function_source("plottable")
        self.assertNotIn("Math.min(...", body)
        self.assertNotIn("Math.max(...", body)

    def test_the_line_is_captioned_with_the_time_it_covers(self):
        self.assertIn("duration(plot.span)", function_source("sparkline"))

    def test_the_caption_costs_the_card_no_height(self):
        # Beside the line, not beneath it. A stacked caption would have given
        # back the height just reclaimed by pinning the svg.
        rule = re.search(r"\.spark \{[^}]*\}", SOURCE).group(0)
        self.assertIn("display: flex", rule)
        self.assertIn("align-items: center", rule)

    def test_the_line_yields_width_to_the_caption_not_the_reverse(self):
        # A flex item's floor is its content size, not zero, so without
        # min-width:0 the caption is pushed off the card on a narrow screen.
        rule = re.search(r"\.spark > svg \{[^}]*\}", SOURCE).group(0)
        self.assertIn("min-width: 0", rule)


class TestToneColorHasNoDeadTernary(unittest.TestCase):
    """`tone(pct) === 'dim' ? 'dim' : tone(pct)` always equals tone(pct)."""

    def test_the_no_op_ternary_is_gone(self):
        self.assertNotIn("?", function_source("toneColor"))



class TestAStaleCopyOfThePageIsRecognisable(unittest.TestCase):
    """The server writes "updated <time>" into the HTML so an offline copy,
    which runs no JavaScript, still dates itself. The live page must keep that
    stamp current, or a page left open all day would say it is hours old.
    """

    def test_the_stamp_is_outside_the_element_every_render_replaces(self):
        # show() overwrites #app wholesale, so a stamp inside it would be
        # destroyed by the first successful poll.
        self.assertTrue('id="stamp"' in SOURCE, 'no stamp element')
        self.assertLess(SOURCE.index('id="stamp"'), SOURCE.index('<div id="app">'))

    def test_the_stamp_carries_the_server_placeholder(self):
        from collector.server import STAMP_PLACEHOLDER

        match = re.search(r'<[^>]*id="stamp"[^>]*>([^<]*)<', SOURCE)
        self.assertIsNotNone(match)
        self.assertIn(STAMP_PLACEHOLDER, match.group(1))

    def test_tick_moves_the_stamp_only_after_a_successful_render(self):
        body = function_source("tick")
        rendered = body.index("show(render(data))")
        stamped = body.index("stamp")
        render_failed = body.index("this page failed to render that update")
        # Not before the fetch, the status check or the parse have passed, and
        # not in the render's catch: a failed update must leave the stamp
        # saying when the page was last right.
        self.assertLess(rendered, stamped)
        self.assertLess(stamped, render_failed)
        self.assertEqual(body.count("stamp"), 1)

    def test_the_live_stamp_uses_the_servers_format_not_the_locales(self):
        # toLocaleString would render "Sep 5, 2:02 PM" on one phone and
        # "05/09, 14:02" on another, so the stamp would change shape on the
        # first poll and the no-JavaScript copy would not match the live one.
        body = function_source("stampText")
        self.assertNotIn("toLocale", body)
        self.assertIn("getDate()", body)


if __name__ == "__main__":
    unittest.main()


class TestTheCloudSectionSaysWhichSilence(unittest.TestCase):
    """2026-09-25. Cloud sessions are polled, not reported, so the section
    has silences of its own: never fetched, withheld as old, login lapsed,
    API error. None of them may render as an empty list.
    """

    def test_a_missing_list_is_never_rendered_as_empty(self):
        body = function_source("renderCloud")
        self.assertIn("Array.isArray(cloud.sessions)", body)
        self.assertIn("stale_as_of", body)
        self.assertIn("checking", body)

    def test_a_failed_poll_is_said_above_the_cards_it_did_not_refresh(self):
        self.assertIn("cloud.state !== 'ok'", function_source("renderCloud"))

    def test_missing_routine_sessions_are_said_and_not_called_none(self):
        # 2026-10-04: a failing trigger list keeps the plain cards and sends
        # routine_detail instead. An empty plain list beside it must not
        # read as "no active cloud sessions", which the routines may falsify.
        body = function_source("renderCloud")
        self.assertIn("esc(cloud.routine_detail)", body)
        self.assertIn("no other cloud sessions", body)
        self.assertIn("warning + routine + pinnedFirst(cloud.sessions", body)

    def test_an_unknown_state_is_shown_verbatim_not_guessed(self):
        self.assertIn("|| [s.bucket", function_source("renderCloudSession"))

    def test_the_cloud_card_states_no_cost(self):
        self.assertNotIn("cost", function_source("renderCloudSession"))

    def test_the_cloud_card_escapes_what_the_session_wrote(self):
        body = function_source("renderCloudSession")
        for call in ("esc(s.title", "esc(note)", "esc(s.model)"):
            self.assertIn(call, body)
        # The url is escaped once, where every card link is built.
        self.assertIn("esc(url)", function_source("card"))

    def test_the_section_is_rendered(self):
        self.assertIn("renderCloud(data.cloud)", function_source("render"))


class TestEveryCardSaysWhereItRuns(unittest.TestCase):
    """2026-09-26. Laptop, Coder VM or cloud, readable at a glance — on the
    card, because on a phone the heading is usually scrolled away.
    """

    def test_host_cards_carry_the_place_the_collector_sent(self):
        body = function_source("renderHostGroups")
        self.assertIn("h.place", body)
        self.assertIn("renderSession(s, place)", body)
        self.assertIn("placeClass(place)", body)
        self.assertIn("placeClass(place)", function_source("renderSession"))

    def test_cloud_cards_are_marked_cloud(self):
        self.assertIn("at-cloud", function_source("renderCloudSession"))
        self.assertIn("heading('cloud', 'cloud')", function_source("renderCloud"))

    def test_no_place_draws_nothing(self):
        # A host that did not say what it is is not guessed at.
        self.assertIn("if (!place) return ''", function_source("placeClass"))
        self.assertIn("if (!place)", function_source("heading"))

    def test_a_place_outside_the_table_is_drawn_as_its_own_word(self):
        # 2026-09-29, open-source plan step 3: any short label a machine
        # states is shown, in a neutral colour. The collector only passes a
        # plain [a-z0-9-] word, and the page escapes it all the same.
        self.assertIn("' at-other'", function_source("placeClass"))
        self.assertIn("esc(place)", function_source("heading"))
        self.assertIn(".at-other", SOURCE)

    def test_the_heading_escapes_the_host_name(self):
        self.assertIn("esc(name)", function_source("heading"))

    def test_colour_is_never_the_only_cue(self):
        body = function_source("heading")
        self.assertIn("known[0]", body)
        self.assertIn("known[1]", body)


class TestARemoteControlCardOpensOnClaudeAi(unittest.TestCase):
    """2026-09-28. A card with a url is one link, the whole card: tapping
    the title alone was tried first and read on the phone as "nothing
    happens when I tap on a card"."""

    def test_the_whole_card_is_the_link(self):
        body = function_source("card")
        self.assertIn("return url\n    ?", body)
        self.assertIn("'<a class=\"card' + cls + '\" href=\"' + esc(url)", body)
        self.assertIn("'<div class=\"card' + cls + '\">'", body)

    def test_host_and_cloud_cards_both_go_through_it(self):
        self.assertIn("card(placeClass(place), s.url,", function_source("renderSession"))
        self.assertIn("card(' at-cloud', s.url,", function_source("renderCloudSession"))

    def test_no_link_is_nested_inside_a_card(self):
        # An <a> inside an <a> is invalid HTML, and browsers split it apart.
        self.assertNotIn('<a class="proj"', SOURCE)


class TestPayloadWordsStayInert(unittest.TestCase):
    def test_esc_covers_both_quote_characters(self):
        # An attribute quoted with ' would otherwise be closable by the payload.
        body = function_source("esc")
        self.assertIn(".replace(/\"/g, '&quot;')", body)
        self.assertIn(".replace(/'/g, '&#39;')", body)

    def test_lookups_only_find_the_tables_own_entries(self):
        # `constructor` is a plain [a-z0-9-] word the collector passes as a
        # place; on a bare object lookup it finds Object's constructor.
        self.assertIn("hasOwnProperty.call(table, key)", function_source("lookup"))
        for name in ("placeClass", "heading", "renderCloudSession"):
            with self.subTest(function=name):
                body = function_source(name)
                self.assertNotIn("PLACES[", body)
                self.assertNotIn("CLOUD_STATES[", body)
        self.assertIn("lookup(PLACES, place)", function_source("heading"))
        self.assertIn("lookup(CLOUD_STATES, s.bucket)", function_source("renderCloudSession"))


class TestPinnedCardsFloatWithinTheirGroup(unittest.TestCase):
    """2026-10-03: a pin floats a card to the top of its own group, kept per
    device. The order within pinned and within unpinned cards stays the
    collector's, so pinning moves only the card that was pinned."""

    def test_the_partition_is_stable(self):
        body = function_source("pinnedFirst")
        self.assertIn("list.filter(s => keyOf(s) in pins).concat(list.filter(s => !(keyOf(s) in pins)))", body)
        self.assertNotIn(".sort(", body)

    def test_both_groups_are_partitioned(self):
        self.assertIn("pinnedFirst(sessions.filter(s => s.host === host), localKey)",
                      function_source("renderHostGroups"))
        self.assertIn("pinnedFirst(cloud.sessions, cloudKey)", function_source("renderCloud"))

    def test_both_kinds_of_card_carry_the_button(self):
        self.assertIn("pinButton(localKey(s))", function_source("renderSession"))
        self.assertIn("pinButton(cloudKey(s))", function_source("renderCloudSession"))

    def test_the_button_says_its_state(self):
        body = function_source("pinButton")
        for needle in ("aria-pressed", "'Unpin'", "'Pin to top'", 'title="', "esc(key)"):
            self.assertIn(needle, body)

    def test_it_is_gitgumbos_pin(self):
        body = function_source("pinButton")
        self.assertIn('<line x1="12" x2="12" y1="17" y2="22"/>', body)
        self.assertIn("M5 17h14v-1.76", body)

    def test_storage_failures_are_caught(self):
        self.assertIn("try {", function_source("loadPins"))
        self.assertIn("try {", function_source("savePins"))

    def test_a_tap_on_the_pin_does_not_follow_the_card_link(self):
        start = SOURCE.index("addEventListener('click'")
        handler = SOURCE[start:SOURCE.index("\n});\n", start)]
        self.assertIn("event.preventDefault();", handler)
        self.assertIn("event.stopPropagation();", handler)

    def test_no_inline_handler_the_csp_would_block(self):
        self.assertNotIn("onclick", SOURCE)

    def test_pins_are_read_once_not_on_every_render(self):
        # Where storage refuses writes, a re-read would drop this visit's pins.
        self.assertNotIn("loadPins()", function_source("render"))


class TheLogoShowsOnAWideScreenOnly(unittest.TestCase):
    """On a phone the cards get every line; on a laptop there is room."""

    def test_the_header_is_hidden_unless_the_screen_is_wide(self):
        self.assertIn(".brand { display: none; }", SOURCE)
        wide = SOURCE[SOURCE.index("@media (min-width: 700px)"):]
        self.assertIn("display: flex", wide[:wide.index("}\n  }")])

    def test_it_uses_an_icon_the_collector_serves(self):
        self.assertIn('<header class="brand"><img src="/icons/icon-192.png"', SOURCE)


class TestThemes(unittest.TestCase):
    """2026-10-03. GitGumbo's theme picker, on this page's own palettes."""

    def css_themes(self):
        found = {}
        for name, block in re.findall(r':root\[data-theme="([a-z-]+)"\] \{(.*?)\}', SOURCE, re.S):
            found[name] = dict(re.findall(r"--([a-z]+): (#[0-9a-f]{6})", block))
        root = re.search(r"  :root \{(.*?)\n  \}", SOURCE, re.S).group(1)
        found["dark"] = dict(re.findall(r"--([a-z]+): (#[0-9a-f]{6})", root))
        return found

    def js_themes(self):
        found = {}
        for name, body in re.findall(r"^  '?([a-z-]+)'?: \{ label: '[^']+', (.*?) \},$", SOURCE, re.M):
            found[name] = dict(re.findall(r"(\w+): '(#[0-9a-f]{6})'", body))
        return found

    def test_every_palette_has_a_picker_entry_and_the_reverse(self):
        self.assertEqual(set(self.css_themes()), set(self.js_themes()))
        self.assertEqual(len(self.js_themes()), 7)

    def test_each_preview_mirrors_its_palette(self):
        css = self.css_themes()
        for name, preview in self.js_themes().items():
            for token, colour in preview.items():
                self.assertEqual(css[name][token], colour, "%s --%s" % (name, token))

    def test_every_palette_sets_every_token(self):
        tokens = set(self.css_themes()["dark"])
        for name, palette in self.css_themes().items():
            self.assertEqual(set(palette), tokens, name)

    def test_the_theme_is_applied_before_the_body_is_parsed(self):
        head = SOURCE[:SOURCE.index("<body>")]
        self.assertIn("applyTheme(themeChoice);", head)

    def test_a_stored_choice_is_checked_before_it_is_used(self):
        body = function_source("loadThemeChoice")
        self.assertIn("isThemeChoice(stored)", body)
        self.assertIn("try", body)

    def test_choosing_redraws_the_cards(self):
        # Bars and sparklines take toneColor() at render time.
        body = function_source("chooseTheme")
        self.assertIn("applyTheme(choice)", body)
        self.assertIn("redraw()", body)

    def test_enter_on_an_option_dismisses_without_picking(self):
        self.assertIn("event.preventDefault();\n    setMenuOpen(false);", SOURCE)


class TestTheCloudContextIsDated(unittest.TestCase):
    """2026-10-03. The API's context figure lags with no time of its own."""

    def test_the_card_shows_when_the_figure_was_first_seen(self):
        body = function_source("renderCloudSession")
        self.assertIn("num(s.ctx_as_of)", body)
        self.assertIn("clock(asOf)", body)

    def test_a_date_the_poll_cannot_vouch_for_is_marked(self):
        self.assertIn("s.ctx_as_of_exact === true ? '' : '≤'", function_source("renderCloudSession"))

    def test_an_unknown_figure_is_not_dated(self):
        self.assertIn("known ? num(s.ctx_as_of) : null", function_source("renderCloudSession"))
