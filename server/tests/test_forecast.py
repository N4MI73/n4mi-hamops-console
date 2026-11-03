#!/usr/bin/env python3
"""Tests for the NWS forecast block, grid locators and the storm-alert rule.

No network: NWS is replaced by a stand-in that answers by URL, the clock is
fixed, and background refreshes run in-line (or are held, to test that a
request never waits for NWS).

    python server/tests/test_forecast.py      (or: python -m pytest server/tests)

The NWS documents below are cut down from the live samples of 2026-11-03
(Claude Project file live_samples_2026-11-03.md), including the first live
thunderstorm entry seen in the gridpoint `weather` series.
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

from hamops_console import forecast as fc, grid, settings as settings_mod  # noqa: E402
from hamops_console.weather import WeatherSource, apply_storm_alerts, storm_alert  # noqa: E402

G = "https://api.weather.gov/gridpoints/LWX/97,71"
POINTS = {"properties": {
    "forecast": G + "/forecast", "forecastHourly": G + "/forecast/hourly", "forecastGridData": G,
    "observationStations": G + "/stations", "cwa": "LWX", "timeZone": "America/New_York",
    "forecastZone": "https://api.weather.gov/zones/forecast/VAZ054",
    "county": "https://api.weather.gov/zones/county/VAC013"}}


def _period(n, name, start, end, temp, pop, wind, wdir, short, detail):
    return {"number": n, "name": name, "startTime": start, "endTime": end, "isDaytime": "Night" not in name,
            "temperature": temp, "temperatureUnit": "F", "temperatureTrend": None,
            "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": pop},
            "windSpeed": wind, "windDirection": wdir, "icon": "https://api.weather.gov/icons/x",
            "shortForecast": short, "detailedForecast": detail}


FORECAST = {"properties": {
    "units": "us", "generatedAt": "2026-11-03T00:27:06+00:00", "updateTime": "2026-11-02T18:21:17+00:00",
    "validTimes": "2026-11-02T12:00:00+00:00/P7DT13H", "periods": [
        _period(1, "Tonight", "2026-11-02T19:00:00-05:00", "2026-11-03T06:00:00-05:00", 57, 0, "7 mph", "NE",
                "Mostly Clear", "Mostly clear, with a low around 57. Northeast wind around 7 mph."),
        _period(2, "Tuesday", "2026-11-03T06:00:00-05:00", "2026-11-03T18:00:00-05:00", 80, 2, "6 to 9 mph", "E",
                "Mostly Sunny", "Mostly sunny, with a high near 80. East wind 6 to 9 mph."),
        _period(3, "Tuesday Night", "2026-11-03T18:00:00-05:00", "2026-11-04T06:00:00-05:00", 61, 5, "7 mph", "E",
                "Mostly Cloudy", "Mostly cloudy, with a low around 61. East wind around 7 mph."),
        _period(4, "Wednesday", "2026-11-04T06:00:00-05:00", "2026-11-04T18:00:00-05:00", 78, 30, "5 mph", "SE",
                "Chance Light Rain", "A chance of light rain after 1pm."),
        _period(5, "Wednesday Night", "2026-11-04T18:00:00-05:00", "2026-11-05T06:00:00-05:00", 62, 60, "5 mph",
                "S", "Light Rain Likely", "Light rain likely."),
    ]}}


def _hours(first_utc, count):
    out = []
    for i in range(count):
        start = first_utc + timedelta(hours=i)
        out.append({"number": i + 1, "name": "", "startTime": start.isoformat(),
                    "endTime": (start + timedelta(hours=1)).isoformat(), "isDaytime": False,
                    "temperature": 67 - i, "temperatureUnit": "F", "temperatureTrend": None,
                    "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": 0},
                    "dewpoint": {"unitCode": "wmoUnit:degC", "value": 15.6},
                    "relativeHumidity": {"unitCode": "wmoUnit:percent", "value": 78},
                    "windSpeed": "7 mph", "windDirection": "E", "icon": "https://api.weather.gov/icons/x",
                    "shortForecast": "Partly Cloudy", "detailedForecast": ""})
    return out


HOURLY = {"properties": {"updateTime": "2026-11-02T18:21:17+00:00",
                         "periods": _hours(datetime(2026, 11, 3, 0, 0, tzinfo=timezone.utc), 72)}}


def _wx(coverage, weather, intensity=None):
    return {"coverage": coverage, "weather": weather, "intensity": intensity,
            "visibility": {"unitCode": "wmoUnit:km", "value": None}, "attributes": []}


GRIDPOINT = {"properties": {
    "updateTime": "2026-11-02T18:21:17+00:00",
    "weather": {"values": [
        {"validTime": "2026-11-02T12:00:00+00:00/P2DT6H", "value": [_wx(None, None)]},
        {"validTime": "2026-11-04T18:00:00+00:00/PT12H", "value": [_wx("chance", "rain", "light")]},
        {"validTime": "2026-11-05T06:00:00+00:00/PT6H", "value": [_wx("likely", "rain", "light")]},
        {"validTime": "2026-11-05T12:00:00+00:00/PT12H",
         "value": [_wx("slight_chance", "thunderstorms"), _wx("slight_chance", "rain_showers", "light")]},
    ]},
    "windGust": {"uom": "wmoUnit:km_h-1", "values": [
        {"validTime": "2026-11-03T00:00:00+00:00/PT2H", "value": 24.076},
        {"validTime": "2026-11-03T02:00:00+00:00/PT6H", "value": 16.668}]},
    "probabilityOfThunder": {"values": []},
}}

T0 = datetime(2026, 11, 3, 0, 27, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class NWS:
    """Answers by URL; `down` makes every call fail; `fail` names URLs that fail."""

    def __init__(self):
        self.docs = {G + "/forecast": FORECAST, G + "/forecast/hourly": HOURLY, G: GRIDPOINT}
        self.calls, self.fail, self.down, self.agents = [], set(), False, []

    def __call__(self, url, contact):
        self.calls.append(url)
        self.agents.append(fc.user_agent(contact))
        if self.down or url in self.fail:
            raise urllib.error.URLError("offline")
        if url.startswith(fc.API + "/points/"):
            return copy.deepcopy(POINTS)
        return copy.deepcopy(self.docs[url])


class Held:
    """Holds background jobs until run() -- proves a request doesn't wait."""

    def __init__(self):
        self.jobs = []

    def __call__(self, job):
        self.jobs.append(job)

    def run(self):
        while self.jobs:
            self.jobs.pop(0)()


