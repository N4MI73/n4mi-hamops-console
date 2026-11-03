#!/usr/bin/env python3
"""Tests for the weather blocks (Weather Sentinel relay) and alert classification.

No network: Weather Sentinel is replaced by a stand-in and the clock is fixed.
Needs Flask for the endpoint test (on the Pi: the python3-flask package).

    python server/tests/test_weather.py      (or: python -m pytest server/tests)

The replies below are the live sample of 2026-11-03 and the alert/lightning
samples generated from Weather Sentinel's own code on 2026-11-01
(Claude Project files live_samples_2026-11-03.md and
weather_sentinel_alert_samples_2026-11-01.md).
"""

import copy
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from hamops_console import alert_classes, freshness, settings as settings_mod  # noqa: E402
from hamops_console.weather import WeatherSource  # noqa: E402

LIVE = {
    "lightning": {"active": False,
                  "close": {"active": False, "hold_minutes": 30, "last_distance_mi": None,
                            "last_strike_epoch": None, "minutes_since": None, "radius_mi": 10.0,
                            "strikes_last_minute": 0},
                  "closest_distance_mi": None, "filter_radius_mi": 20.0, "level": "none",
                  "most_recent_strike_at": None, "most_recent_strike_epoch": None,
                  "strike_count_recent": 0, "window_minutes": 10},
    "nws": {"alerts": [], "available": True, "last_polled_at": "2026-11-03T00:22:46.896154+00:00"},
    "simulation": False,
    "tempest": {"available": True, "feels_like_f": 69.5, "gust_mph": 1.7, "humidity_percent": 69,
                "observation_age_sec": 16, "observed_at": "2026-11-03T00:26:13+00:00",
                "pressure_inhg_station": 29.67, "rain_rate_in_hr": 0.0, "rain_rate_level": "none",
                "rain_this_interval_in": 0.0, "temperature_f": 69.5, "wind_direction": "WNW",
                "wind_mph": 0.8},
}
T_LIVE = datetime(2026, 11, 3, 0, 27, 0, tzinfo=timezone.utc)


def _alert(id_, event, level, expires, what, hazard, acknowledged):
    return {"id": id_, "event": event, "severity": "Severe", "urgency": "Immediate",
            "certainty": "Observed", "onset": None, "expires": expires, "headline": what,
            "instruction": "Example instruction.", "what": what, "hazard": hazard,
            "level": level, "acknowledged": acknowledged, "last_transition": "new",
            "needs_alert": not acknowledged,
            "first_seen_at": "2026-11-01T14:51:32.988451+00:00",
            "last_updated_at": "2026-11-01T14:51:32.988451+00:00"}


# Sentinel's list order (watch first, because it is still sounding) on purpose.
MULTI_ALERTS = [
    _alert("urn:sim:multi-flood-watch", "Flood Watch", "watch", "2026-11-01T15:36:32.988451+00:00",
           "Additional heavy rain is possible across the simulated area tonight.",
           "Flooding caused by excessive rainfall is possible.", False),
    _alert("urn:sim:multi-tstorm-warning", "Severe Thunderstorm Warning", "warning",
           "2026-11-01T15:36:32.988451+00:00",
           "A severe thunderstorm was located over the simulated area, moving east at 25 mph.",
           "60 mph wind gusts and quarter size hail.", True),
    _alert("urn:sim:multi-flood-advisory", "Flood Advisory", "advisory",
           "2026-11-01T15:36:32.988451+00:00",
           "Flooding caused by excessive rainfall is expected in the simulated area.",
           "Minor flooding of low-lying areas.", True),
]
CLOSE_LIGHTNING = {
    "active": True, "level": "frequent", "closest_distance_mi": 3.5,
    "most_recent_strike_at": "2026-11-01T14:51:32+00:00", "most_recent_strike_epoch": 1793544692,
    "strike_count_recent": 4, "window_minutes": 10, "filter_radius_mi": 10.0,
    "close": {"active": True, "radius_mi": 10.0, "hold_minutes": 30, "last_distance_mi": 3.5,
              "last_strike_epoch": 1793544692, "minutes_since": 0, "strikes_last_minute": 1},
}
T_STORM = datetime(2026, 11, 1, 14, 52, 0, tzinfo=timezone.utc)


