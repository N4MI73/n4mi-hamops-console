#!/usr/bin/env python3
"""Tests for the Field Day timer and its settings (2026-11-12).

    python server/tests/test_field_day.py      (or: python -m pytest server/tests)
"""

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from hamops_console import field_day, settings as settings_mod, settings_api  # noqa: E402


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def settings(profile="field_day", start="2027-01-30T19:00Z", end="2027-01-31T19:00Z"):
    s = settings_mod.load(os.path.join(tempfile.mkdtemp(), "missing.json"))
    s["profile"] = profile
    s["field_day"] = {"ops_start": start, "ops_end": end}
    return s


class InEastern(unittest.TestCase):
    """Local midnight depends on the Pi's zone; these run as if in Georgia."""

    def setUp(self):
        self.old = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()

    def tearDown(self):
        if self.old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.old
        time.tzset()


class Phases(InEastern):

    def test_setup_starts_at_local_midnight(self):
        # Ops 19:00Z Sat = 2:00 PM EST; local midnight Sat = 05:00Z.
        self.assertEqual(field_day.setup_start(utc(2027, 1, 30, 19)), utc(2027, 1, 30, 5))

    def test_early_utc_start_belongs_to_the_previous_local_day(self):
        # 02:00Z Sun is 9 PM Sat in Georgia, so setup starts at midnight Sat local.
        self.assertEqual(field_day.setup_start(utc(2027, 1, 31, 2)), utc(2027, 1, 30, 5))

    def test_daylight_saving_offset(self):
        # June (ARRL Field Day): EDT, local midnight = 04:00Z.
        self.assertEqual(field_day.setup_start(utc(2027, 6, 26, 18)), utc(2027, 6, 26, 4))

    def test_four_phases(self):
        s = settings()
        for now, want in [(utc(2027, 1, 28, 12), "reminder"), (utc(2027, 1, 30, 4, 59), "reminder"),
                          (utc(2027, 1, 30, 5), "setup"), (utc(2027, 1, 30, 18, 59), "setup"),
                          (utc(2027, 1, 30, 19), "operations"), (utc(2027, 1, 31, 18, 59), "operations"),
                          (utc(2027, 1, 31, 19), "complete"), (utc(2027, 2, 3), "complete")]:
            self.assertEqual(field_day.block(s, now)["phase"], want, now)

    def test_block_times(self):
        b = field_day.block(settings(), utc(2027, 1, 28))
        self.assertEqual((b["ops_start"], b["ops_end"], b["setup_start"]),
                         ("2027-01-30T19:00:00Z", "2027-01-31T19:00:00Z", "2027-01-30T05:00:00Z"))
        self.assertTrue(b["enabled"])

    def test_off_and_unset(self):
        self.assertEqual(field_day.block(settings(profile="general_hf"), utc(2027, 1, 30, 20))["phase"], "off")
        self.assertEqual(field_day.block(settings(start=None, end=None))["phase"], "unset")
        self.assertEqual(field_day.block(settings(start="2027-01-31T19:00Z", end="2027-01-30T19:00Z"))["phase"], "unset")
        self.assertEqual(field_day.block(settings(start="tomorrow"))["phase"], "unset")


class SettingsChecks(unittest.TestCase):

    def check(self, changes, current=None):
        return settings_api.check(changes, current or settings(profile="general_hf", start=None, end=None))

    def test_good(self):
        errors, warnings, _ = self.check({"profile": "field_day",
                                          "field_day": {"ops_start": "2027-01-30T19:00Z", "ops_end": "2027-01-31T19:00Z"}})
        self.assertEqual((errors, warnings), ([], []))

    def test_one_time_at_a_time_and_clearing(self):
        self.assertEqual(self.check({"field_day": {"ops_start": "2027-01-30T19:00Z"}})[0], [])
        self.assertEqual(self.check({"field_day": {"ops_start": None}})[0], [])

    def test_bad(self):
        for changes, words in [({"profile": "contest"}, "general_hf"),
                               ({"field_day": {"ops_start": "2027-01-30 19:00"}}, "UTC date and time"),
                               ({"field_day": {"ops_start": "2027-02-30T19:00Z"}}, "UTC date and time"),
                               ({"field_day": {"setup": "x"}}, "isn't a Field Day setting"),
                               ({"field_day": "soon"}, "must be an object"),
                               ({"field_day": {"ops_start": "2027-01-31T19:00Z", "ops_end": "2027-01-30T19:00Z"}},
                                "end after they start")]:
            errors = self.check(changes)[0]
            self.assertTrue(any(words in e for e in errors), (changes, errors))

    def test_end_before_existing_start(self):
        current = settings(start="2027-01-30T19:00Z", end="2027-01-31T19:00Z")
        self.assertTrue(self.check({"field_day": {"ops_end": "2027-01-30T18:00Z"}}, current)[0])

    def test_long_event_warns(self):
        _, warnings, _ = self.check({"field_day": {"ops_start": "2027-01-30T19:00Z", "ops_end": "2027-02-03T19:00Z"}})
        self.assertTrue(warnings)


if __name__ == "__main__":
    unittest.main()