def make(now=T0, grid_value="FM18lv", run=None, mode="home"):
    settings = settings_mod.load(os.path.join(tempfile.mkdtemp(), "missing.json"))
    settings["mode"] = mode
    settings["station"][mode]["grid"] = grid_value
    nws, clock = NWS(), Clock(now)
    source = fc.ForecastSource(settings, fetch=nws, clock=clock, run=run or (lambda job: job()))
    return source, nws, clock


class Grid(unittest.TestCase):

    def test_centres(self):
        self.assertEqual(grid.centre("FM18lv"), (38.8958, -77.0417))
        self.assertEqual(grid.centre("fm18LV"), (38.8958, -77.0417))
        self.assertEqual(grid.centre("FM18"), (38.5, -77.0))
        self.assertEqual(grid.centre("JJ00aa"), (0.0208, 0.0417))

    def test_invalid(self):
        for bad in ("XX99", "FM1", "FM18l", "FM18zz", "", None, 42):
            self.assertFalse(grid.is_valid(bad), bad)
        with self.assertRaises(ValueError):
            grid.centre("FM1")


class Helpers(unittest.TestCase):

    def test_parse_interval(self):
        start, end = fc.parse_interval("2026-11-02T12:00:00+00:00/P2DT6H")
        self.assertEqual(end - start, timedelta(days=2, hours=6))
        start, end = fc.parse_interval("2026-11-05T12:00:00+00:00/PT12H")
        self.assertEqual(end - start, timedelta(hours=12))
        for bad in ("2026-11-05T12:00:00+00:00", "x/PT1H", "2026-11-05T12:00:00+00:00/P", None):
            self.assertIsNone(fc.parse_interval(bad), bad)

    def test_wind_mph(self):
        self.assertEqual(fc._wind_mph("7 mph"), 7)
        self.assertEqual(fc._wind_mph("6 to 9 mph"), 9)
        self.assertIsNone(fc._wind_mph("calm"))

    def test_user_agent(self):
        self.assertEqual(fc.user_agent("n0call@example.com"), "HamOpsConsole/0.1.0 (n0call@example.com)")
        self.assertEqual(fc.user_agent("<your email or callsign contact>"), "HamOpsConsole/0.1.0")
        self.assertEqual(fc.user_agent(""), "HamOpsConsole/0.1.0")


