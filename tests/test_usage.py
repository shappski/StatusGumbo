import unittest

from collector.usage import FIVE_HOUR_SECS, SEVEN_DAY_SECS, window_pace


class TestWindowPace(unittest.TestCase):
    def test_behind_linear_burn_is_negative(self):
        # Halfway through the window (elapsed 50%) having used 10%.
        resets_at = 1_785_000_000
        now = resets_at - FIVE_HOUR_SECS / 2
        self.assertAlmostEqual(
            window_pace(10, resets_at, FIVE_HOUR_SECS, now), -40.0
        )

    def test_ahead_of_linear_burn_is_positive(self):
        resets_at = 1_785_000_000
        now = resets_at - FIVE_HOUR_SECS / 2
        self.assertAlmostEqual(
            window_pace(90, resets_at, FIVE_HOUR_SECS, now), 40.0
        )

    def test_on_pace_is_zero(self):
        resets_at = 1_785_000_000
        now = resets_at - FIVE_HOUR_SECS / 2
        self.assertAlmostEqual(
            window_pace(50, resets_at, FIVE_HOUR_SECS, now), 0.0
        )

    def test_elapsed_clamps_at_zero_before_window_start(self):
        # A clock skewed earlier than the window start must not produce a
        # negative elapsed, which would invent pace out of nothing.
        resets_at = 1_785_000_000
        now = resets_at - FIVE_HOUR_SECS - 10_000
        self.assertAlmostEqual(
            window_pace(20, resets_at, FIVE_HOUR_SECS, now), 20.0
        )

    def test_elapsed_clamps_at_hundred_after_reset(self):
        resets_at = 1_785_000_000
        now = resets_at + 10_000
        self.assertAlmostEqual(
            window_pace(20, resets_at, FIVE_HOUR_SECS, now), -80.0
        )

    def test_missing_reset_returns_none(self):
        self.assertIsNone(window_pace(50, None, FIVE_HOUR_SECS, 1_785_000_000))

    def test_missing_used_returns_none(self):
        self.assertIsNone(
            window_pace(None, 1_785_000_000, FIVE_HOUR_SECS, 1_785_000_000)
        )

    def test_seven_day_window_length(self):
        self.assertEqual(SEVEN_DAY_SECS, 7 * 24 * 60 * 60)
        self.assertEqual(FIVE_HOUR_SECS, 5 * 60 * 60)


class TestWindowPaceRejectsNonNumbers(unittest.TestCase):
    """FINDING 7. ingest typed rate_limits only as far as `isinstance(dict)`;
    the values inside were never checked. A tick carrying
    {"five_hour": {"used_percentage": "58"}} returned 204, and every later
    snapshot raised TypeError here. None is the established "cannot be
    computed" answer and the page already renders it honestly.
    """

    RESETS_AT = 1_785_000_000
    NOW = 1_785_000_000 - 9000

    def test_string_used_percentage_returns_none(self):
        self.assertIsNone(
            window_pace("58", self.RESETS_AT, FIVE_HOUR_SECS, self.NOW)
        )

    def test_string_resets_at_returns_none(self):
        self.assertIsNone(
            window_pace(58, "1785000000", FIVE_HOUR_SECS, self.NOW)
        )

    def test_container_values_return_none(self):
        self.assertIsNone(
            window_pace({}, self.RESETS_AT, FIVE_HOUR_SECS, self.NOW)
        )
        self.assertIsNone(window_pace(58, [], FIVE_HOUR_SECS, self.NOW))

    def test_bool_does_not_count_as_a_number(self):
        # bool is a subclass of int in Python, so a bare isinstance(int)
        # check would silently accept True and report it as 1% used — a
        # confident figure invented out of a type error.
        self.assertIsNone(
            window_pace(True, self.RESETS_AT, FIVE_HOUR_SECS, self.NOW)
        )
        self.assertIsNone(window_pace(58, True, FIVE_HOUR_SECS, self.NOW))

    def test_a_genuine_zero_is_still_a_number(self):
        # 0% used is a real, reportable figure and must not be swept up.
        self.assertIsNotNone(
            window_pace(0, self.RESETS_AT, FIVE_HOUR_SECS, self.NOW)
        )

    def test_floats_are_accepted(self):
        self.assertIsNotNone(
            window_pace(58.5, float(self.RESETS_AT), FIVE_HOUR_SECS, self.NOW)
        )


if __name__ == "__main__":
    unittest.main()