def storm_reply(**changes):
    """A real-mode reply during a storm: three alerts and close lightning."""
    reply = copy.deepcopy(LIVE)
    reply["nws"] = {"alerts": copy.deepcopy(MULTI_ALERTS), "available": True,
                    "last_polled_at": "2026-11-01T14:51:32.988451+00:00"}
    reply["lightning"] = copy.deepcopy(CLOSE_LIGHTNING)
    reply["tempest"]["observed_at"] = "2026-11-01T14:51:40+00:00"
    reply.update(changes)
    return reply


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class StandIn:
    """Plays Weather Sentinel: returns `reply`, or raises if it is an exception."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def __call__(self, base_url):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return copy.deepcopy(self.reply)


def make(reply, now, mode="home"):
    settings = settings_mod.load(os.path.join(tempfile.mkdtemp(), "missing.json"))
    settings["mode"] = mode
    settings["sources"]["sentinel_url"] = "http://sentinel.example:8085"
    stand_in, clock = StandIn(reply), Clock(now)
    return WeatherSource(settings, fetch=stand_in, clock=clock), stand_in, clock


class AlertClasses(unittest.TestCase):

    def test_kind(self):
        self.assertEqual(alert_classes.kind("Tornado Warning"), "warning")
        self.assertEqual(alert_classes.kind("Severe Thunderstorm Watch"), "watch")
        self.assertEqual(alert_classes.kind("Wind Advisory"), "advisory")
        self.assertEqual(alert_classes.kind("Special Weather Statement"), "statement")
        self.assertEqual(alert_classes.kind(None), "statement")

    def test_hazard_class(self):
        cases = {
            "Tornado Warning": "site_hazard", "Severe Thunderstorm Watch": "site_hazard",
            "High Wind Warning": "site_hazard", "Flash Flood Warning": "site_hazard",
            "Flood Watch": "site_hazard", "Tropical Storm Watch": "site_hazard",
            "Storm Surge Warning": "site_hazard", "Special Weather Statement": "site_hazard",
            "Wind Chill Advisory": "temperature", "Extreme Cold Warning": "temperature",
            "Heat Advisory": "temperature", "Extreme Heat Warning": "temperature",
            "Freeze Warning": "other", "Winter Storm Warning": "other",
            "Red Flag Warning": "other", "Dense Fog Advisory": "other", "": "other",
        }
        for event, expected in cases.items():
            self.assertEqual(alert_classes.hazard_class(event), expected, event)


class Freshness(unittest.TestCase):

    def test_force_stale_and_unavailable_after(self):
        now = T_LIVE
        b = freshness.block("X", now, now, 600, now=now, force_stale=True, error="X can't reach NWS")
        self.assertEqual((b["status"], b["message"]), ("stale", "X can't reach NWS"))
        old = now - timedelta(minutes=31)
        b = freshness.block("X", now, old, 600, now=now, unavailable_after_sec=1800)
        self.assertEqual(b["status"], "unavailable")
        b = freshness.block("X", now, old, 600, now=now, test=True, unavailable_after_sec=1800)
        self.assertEqual(b["status"], "unavailable")  # too old beats test mode


class LiveSample(unittest.TestCase):

    def setUp(self):
        self.source, self.stand_in, self.clock = make(LIVE, T_LIVE)
        self.w = self.source.blocks()

    def test_alerts_ok_and_empty(self):
        a = self.w["alerts"]
        self.assertEqual(a["status"], "ok")
        self.assertEqual(a["source"], "Weather Sentinel")
        self.assertEqual(a["issued_at"], "2026-11-03T00:22:46Z")
        self.assertEqual(a["items"], [])

    def test_conditions(self):
        c = self.w["conditions"]
        self.assertEqual(c["status"], "ok")
        self.assertEqual(c["issued_at"], "2026-11-03T00:26:13Z")
        self.assertEqual((c["temperature_f"], c["humidity_pct"], c["wind_dir"]), (69.5, 69, "WNW"))
        self.assertEqual((c["pressure_inhg"], c["station"]), (29.67, None))

    def test_lightning_quiet(self):
        l = self.w["lightning"]
        self.assertEqual(l["status"], "ok")
        self.assertIs(l["within_10mi"], False)
        self.assertIs(l["within_5mi"], False)
        self.assertIs(l["frequent"], False)
        self.assertEqual((l["strikes_recent"], l["window_min"], l["closest_mi"]), (0, 10, None))

    def test_later_blocks_not_yet_built(self):
        self.assertIsNone(self.w["forecast"])
        self.assertIsNone(self.w["weather_radio"])
        self.assertEqual(self.w["location"]["label"], "Home")

    def test_fetches_at_most_once_a_minute(self):
        self.clock.now = T_LIVE + timedelta(seconds=30)
        self.source.blocks()
        self.assertEqual(self.stand_in.calls, 1)
        self.clock.now = T_LIVE + timedelta(seconds=61)
        self.source.blocks()
        self.assertEqual(self.stand_in.calls, 2)


class Storm(unittest.TestCase):

    def test_alerts_sorted_by_console_and_bedside_fields_dropped(self):
        source, _, _ = make(storm_reply(), T_STORM)
        items = source.blocks()["alerts"]["items"]
        self.assertEqual([i["event"] for i in items],
                         ["Severe Thunderstorm Warning", "Flood Watch", "Flood Advisory"])
        first = items[0]
        self.assertEqual((first["kind"], first["hazard_class"]), ("warning", "site_hazard"))
        self.assertEqual(first["hazard"], "60 mph wind gusts and quarter size hail.")
        self.assertEqual(first["expires"], "2026-11-01T15:36:32Z")
        for item in items:
            for field in ("acknowledged", "needs_alert", "level", "last_transition"):
                self.assertNotIn(field, item)

    def test_expired_alerts_dropped(self):
        source, _, _ = make(storm_reply(), T_STORM + timedelta(minutes=45))
        self.assertEqual(source.blocks()["alerts"]["items"], [])

    def test_close_lightning_and_own_5_mile_hold(self):
        source, stand_in, clock = make(storm_reply(), T_STORM)
        l = source.blocks()["lightning"]
        self.assertIs(l["within_10mi"], True)
        self.assertIs(l["within_5mi"], True)
        self.assertIs(l["frequent"], False)  # 1 strike in the last minute
        self.assertEqual(l["closest_mi"], 3.5)
        self.assertEqual(l["hold_until_5mi"], "2026-11-01T15:21:32Z")
        self.assertEqual(l["hold_until_10mi"], "2026-11-01T15:21:32Z")
        # The storm moves off: Sentinel's window empties, the console's hold remains.
        stand_in.reply = copy.deepcopy(LIVE)
        clock.now = T_STORM + timedelta(minutes=15)
        l = source.blocks()["lightning"]
        self.assertIs(l["within_10mi"], False)
        self.assertIs(l["within_5mi"], True)
        clock.now = T_STORM + timedelta(minutes=31)
        self.assertIs(source.blocks()["lightning"]["within_5mi"], False)

    def test_strike_beyond_5_miles_starts_no_hold(self):
        reply = storm_reply()
        reply["lightning"]["closest_distance_mi"] = 7.0
        reply["lightning"]["close"]["last_distance_mi"] = 7.0
        source, _, _ = make(reply, T_STORM)
        l = source.blocks()["lightning"]
        self.assertIs(l["within_10mi"], True)
        self.assertIs(l["within_5mi"], False)

    def test_frequent_and_missing_field(self):
        reply = storm_reply()
        reply["lightning"]["close"]["strikes_last_minute"] = 6
        source, _, _ = make(reply, T_STORM)
        self.assertIs(source.blocks()["lightning"]["frequent"], True)
        del reply["lightning"]["close"]["strikes_last_minute"]  # an older Sentinel server
        source, _, _ = make(reply, T_STORM)
        self.assertIsNone(source.blocks()["lightning"]["frequent"])  # unknown, not False


class Simulation(unittest.TestCase):

    def test_nothing_passes_through_in_test_mode(self):
        source, stand_in, clock = make(storm_reply(simulation=True), T_STORM)
        w = source.blocks()
        for name in ("alerts", "conditions", "lightning"):
            self.assertEqual(w[name]["status"], "test", name)
            self.assertEqual(w[name]["message"], "Home weather station in test mode")
        self.assertEqual(w["alerts"]["items"], [])
        for flag in ("within_10mi", "within_5mi", "frequent", "hold_until_5mi"):
            self.assertIsNone(w["lightning"][flag], flag)
        # Simulation ends: the simulated 3.5 mi strike must not have started a hold.
        stand_in.reply = copy.deepcopy(LIVE)
        clock.now = T_STORM + timedelta(minutes=2)
        self.assertIs(source.blocks()["lightning"]["within_5mi"], False)


class Failures(unittest.TestCase):

    def test_never_reached(self):
        source, _, _ = make(urllib.error.URLError("refused"), T_LIVE)
        w = source.blocks()
        self.assertEqual(w["alerts"]["status"], "unavailable")
        self.assertEqual(w["alerts"]["items"], [])
        self.assertIn("not answering", w["alerts"]["message"])
        self.assertEqual(w["conditions"]["status"], "unavailable")
        self.assertIsNone(w["conditions"]["temperature_f"])
        self.assertIsNone(w["lightning"]["within_10mi"])

    def test_address_not_set(self):
        source, _, _ = make(LIVE, T_LIVE)
        source.settings["sources"]["sentinel_url"] = ""
        self.assertIn("not set", source.blocks()["alerts"]["message"])

    def test_goes_stale_then_alerts_unavailable(self):
        source, stand_in, clock = make(storm_reply(), T_STORM)
        source.blocks()
        stand_in.reply = urllib.error.URLError("timed out")
        clock.now = T_STORM + timedelta(minutes=12)
        w = source.blocks()
        self.assertEqual(w["alerts"]["status"], "stale")
        self.assertEqual(len(w["alerts"]["items"]), 3)  # last good list, greyed
        self.assertEqual(w["conditions"]["status"], "stale")
        clock.now = T_STORM + timedelta(minutes=32)
        w = source.blocks()
        self.assertEqual(w["alerts"]["status"], "unavailable")
        self.assertEqual(w["alerts"]["items"], [])  # never show a 30-minute-old list

    def test_sentinel_cannot_reach_nws(self):
        reply = copy.deepcopy(LIVE)
        reply["nws"]["available"] = False
        source, _, _ = make(reply, T_LIVE)
        a = source.blocks()["alerts"]
        self.assertEqual((a["status"], a["message"]), ("stale", "Weather Sentinel can't reach NWS"))

    def test_tempest_not_reporting(self):
        reply = copy.deepcopy(LIVE)
        reply["tempest"]["available"] = False
        source, _, _ = make(reply, T_LIVE)
        self.assertEqual(source.blocks()["conditions"]["status"], "stale")

    def test_missing_blocks_are_unknown_not_all_clear(self):
        source, _, _ = make({"simulation": False}, T_LIVE)
        w = source.blocks()
        self.assertEqual(w["alerts"]["status"], "unavailable")
        self.assertIn("no NWS alert information", w["alerts"]["message"])
        self.assertEqual(w["conditions"]["status"], "unavailable")
        self.assertEqual(w["lightning"]["status"], "unavailable")
        self.assertIsNone(w["lightning"]["within_5mi"])

    def test_odd_values_dont_crash(self):
        reply = copy.deepcopy(LIVE)
        reply["nws"]["alerts"] = ["junk", {"no": "event"}, {"event": "Tornado Warning", "expires": "soon"}]
        reply["tempest"]["temperature_f"] = "warm"
        reply["lightning"]["close"] = "nope"
        source, _, _ = make(reply, T_LIVE)
        w = source.blocks()
        self.assertEqual([i["event"] for i in w["alerts"]["items"]], ["Tornado Warning"])
        self.assertIsNone(w["conditions"]["temperature_f"])
        self.assertIsNone(w["lightning"]["within_10mi"])

    def test_not_a_json_object(self):
        source, _, _ = make(["not", "an", "object"], T_LIVE)
        self.assertIn("unexpected", source.blocks()["alerts"]["message"])


class FieldMode(unittest.TestCase):

    def test_not_built_yet(self):
        source, stand_in, _ = make(LIVE, T_LIVE, mode="field")
        w = source.blocks()
        self.assertEqual(w["alerts"]["status"], "unavailable")
        self.assertEqual(w["alerts"]["items"], [])
        self.assertIsNone(w["lightning"])
        self.assertEqual(stand_in.calls, 0)  # never asks the home station in Field Mode


class Endpoint(unittest.TestCase):

    def test_api_weather(self):
        try:
            from hamops_console.app import create_app
        except ImportError:
            self.skipTest("Flask not installed")
        source, _, _ = make(LIVE, T_LIVE)
        dashboard = tempfile.mkdtemp()
        client = create_app(source.settings, weather=source, dashboard_dir=dashboard).test_client()
        r = client.get("/api/weather")
        body = r.get_json()
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(body["mode"], "home")
        self.assertEqual(body["alerts"]["status"], "ok")
        self.assertEqual(body["conditions"]["temperature_f"], 69.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