class ThunderOutlook(unittest.TestCase):

    def _iv(self, hours_from, length, coverage, now=T0):
        start = now + timedelta(hours=hours_from)
        return (start, start + timedelta(hours=length), coverage)

    def test_none_in_window(self):
        self.assertEqual(fc.thunder_outlook([self._iv(13, 6, "likely")], T0), (None, None))

    def test_possible(self):
        nxt, out = fc.thunder_outlook([self._iv(3, 6, "slight_chance")], T0)
        self.assertEqual(nxt, {"start": "2026-11-03T03:00:00Z", "coverage": "slight_chance"})
        self.assertEqual(out, {"level": "possible", "start": "2026-11-03T03:00:00Z", "basis": "forecast"})

    def test_likely_start_is_first_likely_hour(self):
        nxt, out = fc.thunder_outlook([self._iv(5, 3, "likely"), self._iv(1, 4, "chance")], T0)
        self.assertEqual(nxt["start"], "2026-11-03T01:00:00Z")   # first thunder of any kind
        self.assertEqual((out["level"], out["start"]), ("likely", "2026-11-03T05:00:00Z"))

    def test_interval_already_under_way_starts_this_hour(self):
        nxt, out = fc.thunder_outlook([self._iv(-2, 6, "numerous")], T0)
        self.assertEqual(nxt["start"], "2026-11-03T00:00:00Z")
        self.assertEqual(out["level"], "likely")

    def test_live_gridpoint_thursday(self):
        intervals = fc.thunder_intervals(GRIDPOINT["properties"])
        self.assertEqual(len(intervals), 1)
        self.assertIsNone(fc.thunder_outlook(intervals, T0)[0])  # Thursday is beyond 12 h tonight
        nxt, out = fc.thunder_outlook(intervals, datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc))
        self.assertEqual(nxt, {"start": "2026-11-05T12:00:00Z", "coverage": "slight_chance"})
        self.assertEqual(out["level"], "possible")


