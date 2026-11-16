#!/usr/bin/env python3
"""Tests for Field Mode weather: NWS alerts and the nearest station's observation (2026-11-16).

No network: NWS is replaced by a stand-in serving trimmed live replies from 2026-11-16
(tests/data/nws_field_samples.json), and the clock is fixed.

    python server/tests/test_field_weather.py      (or: python -m pytest server/tests)
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

from hamops_console import field_weather, freshness, settings as settings_mod  # noqa: E402
from hamops_console.field_weather import FieldWeather  # noqa: E402

with open(os.path.join(HERE, "data", "nws_field_samples.json"), encoding="utf-8") as f:
    SAMPLES = json.load(f)

GRID = "FM18lv"                                                # a neutral test grid (Washington DC)
T_ALERTS = datetime(2026, 11, 16, 21, 30, tzinfo=timezone.utc)  # after two of the three `expires`
T_OBS = datetime(2026, 11, 16, 13, 46, tzinfo=timezone.utc)     # when the samples were fetched
STATIONS_URL = "https://api.weather.gov/gridpoints/LWX/98,72/stations"


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class FakeForecast:
    def __init__(self, url=STATIONS_URL):
        self.url = url

    def stations_url(self, grid):
        return self.url


class NWS:
    """Plays api.weather.gov: replies by URL; a value that is an exception is raised."""

    def __init__(self, alerts=None, stations=None, observations=None):
        # NWS's `updated` (when the list was built) is the alerts' age; make it current.
        self.alerts = dict(SAMPLES["alerts"], updated="2026-11-16T21:29:00+00:00") if alerts is None else alerts
        self.stations = SAMPLES["stations"] if stations is None else stations
        self.observations = observations or {}
        self.urls = []

    def __call__(self, url, contact):
        self.urls.append(url)
        if "/alerts/active" in url:
            reply = self.alerts
        elif url == STATIONS_URL:
            reply = self.stations
        else:
            sid = url.split("/stations/")[1].split("/")[0]
            reply = self.observations.get(sid, urllib.error.URLError("no such station"))
        if isinstance(reply, Exception):
            raise reply
        return copy.deepcopy(reply)


def make(nws, now, grid=GRID, forecast=None):
    settings = settings_mod.load(os.path.join(tempfile.mkdtemp(), "missing.json"))
    settings["mode"] = "field"
    settings["station"]["field"]["grid"] = grid
    clock = Clock(now)
    return FieldWeather(settings, forecast or FakeForecast(), fetch=nws, clock=clock,
                        run=lambda job: job()), clock


def obs_at(when, **changes):
    o = copy.deepcopy(SAMPLES["observation"])
    o["properties"]["timestamp"] = freshness.iso(when)
    o["properties"].update(changes)
    return o


class Alerts(unittest.TestCase):

    def test_kept_past_expires_dropped_after_ends(self):
        source, _ = make(NWS(), T_ALERTS)
        block = source.alerts_block()
        self.assertEqual(block["status"], "ok")
        events = [i["event"] for i in block["items"]]
        # The watch and the advisory are past `expires` but not `ends`; the flood warning has ended.
        self.assertEqual(sorted(events), ["Wind Advisory", "Winter Storm Watch"])
        watch = next(i for i in block["items"] if i["event"] == "Winter Storm Watch")
        self.assertEqual(watch["ends"], "2026-11-20T12:00:00Z")
        self.assertEqual(watch["expires"], "2026-11-16T20:00:00Z")
        self.assertEqual((watch["kind"], watch["hazard_class"]), ("watch", "other"))
        self.assertTrue(watch["hazard"].startswith("For the Winter Weather Advisory"))

    def test_point_query_and_fetch_interval(self):
        nws = NWS()
        source, clock = make(nws, T_ALERTS)
        source.alerts_block()
        self.assertTrue(nws.urls[0].startswith("https://api.weather.gov/alerts/active?point="))
        clock.now += timedelta(seconds=90)
        source.alerts_block()
        self.assertEqual(len(nws.urls), 1)          # every 2 min, not on every request
        clock.now += timedelta(seconds=40)
        source.alerts_block()
        self.assertEqual(len(nws.urls), 2)

    def test_ends_from_event_ending_time_or_none(self):
        props = copy.deepcopy(SAMPLES["alerts"]["features"][0]["properties"])
        props["ends"] = None
        self.assertEqual(field_weather.translate_alert(props)["ends"], "2026-11-20T12:00:00Z")
        props["parameters"] = {}
        item = field_weather.translate_alert(props)
        self.assertIsNone(item["ends"])
        doc = {"features": [{"properties": props}]}
        self.assertEqual(len(field_weather.translate_alerts(doc, T_ALERTS + timedelta(days=30))), 1)

    def test_only_actual_and_not_cancel(self):
        base = SAMPLES["alerts"]["features"][0]["properties"]
        for change in ({"status": "Exercise"}, {"status": "Test"}, {"messageType": "Cancel"}, {"event": ""}):
            self.assertIsNone(field_weather.translate_alert(dict(base, **change)), change)
        self.assertIsNone(field_weather.translate_alert("junk"))

    def test_missing_status_kept_and_bad_ends_kept(self):
        base = dict(SAMPLES["alerts"]["features"][0]["properties"])
        base.pop("status")
        self.assertIsNotNone(field_weather.translate_alert(base))
        doc = {"features": [{"properties": dict(base, ends="soon", parameters={})}]}
        self.assertEqual(len(field_weather.translate_alerts(doc, T_ALERTS)), 1)   # unreadable end: kept

    def test_new_grid_never_shows_old_places_alerts(self):
        nws = NWS()
        source, clock = make(nws, T_ALERTS)
        self.assertEqual(len(source.alerts_block()["items"]), 2)
        source.settings["station"]["field"]["grid"] = "EM73"
        nws.alerts = urllib.error.URLError("offline")
        block = source.alerts_block()
        self.assertEqual((block["status"], block["items"]), ("unavailable", []))

    def test_unexpected_error_still_ends_the_attempt(self):
        nws = NWS(alerts=RuntimeError("surprise"))
        source, clock = make(nws, T_ALERTS)
        self.assertEqual(source.alerts_block()["status"], "unavailable")
        self.assertFalse(source._alerts["busy"])
        nws.alerts = NWS().alerts
        clock.now += timedelta(minutes=3)
        self.assertEqual(source.alerts_block()["status"], "ok")

    def test_no_second_fetch_while_one_is_running(self):
        jobs = []
        nws = NWS()
        source, _ = make(nws, T_ALERTS)
        source._run = jobs.append            # hold the job instead of running it
        source.alerts_block()
        source._clock.now += timedelta(minutes=5)
        block = source.alerts_block()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(block["message"], "Fetching NWS alerts")

    def test_empty_list_is_ok_not_unknown(self):
        source, _ = make(NWS(alerts={"type": "FeatureCollection", "features": [],
                                     "updated": "2026-11-16T21:29:00+00:00"}), T_ALERTS)
        block = source.alerts_block()
        self.assertEqual((block["status"], block["items"]), ("ok", []))

    def test_never_reached_then_stale_then_unavailable(self):
        nws = NWS(alerts=urllib.error.URLError("offline"))
        source, clock = make(nws, T_ALERTS)
        block = source.alerts_block()
        self.assertEqual(block["status"], "unavailable")
        self.assertIn("NWS not answering", block["message"])
        nws.alerts = dict(SAMPLES["alerts"], updated=freshness.iso(T_ALERTS))
        clock.now += timedelta(minutes=3)            # the retry after a failure
        self.assertEqual(source.alerts_block()["status"], "ok")
        nws.alerts = urllib.error.URLError("offline")
        clock.now += timedelta(minutes=12)
        block = source.alerts_block()
        self.assertEqual(block["status"], "stale")
        self.assertEqual(len(block["items"]), 2)     # last good list still shown, marked stale
        clock.now += timedelta(minutes=20)
        block = source.alerts_block()
        self.assertEqual((block["status"], block["items"]), ("unavailable", []))

    def test_unexpected_reply(self):
        source, _ = make(NWS(alerts={"oops": 1}), T_ALERTS)
        block = source.alerts_block()
        self.assertEqual(block["status"], "unavailable")
        self.assertEqual(block["message"], "NWS sent no alert list")

    def test_bad_grid(self):
        nws = NWS()
        source, _ = make(nws, T_ALERTS, grid="")
        self.assertEqual(source.alerts_block()["status"], "unavailable")
        self.assertEqual(nws.urls, [])


class Conditions(unittest.TestCase):

    def test_nearest_station(self):
        nws = NWS(observations={"KDCA": SAMPLES["observation"]})
        source, _ = make(nws, T_OBS)
        c = source.conditions_block()
        self.assertEqual(c["status"], "ok")
        self.assertEqual(c["station"], {"id": "KDCA", "name": c["station"]["name"], "distance_mi": 4.6})
        self.assertEqual((c["temperature_f"], c["humidity_pct"], c["wind_dir"], c["gust_mph"]),
                         (42.8, 70, "N", None))
        self.assertEqual(c["feels_like_f"], 38.6)    # wind chill
        self.assertEqual((c["wind_mph"], c["pressure_inhg"], c["rain_rate_in_hr"]), (6.9, 30.48, None))
        self.assertEqual(c["issued_at"], "2026-11-16T13:10:00Z")

    def test_falls_back_to_next_station(self):
        old = obs_at(T_OBS - timedelta(hours=5))
        no_temp = obs_at(T_OBS - timedelta(minutes=20),
                         temperature={"unitCode": "wmoUnit:degC", "value": None, "qualityControl": "Z"})
        good = obs_at(T_OBS - timedelta(minutes=30))
        nws = NWS(observations={"KDCA": old, "KCGS": no_temp, "KADW": good})
        source, _ = make(nws, T_OBS)
        c = source.conditions_block()
        self.assertEqual((c["status"], c["station"]["id"], c["station"]["distance_mi"]), ("ok", "KADW", 9.9))

    def test_none_good_shows_freshest_found(self):
        nws = NWS(observations={"KDCA": obs_at(T_OBS - timedelta(hours=5)),
                                "KCGS": obs_at(T_OBS - timedelta(hours=3))})
        source, _ = make(nws, T_OBS)
        c = source.conditions_block()
        self.assertEqual((c["status"], c["station"]["id"]), ("stale", "KCGS"))
        self.assertIsNotNone(c["temperature_f"])

    def test_waits_for_the_nws_location(self):
        nws = NWS()
        source, _ = make(nws, T_OBS, forecast=FakeForecast(url=None))
        c = source.conditions_block()
        self.assertEqual(c["status"], "unavailable")
        self.assertIn("waiting for the forecast lookup", c["message"])
        self.assertIsNone(c["station"])

    def test_station_list_fetched_once_a_day(self):
        nws = NWS(observations={"KDCA": SAMPLES["observation"]})
        source, clock = make(nws, T_OBS)
        source.conditions_block()
        clock.now += timedelta(minutes=16)
        nws.observations["KDCA"] = obs_at(clock.now - timedelta(minutes=5))
        source.conditions_block()
        self.assertEqual(sum(1 for u in nws.urls if u == STATIONS_URL), 1)
        self.assertEqual(sum(1 for u in nws.urls if "observations/latest" in u), 2)

    def test_units_and_quality_control(self):
        o = SAMPLES["observation"]["properties"]
        bad = dict(o, temperature={"unitCode": "wmoUnit:degC", "value": 6, "qualityControl": "X"},
                   windSpeed={"unitCode": "wmoUnit:km_h-1", "value": 0, "qualityControl": "V"},
                   windChill={"unitCode": "wmoUnit:degC", "value": None, "qualityControl": "Z"},
                   heatIndex={"unitCode": "wmoUnit:degC", "value": None, "qualityControl": "V"},
                   relativeHumidity={"unitCode": "wmoUnit:degF", "value": 50})
        c = field_weather.translate_observation(bad)
        self.assertIsNone(c["temperature_f"])        # failed NWS quality control
        self.assertIsNone(c["feels_like_f"])
        self.assertEqual((c["wind_mph"], c["wind_dir"]), (0.0, None))   # calm: no direction
        self.assertIsNone(c["humidity_pct"])         # unexpected unit
        self.assertEqual(field_weather.translate_observation("junk")["temperature_f"], None)

    def test_station_distance_worked_out_when_missing(self):
        doc = copy.deepcopy(SAMPLES["stations"])
        for f in doc["features"]:
            f["properties"].pop("distance")
        found = field_weather.station_list(doc, (38.9, -77.0))
        self.assertEqual(found[0]["id"], "KDCA")
        self.assertAlmostEqual(found[0]["distance_mi"], 4.6, delta=1.0)   # NWS measures from its own grid cell


class InWeatherSource(unittest.TestCase):

    def test_field_mode_uses_nws_and_never_asks_sentinel(self):
        from hamops_console.forecast import ForecastSource
        from hamops_console.nwr import NwrSource
        from hamops_console.weather import LightningState, WeatherSource
        settings = settings_mod.load(os.path.join(tempfile.mkdtemp(), "missing.json"))
        settings["mode"] = "field"
        settings["station"]["field"]["grid"] = GRID
        settings["sources"]["sentinel_url"] = "http://sentinel.example:8085"
        clock = Clock(T_ALERTS)
        offline = lambda *a: (_ for _ in ()).throw(urllib.error.URLError("offline"))  # noqa: E731
        sentinel_calls = []
        nws = NWS()
        source = WeatherSource(
            settings, fetch=lambda *a: sentinel_calls.append(a) or {}, clock=clock,
            forecast=ForecastSource(settings, fetch=offline, clock=clock, run=lambda job: job()),
            lightning_state=LightningState(os.path.join(tempfile.mkdtemp(), "l.json")),
            nwr=NwrSource(settings, fetch=offline, clock=clock, run=lambda job: job(),
                          path=os.path.join(tempfile.mkdtemp(), "nwr.json")),
            field=FieldWeather(settings, FakeForecast(), fetch=nws, clock=clock, run=lambda job: job()))
        w = source.blocks()
        self.assertEqual(sentinel_calls, [])
        self.assertIsNone(w["lightning"])
        self.assertEqual([i["event"] for i in w["alerts"]["items"]], ["Winter Storm Watch", "Wind Advisory"])
        self.assertEqual(w["alerts"]["source"], "NWS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
