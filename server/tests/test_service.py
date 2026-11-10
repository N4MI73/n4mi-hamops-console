#!/usr/bin/env python3
"""Tests for the console service's first step: settings, freshness, the
propagation block (PropMon relay) and the HTTP endpoints.

No network, no Pi: PropMon is replaced by a stand-in, and the clock is fixed.
Needs Flask (on the Pi: the python3-flask package).

    python server/tests/test_service.py      (or: python -m pytest server/tests)

The PropMon reply below is the live sample taken 2026-11-02.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from hamops_console import freshness, settings as settings_mod, system_info  # noqa: E402
from hamops_console.app import create_app  # noqa: E402
from hamops_console.propagation import PropagationSource, translate  # noqa: E402

# PropMon v2 (`/api/v2/conditions`), shaped like the live reply of 2026-11-07
# (tower block trimmed; the console doesn't read it).
PROPMON_SAMPLE = {
    "version": 2, "source": "hamqsl", "period": "day", "summary": "FAIR: 30m 20m 17m",
    "updated": "2026-11-02T20:11:06Z",
    "bands": [{"band": "160m", "status": "poor", "tod": "day"}, {"band": "80m", "status": "poor", "tod": "day"},
              {"band": "40m", "status": "poor", "tod": "day"}, {"band": "30m", "status": "fair", "tod": "day"},
              {"band": "20m", "status": "fair", "tod": "day"}, {"band": "17m", "status": "fair", "tod": "day"},
              {"band": "15m", "status": "poor", "tod": "day"}, {"band": "12m", "status": "poor", "tod": "day"},
              {"band": "10m", "status": "poor", "tod": "day"}, {"band": "6m", "status": "poor", "tod": "day"}],
    "solar": {"a_index": 19, "k_index": 1, "sfi": 95, "solar_wind": 393.9, "sunspots": 37, "xray": "B4.3",
              "bz": 1.2, "hamqsl_updated": "02 Nov 2026 2000 GMT"},
    "geomagnetic": {"penalty": 1.0, "recovery_active": False, "recovery_ends": None},
    "tower": {"status": "NONE", "wind_gust_mph": 0.0},
}
# The live 2026-11-07 storm reply: K 5, every band poor from HamQSL (real), recovery on.
PROPMON_STORM = dict(
    PROPMON_SAMPLE, period="dusk", summary="POOR: all bands", updated="2026-11-07T22:40:53Z",
    bands=[{"band": b, "status": "poor", "tod": "dusk"} for b in
           ("160m", "80m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m")],
    solar={"a_index": 23, "k_index": 5, "sfi": 127, "solar_wind": 684.5, "sunspots": 93, "xray": "C5.3"},
    geomagnetic={"penalty": 0.45, "recovery_active": True, "recovery_ends": "2026-11-08T06:00:00Z"},
)
T0 = datetime(2026, 11, 2, 20, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def make_settings(**sources):
    s = settings_mod.load(os.path.join(tempfile.mkdtemp(), "missing.json"))
    s["sources"].update(sources)
    return s


class Settings(unittest.TestCase):

    def test_missing_file_gives_defaults(self):
        s = settings_mod.load(os.path.join(tempfile.mkdtemp(), "nope.json"))
        self.assertEqual(s["mode"], "home")
        self.assertEqual(s["server"]["host"], "127.0.0.1")
        self.assertEqual(s["thresholds"]["propagation_stale_min"], 15)

    def test_partial_file_overlays_defaults_and_skips_comments(self):
        path = os.path.join(tempfile.mkdtemp(), "settings.json")
        with open(path, "w") as f:
            json.dump({"_comment": "x", "sources": {"propmon_url": "http://example:8076"},
                       "thresholds": {"propagation_stale_min": 45}}, f)
        s = settings_mod.load(path)
        self.assertEqual(s["sources"]["propmon_url"], "http://example:8076")
        self.assertEqual(s["thresholds"]["propagation_stale_min"], 45)
        self.assertEqual(s["thresholds"]["nws_alerts_stale_min"], 10)
        self.assertNotIn("_comment", s)

    def test_bad_file_and_bad_mode_fall_back(self):
        path = os.path.join(tempfile.mkdtemp(), "settings.json")
        with open(path, "w") as f:
            f.write("{ not json")
        self.assertEqual(settings_mod.load(path)["mode"], "home")
        with open(path, "w") as f:
            json.dump({"mode": "camping"}, f)
        self.assertEqual(settings_mod.load(path)["mode"], "home")

    def test_example_file_in_repo_loads(self):
        example = os.path.join(os.path.dirname(os.path.dirname(HERE)), "config", "settings.example.json")
        s = settings_mod.load(example)
        self.assertEqual(s["station"]["field"]["grid"], "AA00aa")


class Freshness(unittest.TestCase):

    def test_never_fetched_is_unavailable(self):
        b = freshness.block("PropMon", None, None, 1800, now=T0)
        self.assertEqual(b["status"], "unavailable")
        self.assertIsNone(b["age_sec"])
        self.assertTrue(b["message"])

    def test_age_counts_from_issued_time(self):
        issued = T0 - timedelta(minutes=10)
        b = freshness.block("PropMon", T0, issued, 1800, now=T0)
        self.assertEqual((b["status"], b["age_sec"]), ("ok", 600))
        self.assertEqual(b["issued_at"], "2026-11-02T20:02:00Z")

    def test_old_data_is_stale_with_reason(self):
        b = freshness.block("PropMon", T0 - timedelta(minutes=40), None, 1800, now=T0)
        self.assertEqual(b["status"], "stale")
        self.assertIn("40 min", b["message"])

    def test_test_mode(self):
        self.assertEqual(freshness.block("X", T0, None, 60, now=T0, test=True)["status"], "test")

    def test_parse_time_variants(self):
        self.assertEqual(freshness.parse_time("2026-11-02T20:11:06Z"),
                         datetime(2026, 11, 2, 20, 11, 6, tzinfo=timezone.utc))
        self.assertIsNotNone(freshness.parse_time("2026-11-02T20:12:34.812412+00:00"))
        self.assertIsNone(freshness.parse_time("yesterday"))
        self.assertIsNone(freshness.parse_time(None))


class Propagation(unittest.TestCase):

    def test_translate_renames_and_orders_bands(self):
        d = translate(PROPMON_SAMPLE)
        self.assertEqual((d["sfi"], d["k_index"], d["a_index"], d["xray"]), (95, 1, 19, "B4.3"))
        self.assertEqual(d["solar_wind_kms"], 393.9)
        order = [(b["band"], b["rating"]) for b in d["bands"]]
        self.assertEqual(order[:3], [("30m", "fair"), ("20m", "fair"), ("17m", "fair")])
        self.assertEqual([b for b, _ in order[3:]], ["160m", "80m", "40m", "15m", "12m", "10m", "6m"])
        self.assertNotIn("tower", d)
        self.assertEqual((d["period"], d["ratings_available"], d["recovery_active"]), ("day", True, False))
        self.assertIsNone(d["recovery_ends"])

    def test_storm_all_poor_is_real_with_recovery(self):
        d = translate(PROPMON_STORM)
        self.assertTrue(d["ratings_available"], "HamQSL-based all-poor is a real rating")
        self.assertEqual({b["rating"] for b in d["bands"]}, {"poor"})
        self.assertEqual((d["k_index"], d["period"]), (5, "dusk"))
        self.assertEqual((d["recovery_active"], d["recovery_ends"]), (True, "2026-11-08T06:00:00Z"))
        self.assertEqual(d["summary"], "POOR: all bands")

    def test_noaa_fallback_means_ratings_unavailable(self):
        for raw in (dict(PROPMON_SAMPLE, source="noaa"), dict(PROPMON_SAMPLE, summary="Data unavailable")):
            d = translate(raw)
            self.assertFalse(d["ratings_available"])
            self.assertEqual({b["rating"] for b in d["bands"]}, {None}, "no band shown as poor")
            self.assertEqual(len(d["bands"]), 10)
            self.assertIsNone(d["summary"])
            self.assertEqual(d["sfi"], 95, "solar figures still shown")

    def test_odd_period_and_recovery_ignored(self):
        d = translate(dict(PROPMON_SAMPLE, period="afternoon",
                           geomagnetic={"recovery_active": "yes", "recovery_ends": "soon"}))
        self.assertIsNone(d["period"])
        self.assertIsNone(d["recovery_active"])
        self.assertIsNone(d["recovery_ends"])

    def test_good_bands_come_first(self):
        raw = {"source": "hamqsl", "bands": [{"band": "40m", "status": "poor"}, {"band": "17m", "status": "good"},
                         {"band": "20m", "status": "good"}, {"band": "15m", "status": "fair"},
                         {"band": "6m", "status": "weird"}]}
        self.assertEqual([b["band"] for b in translate(raw)["bands"]], ["20m", "17m", "15m", "40m", "6m"])
        self.assertIsNone(translate(raw)["bands"][-1]["rating"])

    def test_translate_survives_garbage(self):
        d = translate({"solar": {"sfi": "high", "k_index": True}, "bands": [None, {"status": "good"}, "x"]})
        self.assertIsNone(d["sfi"])
        self.assertIsNone(d["k_index"])
        self.assertEqual(d["bands"], [])
        self.assertEqual(translate(None)["bands"], [])
        self.assertIsNone(translate({"solar": "x", "geomagnetic": []})["sfi"])

    def test_source_fetches_lazily_and_keeps_last_good(self):
        calls = []
        clock = Clock(T0)

        def fetch(base):
            calls.append(base)
            if len(calls) == 2:
                raise OSError("down")
            return dict(PROPMON_SAMPLE, updated=freshness.iso(clock.now))  # PropMon issues fresh data

        src = PropagationSource(make_settings(propmon_url="http://propmon:8076"), fetch=fetch, clock=clock)
        b = src.block()
        self.assertEqual((b["status"], b["sfi"], len(calls)), ("ok", 95, 1))
        clock.now = T0 + timedelta(minutes=2)
        src.block()
        self.assertEqual(len(calls), 1, "no new fetch inside the 5-minute interval")
        clock.now = T0 + timedelta(minutes=6)
        b = src.block()
        self.assertEqual(len(calls), 2)
        self.assertEqual((b["status"], b["sfi"]), ("ok", 95), "last good data kept while young")
        clock.now = T0 + timedelta(minutes=45)
        b = src.block()
        self.assertEqual(len(calls), 3)
        self.assertEqual(b["status"], "ok", "third fetch succeeded")
        # PropMon stops answering for good: data ages into stale, values still shown.
        src._fetch = lambda base: (_ for _ in ()).throw(OSError("down"))
        clock.now = T0 + timedelta(minutes=90)
        b = src.block()
        self.assertEqual((b["status"], b["sfi"]), ("stale", 95))
        self.assertIn("not answering", b["message"])

    def test_fresh_for_15_minutes(self):
        clock = Clock(datetime(2026, 11, 2, 20, 11, 6, tzinfo=timezone.utc))
        src = PropagationSource(make_settings(propmon_url="http://propmon:8076"),
                                fetch=lambda base: PROPMON_SAMPLE, clock=clock)
        clock.now += timedelta(minutes=14)
        self.assertEqual(src.block()["status"], "ok")
        clock.now += timedelta(minutes=2)
        self.assertEqual(src.block()["status"], "stale", "PropMon's issue time is 16 min old")

    def test_no_data_yet_after_propmon_restart(self):
        from hamops_console.propagation import NoDataYet

        def fetch(base):
            raise NoDataYet()

        b = PropagationSource(make_settings(propmon_url="http://propmon:8076"), fetch=fetch, clock=Clock(T0)).block()
        self.assertEqual(b["status"], "unavailable")
        self.assertIn("no data yet", b["message"])
        self.assertIsNone(b["ratings_available"])

    def test_fetch_turns_503_into_no_data_yet(self):
        import urllib.error
        import urllib.request
        from unittest import mock
        from hamops_console import propagation

        err = urllib.error.HTTPError("http://propmon:8076/api/v2/conditions", 503, "Service Unavailable", {}, None)
        with mock.patch.object(urllib.request, "urlopen", side_effect=err) as opened:
            with self.assertRaises(propagation.NoDataYet):
                propagation.fetch_propmon("http://propmon:8076/")
        self.assertEqual(opened.call_args[0][0].full_url, "http://propmon:8076/api/v2/conditions")
        err404 = urllib.error.HTTPError("x", 404, "Not Found", {}, None)
        with mock.patch.object(urllib.request, "urlopen", side_effect=err404):
            with self.assertRaises(urllib.error.HTTPError):
                propagation.fetch_propmon("http://propmon:8076")

    def test_no_address_is_unavailable_with_reason(self):
        b = PropagationSource(make_settings(), fetch=lambda base: PROPMON_SAMPLE, clock=Clock(T0)).block()
        self.assertEqual(b["status"], "unavailable")
        self.assertIn("not set", b["message"])
        self.assertEqual(b["bands"], [])


class SystemInfo(unittest.TestCase):

    def test_throttle_decoding(self):
        self.assertEqual(system_info.throttle_flags("throttled=0x0"),
                         {"undervoltage_now": False, "throttled_now": False,
                          "undervoltage_since_boot": False, "throttled_since_boot": False})
        f = system_info.throttle_flags("throttled=0x50005")
        self.assertTrue(f["undervoltage_now"] and f["throttled_now"])
        self.assertTrue(f["undervoltage_since_boot"] and f["throttled_since_boot"])
        self.assertIsNone(system_info.throttle_flags("")["undervoltage_now"])

    def test_block_runs_anywhere(self):
        b = system_info.block("0.1.0", T0)
        self.assertEqual(b["status"], "ok")
        for key in ("hostname", "cpu_temp_c", "memory_used_pct", "memory_used_gb", "memory_total_gb",
                    "power", "network", "clock", "rtc", "gps"):
            self.assertIn(key, b)
        self.assertIn("wifi_signal_pct", b["network"])

    def test_memory(self):
        text = "MemTotal:        8245760 kB\nMemFree:         100000 kB\nMemAvailable:    7421184 kB\n"
        self.assertEqual(system_info.memory(text), (10, 0.8, 7.9))
        self.assertEqual(system_info.memory("junk"), (None, None, None))

    def test_wifi_signal_from_last_scan(self):
        self.assertEqual(system_info.wifi_signal(" :40\n*:72\n :18\n"), 72)
        self.assertIsNone(system_info.wifi_signal(" :40\n"))
        self.assertIsNone(system_info.wifi_signal(""))

    def test_rtc_battery(self):
        folder = tempfile.mkdtemp()
        with open(os.path.join(folder, "battery_voltage"), "w") as f:
            f.write("2985000\n")
        with open(os.path.join(folder, "charging_voltage"), "w") as f:
            f.write("3000000\n")
        self.assertEqual(system_info.rtc(folder), {"battery_v": 2.98, "charging": True})
        with open(os.path.join(folder, "charging_voltage"), "w") as f:
            f.write("0\n")
        self.assertFalse(system_info.rtc(folder)["charging"])
        self.assertEqual(system_info.rtc(tempfile.mkdtemp()), {"battery_v": None, "charging": None})


class Endpoints(unittest.TestCase):

    def setUp(self):
        dashboard = tempfile.mkdtemp()
        with open(os.path.join(dashboard, "index.html"), "w") as f:
            f.write("<!doctype html><title>test</title>")
        with open(os.path.join(dashboard, "check.html"), "w") as f:
            f.write("<title>check</title>")
        with open(os.path.join(dashboard, "weather.html"), "w") as f:
            f.write("<title>weather</title>")
        with open(os.path.join(dashboard, "propagation.html"), "w") as f:
            f.write("<title>propagation</title>")
        os.makedirs(os.path.join(dashboard, "fonts"))
        with open(os.path.join(dashboard, "fonts", "x.woff2"), "wb") as f:
            f.write(b"wOF2")
        src = PropagationSource(make_settings(propmon_url="http://propmon:8076"),
                                fetch=lambda base: PROPMON_SAMPLE, clock=Clock(T0))
        self.client = create_app(make_settings(), propagation=src, dashboard_dir=dashboard).test_client()

    def test_index_served(self):
        # "with" closes the file Flask opened for the page (otherwise a
        # harmless ResourceWarning on the Pi's Python).
        with self.client.get("/") as r:
            self.assertEqual(r.status_code, 200)
            self.assertIn(b"<title>test</title>", r.data)

    def test_check_page_and_static_files(self):
        with self.client.get("/check") as r:
            self.assertEqual(r.status_code, 200)
            self.assertIn(b"<title>check</title>", r.data)
        with self.client.get("/static/fonts/x.woff2") as r:
            self.assertEqual(r.status_code, 200)

    def test_weather_page_served(self):
        with self.client.get("/weather") as r:
            self.assertEqual(r.status_code, 200)
            self.assertIn(b"<title>weather</title>", r.data)

    def test_propagation_page_served(self):
        with self.client.get("/propagation") as r:
            self.assertEqual(r.status_code, 200)
            self.assertIn(b"<title>propagation</title>", r.data)

    def test_real_dashboard_files_exist(self):
        """The page, its script, style and every font the style names are in the repo."""
        import re
        root = os.path.join(os.path.dirname(os.path.dirname(HERE)), "dashboard")
        for name in ("index.html", "home.js", "home.css", "check.html",
                     "common.js", "weather.html", "weather.js", "weather.css",
                     "propagation.html", "propagation.js", "propagation.css"):
            self.assertTrue(os.path.exists(os.path.join(root, name)), name)
        with open(os.path.join(root, "home.css")) as f:
            fonts = re.findall(r'url\("([^"]+)"\)', f.read())
        self.assertEqual(len(fonts), 5)
        for font in fonts:
            self.assertTrue(os.path.exists(os.path.join(root, font)), font)

    def test_propagation_envelope(self):
        body = self.client.get("/api/propagation").get_json()
        self.assertEqual(body["mode"], "home")
        self.assertTrue(body["generated_at"].endswith("Z"))
        self.assertEqual(body["propagation"]["source"], "PropMon")
        self.assertEqual(body["propagation"]["bands"][0]["band"], "30m")

    def test_system_and_health(self):
        self.assertEqual(self.client.get("/api/system").get_json()["system"]["source"], "Pi")
        self.assertEqual(self.client.get("/healthz").get_json()["status"], "ok")
        self.assertEqual(self.client.get("/api/system").headers["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main(verbosity=2)