class Block(unittest.TestCase):

    def test_live_sample(self):
        source, nws, _ = make()
        b = source.block()
        self.assertEqual(b["status"], "ok")      # updateTime 6 h old is normal
        self.assertEqual(b["source"], "NWS")
        self.assertEqual(b["issued_at"], "2026-11-02T18:21:17Z")
        self.assertIsNone(b["thunder_next"])
        self.assertIsNone(b["lightning_outlook"])
        self.assertEqual(len(b["periods"]), 4)
        first = b["periods"][0]
        self.assertEqual((first["name"], first["wind"], first["temperature_f"]), ("Tonight", "7 mph NE", 57))
        self.assertEqual(first["start"], "2026-11-03T00:00:00Z")
        self.assertEqual(b["periods"][1]["wind"], "6 to 9 mph E")
        self.assertEqual(len(b["hourly"]), 12)
        h = b["hourly"][0]
        self.assertEqual((h["time"], h["temperature_f"], h["wind_mph"], h["gust_mph"]),
                         ("2026-11-03T00:00:00Z", 67, 7, 15))     # 24.076 km/h -> 15 mph
        self.assertEqual(b["hourly"][2]["gust_mph"], 10)           # 16.668 km/h -> 10 mph
        self.assertIsNone(h["thunder"])
        self.assertEqual(source.location(), {"nws_office": "LWX", "nws_zone": "VAZ054", "nws_county": "VAC013"})
        self.assertEqual(nws.calls[0], "https://api.weather.gov/points/38.8958,-77.0417")

    def test_past_hours_and_periods_dropped(self):
        source, _, clock = make()
        source.block()
        clock.now = T0 + timedelta(hours=8)            # 08:27Z: "Tonight" ended 11:00Z, not yet
        b = source.block()
        self.assertEqual(b["hourly"][0]["time"], "2026-11-03T08:00:00Z")
        clock.now = T0 + timedelta(hours=11)           # 11:27Z: Tonight is over
        self.assertEqual(source.block()["periods"][0]["name"], "Tuesday")

    def test_hourly_thunder_marked(self):
        source, nws, clock = make(now=datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc))
        hourly = copy.deepcopy(HOURLY)
        hourly["properties"]["periods"] = _hours(datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc), 24)
        nws.docs[G + "/forecast/hourly"] = hourly
        b = source.block()
        marks = {h["time"]: h["thunder"] for h in b["hourly"]}
        self.assertIsNone(marks["2026-11-05T11:00:00Z"])
        self.assertEqual(marks["2026-11-05T12:00:00Z"], "slight_chance")
        self.assertEqual(b["lightning_outlook"]["level"], "possible")

    def test_old_update_time_is_stale(self):
        source, nws, _ = make(now=datetime(2026, 11, 3, 6, 30, tzinfo=timezone.utc))
        b = source.block()
        self.assertEqual(b["status"], "stale")         # updateTime 18:21Z, 12 h 9 min old
        self.assertEqual(len(b["periods"]), 4)        # still shown, greyed

    def test_gridpoint_failure_stores_nothing(self):
        source, nws, _ = make()
        nws.fail.add(G)
        b = source.block()
        self.assertEqual(b["status"], "unavailable")  # never "no thunderstorms" by omission
        self.assertIsNone(b["thunder_next"])
        self.assertIn("not answering", b["message"])

    def test_retry_timing(self):
        source, nws, clock = make()
        nws.down = True
        source.block()
        tries = len(nws.calls)
        clock.now = T0 + timedelta(seconds=60)
        source.block()
        self.assertEqual(len(nws.calls), tries)        # not yet
        nws.down = False
        clock.now = T0 + timedelta(seconds=121)
        self.assertEqual(source.block()["status"], "ok")  # retried after 2 min, not 30
        done = len(nws.calls)
        clock.now = T0 + timedelta(minutes=20)
        source.block()
        self.assertEqual(len(nws.calls), done)         # good data: next fetch at 30 min
        clock.now = T0 + timedelta(minutes=33)
        source.block()
        self.assertEqual(len(nws.calls), done + 3)     # forecast, hourly, gridpoint; /points kept

    def test_points_rechecked_daily(self):
        source, nws, clock = make()
        source.block()
        clock.now = T0 + timedelta(hours=24, minutes=1)
        source.block()
        self.assertEqual(sum(u.startswith(fc.API + "/points/") for u in nws.calls), 2)

    def test_grid_change_drops_old_data(self):
        source, nws, _ = make()
        source.block()
        source.settings["station"]["home"]["grid"] = "EM84qa"
        nws.down = True
        b = source.block()
        self.assertEqual(b["status"], "unavailable")   # the old place's forecast isn't shown
        self.assertEqual(b["periods"], [])
        self.assertEqual(source.location()["nws_office"], None)

    def test_invalid_grid(self):
        source, nws, _ = make(grid_value="AA00x")
        b = source.block()
        self.assertEqual(b["status"], "unavailable")
        self.assertIn("grid", b["message"])
        self.assertEqual(nws.calls, [])

    def test_request_never_waits_for_nws(self):
        held = Held()
        source, nws, _ = make(run=held)
        b = source.block()
        self.assertEqual(b["status"], "unavailable")
        self.assertEqual(b["message"], "Fetching the NWS forecast")
        self.assertEqual(nws.calls, [])                # nothing fetched during the request
        source.block()
        self.assertEqual(len(held.jobs), 1)            # no second refresh while one is pending
        held.run()
        self.assertEqual(source.block()["status"], "ok")

    def test_odd_reply_doesnt_wedge_refresh(self):
        source, nws, clock = make()
        nws.docs[G + "/forecast"] = {"properties": "nonsense"}
        self.assertEqual(source.block()["status"], "unavailable")
        nws.docs[G + "/forecast"] = FORECAST
        clock.now = T0 + timedelta(seconds=121)
        self.assertEqual(source.block()["status"], "ok")

    def test_contact_sent(self):
        source, nws, _ = make()
        source.settings["nws_user_agent_contact"] = "n0call@example.com"
        source.block()
        self.assertTrue(all(a.endswith("(n0call@example.com)") for a in nws.agents))

    def test_field_mode_uses_field_grid(self):
        source, nws, _ = make(mode="field", grid_value="EM84qa")
        source.block()
        self.assertEqual(nws.calls[0], "https://api.weather.gov/points/34.0208,-82.625")


