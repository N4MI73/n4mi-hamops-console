#!/usr/bin/env python3
"""Tests for Field Mode propagation, the field engine copy and its version check (2026-11-17).

No network: HamQSL and NOAA are replaced by live replies saved 2026-11-17 (tests/data/), and
the clock is fixed. The engine needs python3-astral; without it these tests are skipped
except the ones that check the service copes.

    python server/tests/test_field_propagation.py      (or: python -m pytest server/tests)
"""

import copy
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from hamops_console import engine_check, field_propagation as fp, settings as settings_mod  # noqa: E402

DATA = os.path.join(HERE, "data")
with open(os.path.join(DATA, "hamqsl_2026-11-17.xml"), "rb") as f:
    HAMQSL = f.read()
with open(os.path.join(DATA, "noaa_kp_2026-11-17.json"), "rb") as f:
    KP = f.read()

GRID = "FM18lv"                                                  # neutral test grid (Washington DC)
NOON = datetime(2026, 11, 17, 17, 0, tzinfo=timezone.utc)        # midday Eastern
NIGHT = datetime(2026, 11, 18, 4, 0, tzinfo=timezone.utc)        # 11 PM Eastern
HAVE_ENGINE = fp.engine is not None


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Net:
    """Plays HamQSL and NOAA; a value that is an exception is raised."""

    def __init__(self, hamqsl=HAMQSL, kp=KP):
        self.hamqsl, self.kp, self.urls = hamqsl, kp, []

    def __call__(self, url):
        self.urls.append(url)
        reply = self.hamqsl if url == fp.HAMQSL_URL else self.kp
        if isinstance(reply, Exception):
            raise reply
        return reply


def make(net, now, profile="general_hf", grid=GRID):
    settings = settings_mod.load(os.path.join(tempfile.mkdtemp(), "missing.json"))
    settings["mode"] = "field"
    settings["profile"] = profile
    settings["station"]["field"]["grid"] = grid
    clock = Clock(now)
    return fp.FieldPropagationSource(settings, fetch=net, clock=clock, run=lambda job: job(),
                                     zone=lambda: "America/New_York"), clock


class Parsing(unittest.TestCase):

    def test_hamqsl(self):
        f = fp.parse_hamqsl(HAMQSL)
        self.assertEqual((f["sfi"], f["k_index"], f["a_index"], f["sunspots"]), (123, 1, 3, 145))
        self.assertEqual((f["xray"], f["solar_wind_kms"]), ("C2.0", 362.6))
        self.assertEqual(f["updated"], datetime(2026, 11, 17, 20, 46, tzinfo=timezone.utc))

    def test_hamqsl_missing_and_bad(self):
        f = fp.parse_hamqsl(b"<solar><solardata><solarflux></solarflux><kindex>x</kindex></solardata></solar>")
        self.assertIsNone(f["sfi"])
        self.assertIsNone(f["k_index"])
        with self.assertRaises(ValueError):
            fp.parse_hamqsl(b"<solar></solar>")

    def test_kp_both_shapes(self):
        kp = fp.parse_kp(KP)
        self.assertEqual(len(kp), 62)
        self.assertEqual(kp[-1], (datetime(2026, 11, 17, 15, tzinfo=timezone.utc), 1.67))
        old = json.dumps([["time_tag", "Kp"], ["2026-11-17 12:00:00.000", "5.33"]]).encode()
        self.assertEqual(fp.parse_kp(old), [(datetime(2026, 11, 17, 12, tzinfo=timezone.utc), 5.33)])
        with self.assertRaises(ValueError):
            fp.parse_kp(b"[]")


