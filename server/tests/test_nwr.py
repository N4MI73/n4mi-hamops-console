#!/usr/bin/env python3
"""Tests for the NOAA Weather Radio block (nwr.py) and its use in the alert list.

No network: NWS is replaced by a stand-in, the clock is fixed and background
refreshes run in-line.

    python server/tests/test_nwr.py      (or: python -m pytest server/tests)

The rows below are copied verbatim from the live NWS listing
(https://www.weather.gov/source/nwr/JS/CCL.js, fetched 2026-11-07): the
District of Columbia and neighbouring counties (the tests' neutral location,
FM18lv), plus Alaska rows for a degraded transmitter, an out-of-service one
and a county with no coverage. Only the size check is lowered for the sample.
"""

import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from hamops_console import alerts, freshness, nwr, settings as settings_mod  # noqa: E402

FIELDS = ("ST", "STATE", "COUNTY", "SAME", "SITENAME", "SITELOC", "SITESTATE", "FREQ",
          "CALLSIGN", "LAT", "LON", "PWR", "STATUS", "WFO", "REMARKS")

# (index, values in FIELDS order) -- verbatim from the live file.
ROWS = [
    (13, ("AK", "Alaska", "Chugach", "002063", "Valdez", "Valdez", "AK", "162.550", "WXJ63",
          "61.130437", "-146.352503", "300", "OUT OF SERVICE", "Anchorage|AK", "")),
    (35, ("AK", "Alaska", "Ketchikan Gateway", "002130", "Duke Island", "Duke Island", "AK", "162.450",
          "KZZ92", "54.883333", "-131.358333", "5", "DEGRADED", "Juneau|AK", "ALL")),
    (46, ("AK", "Alaska", "Kusilvak", "002158", "---No  NWR Coverage---", "", "", "", "", "", "",
          "0", "NORMAL", "", "")),
    (764, ("DC", "District of Columbia", "District of Columbia", "011001", "Baltimore", "Pikesville", "MD",
           "162.400", "KEC83", "39.386349", "-76.730772", "1000", "NORMAL", "Baltimore/Washington|VA", "")),
    (765, ("DC", "District of Columbia", "District of Columbia", "011001", "Manassas", "Independent Hill", "VA",
           "162.550", "KHB36", "38.628722", "-77.438833", "1010", "NORMAL", "Baltimore/Washington|VA", "")),
    (766, ("DC", "District of Columbia", "District of Columbia", "011001", "Washington DC", "Silver Spring", "MD",
           "162.450", "WNG736", "38.993013", "-77.030766", "300", "NORMAL", "Baltimore/Washington|VA", "")),
    (2485, ("MD", "Maryland", "Frederick", "024021", "Hagerstown", "Clear Spring", "MD", "162.475", "WXM42",
            "39.651111", "-77.970833", "1000", "NORMAL", "Baltimore/Washington|VA", "")),
    (2497, ("MD", "Maryland", "Montgomery", "024031", "Baltimore", "Pikesville", "MD", "162.400", "KEC83",
            "39.386349", "-76.730772", "1000", "NORMAL", "Baltimore/Washington|VA", "")),
    (6136, ("VA", "Virginia", "Fairfax", "051059", "Manassas", "Independent Hill", "VA", "162.550", "KHB36",
            "38.628722", "-77.438833", "1010", "NORMAL", "Baltimore/Washington|VA", "")),
    (6137, ("VA", "Virginia", "Fairfax", "051059", "Washington DC", "Silver Spring", "MD", "162.450", "WNG736",
            "38.993013", "-77.030766", "300", "NORMAL", "Baltimore/Washington|VA", "")),
    (6183, ("VA", "Virginia", "Loudoun", "051107", "Hagerstown", "Clear Spring", "MD", "162.475", "WXM42",
            "39.651111", "-77.970833", "1000", "NORMAL", "Baltimore/Washington|VA", "NW 1/2")),
]

GRID = "FM18lv"     # centre 38.8958, -77.0417 (Washington DC)
T0 = datetime(2026, 11, 7, 15, 0, 0, tzinfo=timezone.utc)


