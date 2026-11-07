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

PROPMON_SAMPLE = {
    "a_index": 19, "alerts": [{"category": "tower", "level": "NONE", "message": "No active alerts"}],
    "bands": [{"band": "160m", "status": "poor"}, {"band": "80m", "status": "poor"},
              {"band": "40m", "status": "poor"}, {"band": "30m", "status": "fair"},
              {"band": "20m", "status": "fair"}, {"band": "17m", "status": "fair"},
              {"band": "15m", "status": "poor"}, {"band": "12m", "status": "poor"},
              {"band": "10m", "status": "poor"}, {"band": "6m", "status": "poor"}],
    "k_index": 1, "sfi": 95, "solar_wind": 393.9, "summary": "FAIR: 30m 20m 17m", "sunspots": 37,
    "tower_status": "NONE", "updated": "2026-11-02T20:11:06Z", "xray": "B4.3",
}
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
        self.assertEqual(s["thresholds"]["propagation_stale_min"], 30)

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
        self.assertNotIn("tower_status", d)
        self.assertNotIn("alerts", d)

    def test_good_bands_come_first(self):
        raw = {"bands": [{"band": "40m", "status": "poor"}, {"band": "17m", "status": "good"},
                         {"band": "20m", "status": "good"}, {"band": "15m", "status": "fair"},
                         {"band": "6m", "status": "weird"}]}
        self.assertEqual([b["band"] for b in translate(raw)["bands"]], ["20m", "17m", "15m", "40m", "6m"])
        self.assertIsNone(translate(raw)["bands"][-1]["rating"])

    def test_translate_survives_garbage(self):
        d = translate({"sfi": "high", "k_index": True, "bands": [None, {"status": "good"}, "x"]})
        self.assertIsNone(d["sfi"])
        self.assertIsNone(d["k_index"])
        self.assertEqual(d["bands"], [])
        self.assertEqual(translate(None)["bands"], [])

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
        for key in ("hostname", "cpu_temp_c", "memory_used_pct", "power", "network", "clock", "gps"):
            self.assertIn(key, b)


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