@unittest.skipUnless(HAVE_ENGINE, "python3-astral not installed")
class Rating(unittest.TestCase):

    def test_dx_scale_all_ten_bands(self):
        src, _ = make(Net(), NOON)
        b = src.block()
        self.assertEqual(b["status"], "ok")
        self.assertEqual((b["scale"], b["model_version"], b["ratings_available"]), ("dx", "2026-11-17-v3", True))
        self.assertEqual(len(b["bands"]), 10)
        self.assertEqual((b["period"], b["sfi"], b["k_index"], b["rated_for"]), ("day", 123, 1, "FM18"))
        self.assertEqual(b["source"], "HamQSL + NOAA SWPC")
        self.assertEqual(b["recovery_active"], False)
        self.assertIn("FT8", b["meanings"]["fair"])
        order = [{"good": 0, "fair": 1, "poor": 2}[x["rating"]] for x in b["bands"]]
        self.assertEqual(order, sorted(order))                # good, fair, poor

    def test_field_day_scale_with_profile(self):
        src, _ = make(Net(), NOON, profile="field_day")
        b = src.block()
        self.assertEqual((b["scale"], b["scale_version"]), ("field_day", "2026-11-17-fd1"))
        self.assertEqual(sorted(x["band"] for x in b["bands"]),
                         sorted(["160m", "80m", "40m", "20m", "15m", "10m", "6m"]))
        six = next(x for x in b["bands"] if x["band"] == "6m")
        self.assertEqual(six["rating"], "poor")
        self.assertIn("Sporadic E", six["note"])
        self.assertEqual(next(x for x in b["bands"] if x["band"] == "160m")["rating"], "poor")   # daytime
        self.assertIn("North America", b["meanings"]["good"])

    def test_same_figures_rate_by_time_of_day(self):
        src, clock = make(Net(), NOON)
        day = {x["band"]: x["rating"] for x in src.block()["bands"]}
        clock.now = NIGHT                                      # same figures, no new fetch due
        night_block = src.block()
        night = {x["band"]: x["rating"] for x in night_block["bands"]}
        self.assertEqual(night_block["period"], "night")
        self.assertNotEqual(day, night)
        self.assertEqual((day["160m"], night["160m"]), ("poor", "good"))

    def test_matches_engine_directly(self):
        src, _ = make(Net(), NOON)
        b = src.block()
        lat, lon = 38.8958, -77.0417                          # FM18lv centre
        r = fp.engine.rate_all(123, 1, fp.parse_kp(KP), NOON, lat, lon, "America/New_York", src.model)
        self.assertEqual({x["band"]: x["rating"] for x in b["bands"]},
                         {x["band"]: x["status"] for x in r["bands"]})

    def test_storm_recovery_hold(self):
        storm = json.dumps([{"time_tag": "2026-11-17T09:00:00", "Kp": 5.33}]).encode()
        src, _ = make(Net(kp=storm), NOON)
        b = src.block()
        self.assertTrue(b["recovery_active"])
        self.assertEqual(b["recovery_ends"], "2026-11-17T21:00:00Z")

    def test_no_kp_history_says_so(self):
        src, _ = make(Net(kp=urllib.error.URLError("offline")), NOON)
        b = src.block()
        self.assertEqual(b["status"], "ok")
        self.assertIsNone(b["recovery_active"])
        self.assertIn("no storm-recovery hold", b["note"])

    def test_failed_kp_fetch_keeps_the_storm_hold(self):
        storm = json.dumps([{"time_tag": "2026-11-17T09:00:00", "Kp": 6.0}]).encode()
        net = Net(kp=storm)
        src, clock = make(net, NOON)
        self.assertTrue(src.block()["recovery_active"])
        net.kp = urllib.error.URLError("offline")
        clock.now += timedelta(minutes=11)                  # next fetch: HamQSL ok, NOAA fails
        b = src.block()
        self.assertTrue(b["recovery_active"])               # the known storm still holds
        self.assertNotIn("note", b)
        clock.now += timedelta(hours=3, minutes=50)         # history now over 3 h old (hold ended 21:00Z)
        b = src.block()
        self.assertIn("NOAA storm history 4 h old", b["note"])

    def test_bad_zone_name_falls_back(self):
        src, _ = make(Net(), NOON)
        src._zone = lambda: "Not/AZone"
        self.assertEqual(src.block()["status"], "ok")

    def test_missing_sfi_is_unavailable_not_poor(self):
        src, _ = make(Net(hamqsl=HAMQSL.replace(b"<solarflux>123</solarflux>", b"<solarflux></solarflux>")), NOON)
        b = src.block()
        self.assertEqual((b["ratings_available"], b["bands"]), (False, []))

    def test_never_reached_then_stale_then_unavailable(self):
        net = Net(hamqsl=urllib.error.URLError("offline"))
        src, clock = make(net, NOON)
        b = src.block()
        self.assertEqual(b["status"], "unavailable")
        self.assertIn("HamQSL not answering", b["message"])
        self.assertEqual(b["bands"], [])
        net.hamqsl = HAMQSL
        clock.now += timedelta(minutes=3)                     # retry after a failure
        self.assertEqual(src.block()["status"], "ok")
        net.hamqsl = urllib.error.URLError("offline")
        clock.now += timedelta(minutes=16)
        b = src.block()
        self.assertEqual(b["status"], "stale")
        self.assertEqual(len(b["bands"]), 10)                 # last figures, marked stale
        self.assertIn("HamQSL not answering", b["message"])

    def test_fetch_interval(self):
        net = Net()
        src, clock = make(net, NOON)
        src.block()
        clock.now += timedelta(minutes=9)
        src.block()
        self.assertEqual(net.urls.count(fp.HAMQSL_URL), 1)
        clock.now += timedelta(minutes=2)
        src.block()
        self.assertEqual(net.urls.count(fp.HAMQSL_URL), 2)

    def test_bad_grid(self):
        net = Net()
        src, _ = make(net, NOON, grid="")
        self.assertEqual(src.block()["status"], "unavailable")

    def test_site_time_zone_from_forecast(self):
        class Forecast:
            def time_zone(self, grid):
                return "America/Anchorage"
        src, _ = make(Net(), NOON)
        src.forecast = Forecast()
        src.settings["station"]["field"]["grid"] = "BP51"     # Anchorage
        b = src.block()
        self.assertEqual(b["rated_for"], "BP51")
        self.assertEqual(b["period"], "night")                # 8 AM Alaska time: before the dawn window