def ccl(rows=ROWS):
    """Rebuild a CCL.js file in the live layout from the rows above."""
    lines = [f"var {f} = [];" for f in FIELDS] + [""]
    for index, values in rows:
        lines += [f'{f}[{index}] = "{v}";' for f, v in zip(FIELDS, values)] + [""]
    return "\n".join(lines)


def with_status(callsign, status):
    return [(i, v[:12] + (status,) + v[13:] if v[8] == callsign else v) for i, v in ROWS]


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Small(unittest.TestCase):
    """Lowers the size check so the sample counts as a whole list."""

    def setUp(self):
        self._limits = nwr.MIN_TRANSMITTERS, nwr.MIN_COUNTIES
        nwr.MIN_TRANSMITTERS, nwr.MIN_COUNTIES = 1, 1

    def tearDown(self):
        nwr.MIN_TRANSMITTERS, nwr.MIN_COUNTIES = self._limits


class Parse(Small):

    def test_transmitters_counties_and_states(self):
        data = nwr.parse(ccl())
        self.assertEqual(set(data["transmitters"]), {"WXJ63", "KZZ92", "KEC83", "KHB36", "WNG736", "WXM42"})
        self.assertEqual(data["transmitters"]["KHB36"],
                         {"mhz": 162.55, "lat": 38.628722, "lon": -77.438833, "site": "Manassas",
                          "site_state": "VA", "status": "NORMAL"})
        self.assertEqual(data["states"], {"AK": "02", "DC": "11", "MD": "24", "VA": "51"})
        self.assertEqual(data["counties"]["11001"]["tx"], [["KEC83", None], ["KHB36", None], ["WNG736", None]])
        self.assertEqual(data["counties"]["51107"]["tx"], [["WXM42", "NW 1/2"]])
        # A county with no coverage is kept, with no transmitters.
        self.assertEqual(data["counties"]["02158"], {"name": "Kusilvak", "state": "AK", "tx": []})
        self.assertEqual(data["transmitters"]["WXJ63"]["status"], "OUT OF SERVICE")

    def test_rows_that_disagree_keep_the_worse_status(self):
        rows = ROWS + [(9000, ROWS[3][1][:12] + ("OUT OF SERVICE",) + ROWS[3][1][13:])]
        self.assertEqual(nwr.parse(ccl(rows))["transmitters"]["KEC83"]["status"], "OUT OF SERVICE")

    def test_incomplete_or_garbage_is_refused(self):
        nwr.MIN_TRANSMITTERS = 100
        with self.assertRaises(ValueError):
            nwr.parse(ccl())
        nwr.MIN_TRANSMITTERS = 1
        for bad in ("", "<html>Access denied</html>", None):
            with self.assertRaises(ValueError):
                nwr.parse(bad)

    def test_county_key(self):
        states = {"VA": "51", "DC": "11"}
        self.assertEqual(nwr.county_key("VAC059", states), "51059")
        self.assertEqual(nwr.county_key("DCC001", states), "11001")
        for zone in (None, "", "VAZ054", "ANZ535", "TXC001", "VAC59"):
            self.assertIsNone(nwr.county_key(zone, states), zone)

    def test_distance(self):
        # Silver Spring transmitter to the FM18lv centre: about 6.8 mi.
        self.assertAlmostEqual(nwr.distance_mi(38.8958, -77.0417, 38.993013, -77.030766), 6.73, delta=0.1)