class StormAlerts(unittest.TestCase):

    def _alerts(self, *items, status="ok"):
        return {"status": status, "items": list(items)}

    def test_which_alerts_count(self):
        self.assertTrue(storm_alert({"event": "Severe Thunderstorm Warning"}))
        self.assertTrue(storm_alert({"event": "Severe Thunderstorm Watch"}))
        self.assertTrue(storm_alert({"event": "Special Weather Statement",
                                     "headline": "A strong thunderstorm will impact the area"}))
        self.assertFalse(storm_alert({"event": "Special Weather Statement", "hazard": "Dense fog."}))
        self.assertFalse(storm_alert({"event": "Tornado Watch"}))  # ranks as a hazard alert anyway

    def test_alert_makes_lightning_likely(self):
        forecast = {"lightning_outlook": None}
        apply_storm_alerts(forecast, self._alerts({"event": "Severe Thunderstorm Warning"}), T0)
        self.assertEqual(forecast["lightning_outlook"],
                         {"level": "likely", "start": "2026-11-03T00:27:00Z", "basis": "nws_alert"})

    def test_test_mode_and_unavailable_alerts_dont_count(self):
        for status in ("test", "unavailable"):
            forecast = {"lightning_outlook": None}
            apply_storm_alerts(forecast, self._alerts({"event": "Severe Thunderstorm Warning"},
                                                      status=status), T0)
            self.assertIsNone(forecast["lightning_outlook"], status)


class WeatherIntegration(unittest.TestCase):

    def test_location_filled_and_coarse_grid_flagged(self):
        settings = settings_mod.load(os.path.join(tempfile.mkdtemp(), "missing.json"))
        settings["station"]["home"]["grid"] = "FM18"
        clock = Clock(T0)
        nws = NWS()
        forecast = fc.ForecastSource(settings, fetch=nws, clock=clock, run=lambda job: job())
        weather = WeatherSource(settings, fetch=lambda url: {"simulation": False}, clock=clock, forecast=forecast)
        w = weather.blocks()
        self.assertEqual(w["location"]["nws_office"], "LWX")
        self.assertIn("too coarse", w["location"]["grid_warning"])
        self.assertEqual(w["forecast"]["status"], "ok")
        settings["station"]["home"]["grid"] = "FM18lv"
        self.assertIsNone(weather.blocks()["location"]["grid_warning"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