class EngineCopy(unittest.TestCase):
    """Ham Shack Automation's own 200 test vectors against the console's copy."""

    @unittest.skipUnless(HAVE_ENGINE, "python3-astral not installed")
    def test_vectors(self):
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "hamops_console", "fieldengine"))
        import test_field_engine as t
        t.test_vectors()
        t.test_field_day_vectors()
        t.test_bad_scale()
        t.test_unavailable()

    def test_model_file_versions(self):
        m = fp.load_model()
        self.assertEqual((m["model_version"], m["field_day"]["version"]), ("2026-11-17-v3", "2026-11-17-fd1"))


class VersionCheck(unittest.TestCase):

    def setUp(self):
        self.settings = settings_mod.load(os.path.join(tempfile.mkdtemp(), "missing.json"))
        self.model = fp.load_model()
        self.published = json.dumps({"model_version": "2026-11-17-v3",
                                     "field_day": {"version": "2026-11-17-fd1"}}).encode()

    def check(self, reply):
        return engine_check.EngineCheck(self.settings, self.model, fetch=lambda url: reply if not isinstance(reply, Exception) else (_ for _ in ()).throw(reply),
                                        run=lambda job: job())

    def test_current(self):
        c = self.check(self.published)
        c.note_propmon("2026-11-17-v3")
        b = c.block()
        self.assertEqual((b["status"], b["message"]), ("current", None))

    def test_propmon_newer(self):
        c = self.check(self.published)
        c.note_propmon("2026-12-02-v4")
        b = c.block()
        self.assertEqual(b["status"], "out_of_date")
        self.assertIn("model 2026-12-02-v4", b["message"])

    def test_field_day_scale_newer(self):
        c = self.check(json.dumps({"model_version": "2026-11-17-v3", "field_day": {"version": "2027-06-01-fd2"}}).encode())
        b = c.block()
        self.assertEqual(b["status"], "out_of_date")
        self.assertIn("Field Day scale 2027-06-01-fd2", b["message"])

    def test_older_propmon_is_not_out_of_date(self):
        c = self.check(self.published)
        c.note_propmon("2026-11-16-v2")                     # PropMon not redeployed yet
        b = c.block()
        self.assertEqual((b["status"], b["message"]), ("current", None))

    def test_published_behind_this_copy_is_current(self):
        b = self.check(json.dumps({"model_version": "2026-11-01-v2", "field_day": {"version": "2026-11-17-fd1"}}).encode()).block()
        self.assertEqual(b["status"], "current")

    def test_unknown_offline(self):
        b = self.check(urllib.error.URLError("offline")).block()
        self.assertEqual((b["status"], b["message"]), ("unknown", None))
        self.assertEqual(b["model_version"], "2026-11-17-v3")

    def test_never_switches_the_model(self):
        c = self.check(json.dumps({"model_version": "2027-01-01-v9"}).encode())
        c.block()
        self.assertEqual(c.mine[0], "2026-11-17-v3")
        self.assertEqual(fp.load_model()["model_version"], "2026-11-17-v3")


class ModeSwitch(unittest.TestCase):

    def test_home_uses_propmon_and_feeds_the_check(self):
        settings = settings_mod.load(os.path.join(tempfile.mkdtemp(), "missing.json"))

        class Home:
            def block(self):
                return {"source": "PropMon", "model_version": "2026-12-02-v4", "bands": []}

        class Field:
            def block(self):
                raise AssertionError("field source asked in Home Mode")

        check = engine_check.EngineCheck(settings, fp.load_model(), fetch=lambda u: (_ for _ in ()).throw(OSError()),
                                         run=lambda job: job())
        b = fp.ModePropagation(settings, Home(), Field(), check).block()
        self.assertEqual(b["source"], "PropMon")
        self.assertEqual(b["engine"]["status"], "out_of_date")


if __name__ == "__main__":
    unittest.main(verbosity=2)