class Choose(Small):

    def setUp(self):
        super().setUp()
        self.data = nwr.parse(ccl())

    def pick(self, zone, data=None):
        return nwr.choose(data or self.data, 38.8958, -77.0417, zone)

    def test_county_transmitters_first_nearest_first(self):
        county, same, nearest, message = self.pick("DCC001")
        self.assertEqual((county, same, message), ("District of Columbia, DC", "011001", None))
        self.assertEqual([e["callsign"] for e in nearest], ["WNG736", "KHB36", "KEC83"])
        self.assertEqual(nearest[0], {"callsign": "WNG736", "frequency_mhz": 162.45, "site": "Washington DC",
                                      "distance_mi": 6.7, "covers_county": True, "county_part": None,
                                      "status": "NORMAL"})

    def test_county_listing_beats_a_nearer_transmitter(self):
        # Loudoun lists only Hagerstown (the NW half), though Silver Spring is nearer to this point.
        county, same, nearest, _ = self.pick("VAC107")
        self.assertEqual((county, same), ("Loudoun, VA", "051107"))
        self.assertEqual(nearest[0]["callsign"], "WXM42")
        self.assertEqual(nearest[0]["county_part"], "NW 1/2")
        # Only one listed for the county: the two nearest others follow as alternates.
        self.assertEqual([(e["callsign"], e["covers_county"]) for e in nearest],
                         [("WXM42", True), ("WNG736", False), ("KHB36", False)])

    def test_degraded_after_working_and_out_of_service_never_first(self):
        data = nwr.parse(ccl(with_status("WNG736", "OUT OF SERVICE")))
        _, _, nearest, message = self.pick("DCC001", data)
        # The county's two working ones, the nearest other as a third choice, then the dead one.
        self.assertEqual([e["callsign"] for e in nearest], ["KHB36", "KEC83", "WXM42", "WNG736"])
        self.assertIs(nearest[2]["covers_county"], False)
        self.assertEqual(nearest[-1]["status"], "OUT OF SERVICE")
        self.assertIsNone(message)
        data = nwr.parse(ccl(with_status("KHB36", "DEGRADED")))
        _, _, nearest, _ = self.pick("VAC059", data)
        self.assertEqual([e["callsign"] for e in nearest], ["WNG736", "KHB36", "KEC83"])

    def test_county_transmitter_out_of_service_falls_back_to_nearest_working(self):
        data = nwr.parse(ccl(with_status("WXM42", "OUT OF SERVICE")))
        _, _, nearest, message = self.pick("VAC107", data)
        self.assertEqual(nearest[0]["callsign"], "WNG736")
        self.assertIs(nearest[0]["covers_county"], False)
        self.assertEqual(nearest[-1]["callsign"], "WXM42")
        self.assertEqual(message, "The county's own transmitter is out of service")
        self.assertEqual(nwr.tune_words({"status": "ok", "nearest": nearest}), "Tune NOAA Weather Radio.")

    def test_county_unknown_lists_nearest_unconfirmed(self):
        county, same, nearest, message = self.pick(None)
        self.assertEqual((county, same), (None, None))
        self.assertEqual(nearest[0]["callsign"], "WNG736")
        self.assertIsNone(nearest[0]["covers_county"])
        self.assertNotIn("WXJ63", [e["callsign"] for e in nearest])   # out of service: never listed here
        self.assertIn("not confirmed", message)
        _, _, _, message = self.pick("VAC013")     # a county missing from this sample
        self.assertIn("not in the NWS weather radio list", message)

    def test_county_with_no_coverage(self):
        data = nwr.parse(ccl())
        data["states"]["AK"] = "02"
        _, same, nearest, message = nwr.choose(data, 61.5, -163.0, "AKC158")
        self.assertEqual(same, "002158")
        self.assertIs(nearest[0]["covers_county"], False)
        self.assertEqual(message, "NWS lists no weather radio coverage for this county")

    def test_tune_words(self):
        _, _, nearest, _ = self.pick("DCC001")
        self.assertEqual(nwr.tune_words({"status": "ok", "nearest": nearest}),
                         "Tune NOAA Weather Radio WNG736, 162.450 MHz.")
        self.assertEqual(nwr.tune_words({"status": "stale", "nearest": nearest}),
                         "Tune NOAA Weather Radio WNG736, 162.450 MHz.")
        for radio in (None, {"status": "unavailable", "nearest": []}, {"status": "ok", "nearest": []}):
            self.assertEqual(nwr.tune_words(radio), "Tune NOAA Weather Radio.")


class StandIn:
    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    def __call__(self, url, contact):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def settings():
    s = settings_mod.load(os.path.join(tempfile.mkdtemp(), "missing.json"))
    s["station"]["home"]["grid"] = GRID
    return s


