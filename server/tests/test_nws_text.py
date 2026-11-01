#!/usr/bin/env python3
"""Tests for hamops_console.nws_text (the WHAT/HAZARD lines ported from Weather Sentinel).

No network, no Pi, no third-party packages:

    python server/tests/test_nws_text.py      (or: python -m pytest server/tests)

The product texts below are shaped like real NWS alerts (hard line breaks at
about 69 columns, "HAZARD..." and "* WHAT..." sections) but are written for
these tests; the place names are invented.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from hamops_console.nws_text import (  # noqa: E402
    HAZARD_MAX_CHARS, WHAT_MAX_CHARS, _bound, extract_reason_fields,
)

WARNING = {
    "event": "Severe Thunderstorm Warning",
    "parameters": {"NWSheadline": ["SEVERE THUNDERSTORM WARNING IN EFFECT UNTIL 430 PM EDT FOR EXAMPLE COUNTY"]},
    "description": (
        "At 352 PM EDT, a severe thunderstorm was located near Sampletown,\n"
        "moving east at 25 mph.\n\n"
        "HAZARD...60 mph wind gusts and quarter size hail.\n\n"
        "SOURCE...Radar indicated.\n\n"
        "IMPACT...Hail damage to vehicles is expected."
    ),
}

WATCH = {
    "event": "Flood Watch",
    "parameters": {"NWSheadline": ["FLOOD WATCH IN EFFECT THROUGH SATURDAY EVENING"]},
    "description": (
        "* WHAT...Flooding caused by excessive rainfall is possible.\n\n"
        "* WHERE...Portions of central Example State.\n\n"
        "* WHEN...Through Saturday evening."
    ),
}


class ExtractReasonFields(unittest.TestCase):

    def test_warning_headline_loses_time_phrase_and_hazard_found(self):
        what, hazard = extract_reason_fields(WARNING)
        self.assertEqual(what, "SEVERE THUNDERSTORM WARNING IN EFFECT FOR EXAMPLE COUNTY")
        self.assertEqual(hazard, "60 mph wind gusts and quarter size hail.")

    def test_watch_uses_star_what_bullet_for_hazard(self):
        what, hazard = extract_reason_fields(WATCH)
        self.assertEqual(hazard, "Flooding caused by excessive rainfall is possible.")
        # No clock time to strip ("through Saturday evening" has no time), so the headline stays.
        self.assertEqual(what, "FLOOD WATCH IN EFFECT THROUGH SATURDAY EVENING")

    def test_headline_that_only_repeats_the_event_is_dropped(self):
        props = {"event": "Flood Watch", "parameters": {"NWSheadline": ["FLOOD WATCH REMAINS IN EFFECT"]},
                 "description": "* WHAT...Flooding is possible."}
        what, hazard = extract_reason_fields(props)
        self.assertIsNone(what)
        self.assertEqual(hazard, "Flooding is possible.")

    def test_hard_line_breaks_are_squashed(self):
        props = {"event": "Special Weather Statement",
                 "description": "HAZARD...Wind gusts up to 50\nmph and pea size\nhail.\n\nSOURCE...Radar."}
        _, hazard = extract_reason_fields(props)
        self.assertEqual(hazard, "Wind gusts up to 50 mph and pea size hail.")

    def test_no_headline_falls_back_to_first_paragraph(self):
        props = {"event": "Special Weather Statement",
                 "description": "A strong thunderstorm will impact\nSampletown through 445 PM.\n\nHAZARD...Gusty winds."}
        what, hazard = extract_reason_fields(props)
        self.assertEqual(what, "A strong thunderstorm will impact Sampletown through 445 PM.")
        self.assertEqual(hazard, "Gusty winds.")

    def test_labelled_first_paragraph_is_not_used_as_what(self):
        props = {"event": "Wind Advisory", "description": "* WHAT...West winds 20 to 30 mph.\n\n* WHERE...Example County."}
        what, hazard = extract_reason_fields(props)
        self.assertIsNone(what)
        self.assertEqual(hazard, "West winds 20 to 30 mph.")

    def test_long_text_is_bounded_at_a_word(self):
        long = "word " * 80
        props = {"event": "X", "description": "HAZARD..." + long}
        _, hazard = extract_reason_fields(props)
        self.assertLessEqual(len(hazard), HAZARD_MAX_CHARS)
        self.assertTrue(hazard.endswith("..."))
        self.assertTrue(hazard[:-3].endswith("word"))
        self.assertLessEqual(len(_bound("y " * 200, WHAT_MAX_CHARS)), WHAT_MAX_CHARS)

    def test_bad_input_never_raises(self):
        for props in ({}, {"event": None, "description": None},
                      {"description": 12345, "parameters": "not a dict"},
                      {"parameters": {"NWSheadline": [None]}},
                      {"parameters": {"NWSheadline": []}}):
            self.assertEqual(extract_reason_fields(props), (None, None), props)
        # Not even a dict: still no exception.
        self.assertEqual(extract_reason_fields(None), (None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