class Source(Small):

    def make(self, reply, path=None, clock=None):
        self.path = path or os.path.join(tempfile.mkdtemp(), "nwr_stations.json")
        self.fetch, self.clock = StandIn(reply), clock or Clock(T0)
        return nwr.NwrSource(settings(), fetch=self.fetch, clock=self.clock, run=lambda job: job(),
                             path=self.path)

    def test_first_download_is_stored_and_used(self):
        source = self.make(ccl())
        b = source.block(GRID, "DCC001")
        self.assertEqual(self.fetch.calls, 1)
        self.assertEqual(b["status"], "ok")
        self.assertEqual(b["source"], "NWS NWR listing (stored on the Pi)")
        self.assertEqual(b["fetched_at"], "2026-11-07T15:00:00Z")
        self.assertEqual((b["county"], b["same_code"]), ("District of Columbia, DC", "011001"))
        self.assertEqual(b["nearest"][0]["callsign"], "WNG736")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["downloaded_at"], "2026-11-07T15:00:00Z")

    def test_weekly_and_the_stored_copy_survives_a_restart(self):
        source = self.make(ccl())
        source.block(GRID, "DCC001")
        self.clock.now = T0 + timedelta(days=6, hours=23)
        source.block(GRID, "DCC001")
        self.assertEqual(self.fetch.calls, 1)
        # A restart, offline: the stored copy is used and the download is retried, not waited for.
        offline = StandIn(urllib.error.URLError("offline"))
        again = nwr.NwrSource(settings(), fetch=offline, clock=self.clock, run=lambda job: job(), path=self.path)
        b = again.block(GRID, "DCC001")
        self.assertEqual(offline.calls, 0)          # under a week old: nothing to fetch
        self.assertEqual(b["nearest"][0]["callsign"], "WNG736")
        self.clock.now = T0 + timedelta(days=7, hours=1)
        b = again.block(GRID, "DCC001")
        self.assertEqual(offline.calls, 1)
        self.assertEqual(b["status"], "ok")          # a week old is still current
        self.assertEqual(b["nearest"][0]["callsign"], "WNG736")
        self.clock.now = T0 + timedelta(days=7, hours=6)
        again.block(GRID, "DCC001")
        self.assertEqual(offline.calls, 1)          # retried after 6 hours, not every request
        self.clock.now = T0 + timedelta(days=7, hours=7, minutes=1)
        again.block(GRID, "DCC001")
        self.assertEqual(offline.calls, 2)

    def test_on_demand_update_ignores_the_weekly_schedule(self):
        source = self.make(ccl())
        source.block(GRID, "DCC001")
        self.clock.now = T0 + timedelta(days=1)
        self.assertTrue(source.refresh_now())
        self.assertEqual(self.fetch.calls, 2)
        b = source.block(GRID, "DCC001")
        self.assertEqual(b["fetched_at"], "2026-11-08T15:00:00Z")
        self.assertIs(b["updating"], False)
        self.assertEqual(self.fetch.calls, 2)       # and the weekly clock starts again from it

    def test_on_demand_update_while_one_is_running(self):
        held = []
        source = nwr.NwrSource(settings(), fetch=StandIn(ccl()), clock=Clock(T0), run=held.append,
                               path=os.path.join(tempfile.mkdtemp(), "nwr.json"))
        b = source.block(GRID, "DCC001")            # first download starts, held
        self.assertIs(b["updating"], True)
        self.assertEqual(b["message"], "Downloading the NWS weather radio list")
        self.assertFalse(source.refresh_now())
        held.pop()()
        self.assertTrue(source.refresh_now())

    def test_month_old_copy_is_stale_but_shown(self):
        source = self.make(ccl())
        source.block(GRID, "DCC001")
        source._fetch = StandIn(urllib.error.URLError("offline"))
        self.clock.now = T0 + timedelta(days=31)
        b = source.block(GRID, "DCC001")
        self.assertEqual(b["status"], "stale")
        self.assertIn("NWS not answering", b["message"])
        self.assertEqual(b["nearest"][0]["callsign"], "WNG736")

    def test_no_list_yet_is_unavailable_with_reason(self):
        b = self.make(urllib.error.HTTPError(nwr.URL, 403, "Forbidden", {}, None)).block(GRID, "DCC001")
        self.assertEqual(b["status"], "unavailable")
        self.assertEqual(b["message"], "NWS answered with an error (403)")
        self.assertEqual(b["nearest"], [])
        self.assertEqual(nwr.tune_words(b), "Tune NOAA Weather Radio.")

    def test_bad_download_keeps_the_good_copy(self):
        source = self.make(ccl())
        source.block(GRID, "DCC001")
        source._fetch = StandIn("<html>Access denied</html>")
        self.clock.now = T0 + timedelta(days=2)
        b = source.block(GRID, "DCC001")
        self.assertEqual(b["nearest"][0]["callsign"], "WNG736")
        self.assertEqual(b["fetched_at"], "2026-11-07T15:00:00Z")

    def test_damaged_file_is_downloaded_again(self):
        path = os.path.join(tempfile.mkdtemp(), "nwr_stations.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        source = self.make(ccl(), path=path)
        self.assertEqual(source.block(GRID, "DCC001")["status"], "ok")
        self.assertEqual(self.fetch.calls, 1)

    def test_county_unknown_message_and_bad_grid(self):
        source = self.make(ccl())
        b = source.block(GRID, None)
        self.assertEqual(b["status"], "ok")
        self.assertIn("not confirmed", b["message"])
        b = source.block("nonsense", "DCC001")
        self.assertEqual(b["status"], "unavailable")
        self.assertEqual(b["nearest"], [])


class Endpoint(Small):

    def test_refresh_endpoint(self):
        from hamops_console.app import create_app
        held = []
        radio = nwr.NwrSource(settings(), fetch=StandIn(ccl()), clock=Clock(T0), run=held.append,
                              path=os.path.join(tempfile.mkdtemp(), "nwr.json"))

        class Weather:
            nwr = radio

        client = create_app(settings(), propagation=object(), weather=Weather(),
                            settings_file=os.path.join(tempfile.mkdtemp(), "s.json")).test_client()
        r = client.post("/api/weather-radio/refresh")
        self.assertEqual((r.status_code, r.get_json()), (202, {"started": True}))
        r = client.post("/api/weather-radio/refresh")
        self.assertEqual((r.status_code, r.get_json()), (409, {"started": False}))


class AlertWording(Small):
    """The NWS-alerts outage cards name the station to tune (contract section 5)."""

    def test_outage_cards_name_the_station(self):
        source = nwr.NwrSource(settings(), fetch=StandIn(ccl()), clock=Clock(T0), run=lambda job: job(),
                               path=os.path.join(tempfile.mkdtemp(), "nwr.json"))
        radio = source.block(GRID, "DCC001")
        down = freshness.block("Weather Sentinel", None, None, 0, error="Weather Sentinel can't reach NWS")
        down["items"] = []
        fc = freshness.block("NWS", T0, T0, 3600, now=T0)
        fc.update({"lightning_outlook": {"level": "likely", "start": "2026-11-07T16:00:00Z", "basis": "forecast"},
                   "thunder_next": None, "hourly": [], "periods": []})
        items = alerts.build(None, {"alerts": down, "forecast": fc, "weather_radio": radio}, settings(), T0)
        storms = next(i for i in items if i["category"] == "alerts_unavailable_storms")
        self.assertTrue(storms["detail"].endswith("Tune NOAA Weather Radio WNG736, 162.450 MHz."), storms["detail"])
        card = next(i for i in items if i["ref"]["id"] == alerts.NWS_ALERTS_SOURCE_ID)
        self.assertTrue(card["detail"].endswith("Not the same as no alerts. Tune NOAA Weather Radio WNG736, "
                                                "162.450 MHz."), card["detail"])
        # No list yet: the plain words, as before.
        items = alerts.build(None, {"alerts": down, "forecast": fc, "weather_radio": None}, settings(), T0)
        card = next(i for i in items if i["ref"]["id"] == alerts.NWS_ALERTS_SOURCE_ID)
        self.assertTrue(card["detail"].endswith("Tune NOAA Weather Radio."), card["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
