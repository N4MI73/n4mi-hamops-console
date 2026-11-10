#!/usr/bin/env python3
"""Tests for the ranked alert list (/api/alerts) and the console's Acknowledge.

No network: blocks are built by hand (in the shapes the propagation, weather
and forecast modules produce), the clock is fixed, and local time is set to
America/New_York so the wording can be checked.

    python server/tests/test_alerts.py      (or: python -m pytest server/tests)
"""

import copy
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

os.environ["TZ"] = "America/New_York"
if hasattr(time, "tzset"):
    time.tzset()

from hamops_console import alerts, settings as settings_mod  # noqa: E402

NOW = datetime(2026, 11, 1, 14, 52, 0, tzinfo=timezone.utc)
SETTINGS = settings_mod.load(os.path.join(tempfile.mkdtemp(), "missing.json"))


def fresh(source, **fields):
    block = {"status": "ok", "source": source, "fetched_at": "2026-11-01T14:51:00Z",
             "issued_at": None, "age_sec": 60, "message": None}
    block.update(fields)
    return block


def prop(good=(), k=1, xray="B4.3", status="ok"):
    bands = [{"band": b, "rating": "good"} for b in good] + [{"band": "80m", "rating": "poor"}]
    return fresh("PropMon", status=status, k_index=k, xray=xray, bands=bands, summary="summary text",
                 issued_at="2026-11-01T14:49:44Z")


def nws(event, kind, cls, expires="2026-11-01T15:36:32Z", onset=None, id_=None, hazard="Hazard text."):
    return {"id": id_ or f"urn:test:{event}", "event": event, "kind": kind, "hazard_class": cls,
            "severity": "Severe", "headline": "Headline.", "hazard": hazard, "instruction": None,
            "onset": onset, "expires": expires}


QUIET_LIGHTNING = {"within_10mi": False, "within_5mi": False, "frequent": False, "closest_mi": None,
                   "last_strike_at": None, "strikes_recent": 0, "window_min": 10,
                   "hold_until_10mi": None, "hold_until_5mi": None, "hold_since_5mi": None}


def weather(items=(), lightning=None, outlook=None, alerts_status="ok", test=False):
    status = "test" if test else "ok"
    w = {"alerts": fresh("Weather Sentinel", status="test" if test else alerts_status, items=list(items)),
         "conditions": fresh("Weather Sentinel", status=status),
         "lightning": fresh("Weather Sentinel", status=status, **(lightning or QUIET_LIGHTNING)),
         "forecast": fresh("NWS", lightning_outlook=outlook, thunder_next=None, hourly=[], periods=[])}
    if test:
        for b in ("alerts", "conditions", "lightning"):
            w[b]["message"] = "Home weather station in test mode"
    return w


def cats(items):
    return [i["category"] for i in items]


def build(p=None, w=None, acks=None, now=NOW):
    return alerts.build(p if p is not None else prop(), w if w is not None else weather(),
                        SETTINGS, now, acks)


class Quiet(unittest.TestCase):

    def test_all_quiet_never_empty(self):
        items = build()
        self.assertEqual(cats(items), ["all_quiet"])
        self.assertEqual((items[0]["title"], items[0]["detail"], items[0]["color"]), ("All quiet.", None, "green"))
        self.assertEqual(items[0]["rank"], 1)

    def test_one_open_band_in_all_quiet(self):
        self.assertEqual(build(p=prop(good=["20m"]))[0]["detail"], "20m is open.")

    def test_good_propagation(self):
        items = build(p=prop(good=["20m", "17m"]))
        self.assertEqual(cats(items), ["propagation"])
        self.assertEqual((items[0]["title"], items[0]["color"]), ("20m and 17m are open", "green"))

    def test_bad_propagation(self):
        items = build(p=prop(good=["20m", "17m"], k=5))
        self.assertEqual((items[0]["title"], items[0]["color"]), ("Propagation disturbed", "yellow"))
        self.assertIn("K-index 5", items[0]["detail"])
        self.assertIn("M1.2 X-ray flare", build(p=prop(xray="M1.2"))[0]["detail"])

    def test_stale_propagation_not_judged(self):
        items = build(p=prop(good=["20m", "17m"], k=7, status="stale"))
        self.assertEqual(cats(items), ["source_down"])
        self.assertEqual(items[0]["title"], "PropMon data is stale")


class Storm(unittest.TestCase):

    def setUp(self):
        lightning = dict(QUIET_LIGHTNING, within_10mi=True, within_5mi=True, frequent=True, closest_mi=3.5,
                         strikes_recent=4, hold_until_5mi="2026-11-01T15:21:32Z",
                         hold_since_5mi="2026-11-01T14:51:32Z", last_strike_at="2026-11-01T14:51:32Z")
        outlook = {"level": "likely", "start": "2026-11-01T14:52:00Z", "basis": "nws_alert"}
        items = [nws("Flood Advisory", "advisory", "site_hazard"),
                 nws("Flood Watch", "watch", "site_hazard"),
                 nws("Severe Thunderstorm Warning", "warning", "site_hazard",
                     hazard="60 mph wind gusts and quarter size hail.")]
        self.items = build(w=weather(items, lightning, outlook))

    def test_order(self):
        self.assertEqual(cats(self.items), ["hazard_warning", "lightning_5mi", "hazard_alert", "hazard_alert",
                                            "lightning_likely_soon"])
        self.assertEqual([i["rank"] for i in self.items], [1, 2, 3, 4, 5])

    def test_critical_and_wording(self):
        warning, strike = self.items[0], self.items[1]
        self.assertTrue(warning["critical"] and strike["critical"])
        self.assertFalse(any(i["critical"] for i in self.items[2:]))
        self.assertEqual(warning["color"], "red")
        self.assertEqual(warning["detail"], "60 mph wind gusts and quarter size hail. Until 10:36 AM (15:36 UTC).")
        self.assertEqual(warning["until"], "2026-11-01T15:36:32Z")   # NWS expiry, for the Weather page
        self.assertNotIn("until", strike)                            # only NWS alert items carry it
        self.assertEqual(strike["title"], "Lightning 3.5 mi away")
        self.assertEqual(strike["detail"], "4 strikes in 10 min. Stay in until 10:21 AM (15:21 UTC).")
        self.assertEqual(strike["ref"]["id"], "lightning-5mi-2026-11-01T14:51:32Z")
        self.assertEqual(self.items[4]["detail"], "NWS alert, not detected.")

    def test_one_detected_lightning_item(self):
        self.assertNotIn("lightning_frequent", cats(self.items))
        self.assertNotIn("lightning_10mi", cats(self.items))

    def test_acknowledged(self):
        items = build(w=weather([nws("Tornado Warning", "warning", "site_hazard", id_="urn:x")]),
                      acks={"urn:x": "2026-11-01T14:50:00Z"})
        self.assertTrue(items[0]["acknowledged"])


class Lightning(unittest.TestCase):

    def test_frequent_beats_10mi(self):
        l = dict(QUIET_LIGHTNING, within_10mi=True, frequent=True, closest_mi=7.0)
        self.assertEqual(cats(build(w=weather(lightning=l))), ["lightning_frequent"])

    def test_10mi(self):
        l = dict(QUIET_LIGHTNING, within_10mi=True, closest_mi=8.2, strikes_recent=2,
                 hold_until_10mi="2026-11-01T15:20:00Z")
        item = build(w=weather(lightning=l))[0]
        self.assertEqual((item["category"], item["title"]), ("lightning_10mi", "Lightning 8.2 mi away"))
        self.assertIn("Wait until 10:20 AM (15:20 UTC)", item["detail"])

    def test_stale_lightning_not_used(self):
        w = weather(lightning=dict(QUIET_LIGHTNING, within_5mi=True))
        w["lightning"]["status"] = "stale"
        self.assertNotIn("lightning_5mi", cats(build(w=w)))

    def test_forecast_levels(self):
        def outlook(level, hours):
            return {"level": level, "start": (NOW + timedelta(hours=hours)).isoformat(), "basis": "forecast"}
        self.assertEqual(cats(build(w=weather(outlook=outlook("likely", 1)))), ["lightning_likely_soon"])
        later = build(w=weather(outlook=outlook("likely", 4)))[0]
        self.assertEqual((later["category"], later["title"]),
                         ("lightning_outlook", "Lightning likely after 1:52 PM (18:52 UTC)"))
        self.assertEqual(build(w=weather(outlook=outlook("possible", 1)))[0]["category"], "lightning_outlook")

    def test_detected_and_forecast_together(self):
        l = dict(QUIET_LIGHTNING, within_10mi=True, closest_mi=8.0)
        o = {"level": "possible", "start": NOW.isoformat(), "basis": "forecast"}
        self.assertEqual(cats(build(w=weather(lightning=l, outlook=o))), ["lightning_10mi", "lightning_outlook"])


class Sources(unittest.TestCase):

    def test_alerts_unavailable_with_storms(self):
        o = {"level": "possible", "start": (NOW + timedelta(hours=3)).isoformat(), "basis": "forecast"}
        w = weather(outlook=o, alerts_status="unavailable")
        w["alerts"]["message"] = "Weather Sentinel not answering"
        items = build(w=w)
        self.assertEqual(cats(items), ["alerts_unavailable_storms", "lightning_outlook", "source_down"])
        self.assertEqual(items[0]["color"], "red")
        self.assertIn("not the same as no alerts", items[0]["detail"])

    def test_storms_too_far_off(self):
        o = {"level": "possible", "start": (NOW + timedelta(hours=7)).isoformat(), "basis": "forecast"}
        self.assertNotIn("alerts_unavailable_storms", cats(build(w=weather(outlook=o, alerts_status="stale"))))

    def test_test_mode_one_item_and_no_alerts(self):
        w = weather([nws("Tornado Warning", "warning", "site_hazard")], test=True)
        w["alerts"]["items"] = []   # the weather module empties them in test mode
        items = build(w=w)
        self.assertEqual(cats(items), ["source_down"])
        self.assertEqual(items[0]["title"], "Home weather station in test mode")

    def test_one_item_per_source(self):
        w = weather()
        for b in ("alerts", "conditions", "lightning"):
            w[b]["status"] = "unavailable"
        w["alerts"]["items"] = []
        w["forecast"]["status"] = "unavailable"
        titles = [i["title"] for i in build(w=w)]
        # Sentinel's three blocks give one card, worded for the alerts and ranked first.
        self.assertEqual(titles, ["NWS alerts unavailable", "NWS unavailable"])

    def test_nws_alerts_outage_first_among_sources(self):
        # PropMon fetched more recently than the alerts, so "newest first" alone would put
        # PropMon first; the NWS-alerts card must still lead (Dan, 2026-11-05).
        w = weather(alerts_status="stale")
        w["alerts"].update(fetched_at="2026-11-01T14:30:00Z", message="Weather Sentinel can't reach NWS")
        p = prop(status="stale")
        p["fetched_at"] = "2026-11-01T14:50:00Z"
        items = build(p=p, w=w)
        self.assertEqual(cats(items), ["source_down", "source_down"])
        first = items[0]
        self.assertEqual(first["title"], "NWS alerts out of date")
        self.assertEqual(first["detail"], "Weather Sentinel can't reach NWS. Not the same as no alerts. "
                         "Tune NOAA Weather Radio.")
        self.assertEqual(first["ref"], {"block": "weather.alerts", "id": alerts.NWS_ALERTS_SOURCE_ID})
        self.assertEqual(items[1]["title"], "PropMon data is stale")

    def test_other_sentinel_blocks_keep_their_wording(self):
        # Alerts fine, Tempest stale: still "Weather Sentinel", not "NWS alerts".
        w = weather()
        w["conditions"]["status"] = "stale"
        items = build(w=w)
        self.assertEqual([i["title"] for i in items], ["Weather Sentinel data is stale"])
        self.assertEqual(items[0]["ref"]["id"], "source-Weather Sentinel")


class Badge(unittest.TestCase):

    def test_badge_items_after_card_and_all_quiet_stays(self):
        items = build(w=weather([nws("Heat Advisory", "advisory", "temperature"),
                                 nws("Freeze Warning", "warning", "other"),
                                 nws("Dense Fog Advisory", "advisory", "other")]))
        self.assertEqual(cats(items), ["all_quiet", "temperature_alert", "other_warning", "other_notice"])
        self.assertEqual([i["tier"] for i in items], ["card", "badge", "badge", "badge"])
        self.assertEqual([i["color"] for i in items[1:]], ["yellow", "blue", "blue"])

    def test_newest_first_within_category(self):
        items = build(w=weather([nws("Tornado Warning", "warning", "site_hazard", onset="2026-11-01T14:30:00Z"),
                                 nws("Flash Flood Warning", "warning", "site_hazard",
                                     onset="2026-11-01T14:45:00Z")]))
        self.assertEqual([i["title"] for i in items[:2]], ["Flash Flood Warning", "Tornado Warning"])


class AckStoreTests(unittest.TestCase):

    def test_saved_and_reloaded(self):
        path = os.path.join(tempfile.mkdtemp(), "state", "acks.json")
        store = alerts.AckStore(path, clock=lambda: NOW)
        store.add("urn:x")
        self.assertEqual(alerts.AckStore(path, clock=lambda: NOW).current(), {"urn:x": "2026-11-01T14:52:00Z"})

    def test_old_entries_dropped(self):
        path = os.path.join(tempfile.mkdtemp(), "acks.json")
        store = alerts.AckStore(path, clock=lambda: NOW)
        store.add("urn:old")
        later = alerts.AckStore(path, clock=lambda: NOW + timedelta(hours=49))
        self.assertEqual(later.current(), {})

    def test_broken_file_is_empty(self):
        path = os.path.join(tempfile.mkdtemp(), "acks.json")
        with open(path, "w") as f:
            f.write("[not, json")
        self.assertEqual(alerts.AckStore(path).current(), {})


class HoldId(unittest.TestCase):
    """A 5-mile hold keeps one id while strikes extend it, and a new hold gets a new one."""

    def test_stable_through_extension(self):
        import test_weather as tw
        source, stand_in, clock = tw.make(tw.storm_reply(), tw.T_STORM)
        first = source.blocks()["lightning"]["hold_since_5mi"]
        later = tw.storm_reply()
        epoch = later["lightning"]["close"]["last_strike_epoch"] + 300       # another strike 5 min on
        later["lightning"]["close"]["last_strike_epoch"] = epoch
        stand_in.reply = later
        clock.now = tw.T_STORM + timedelta(minutes=6)
        l = source.blocks()["lightning"]
        self.assertEqual(l["hold_since_5mi"], first)
        self.assertEqual(l["hold_until_5mi"], "2026-11-01T15:26:32Z")      # extended
        new = copy.deepcopy(later)
        new["lightning"]["close"]["last_strike_epoch"] = epoch + 3600      # an hour on: a new hold
        stand_in.reply = new
        clock.now = tw.T_STORM + timedelta(minutes=67)
        self.assertNotEqual(source.blocks()["lightning"]["hold_since_5mi"], first)


class Endpoints(unittest.TestCase):

    def setUp(self):
        try:
            from hamops_console.app import create_app
        except ImportError:
            self.skipTest("Flask not installed")

        class Fixed:
            def __init__(self, value):
                self.value = value

            def block(self):
                return copy.deepcopy(self.value)

            def blocks(self):
                return copy.deepcopy(self.value)

        w = weather([nws("Tornado Warning", "warning", "site_hazard", id_="urn:tor", expires="2099-01-01T00:00:00Z")])
        self.store = alerts.AckStore(os.path.join(tempfile.mkdtemp(), "acks.json"))
        self.ended = alerts.EndedStore(os.path.join(tempfile.mkdtemp(), "ended.json"))
        self.weather = Fixed(w)
        self.client = create_app(SETTINGS, propagation=Fixed(prop()), weather=self.weather, acks=self.store,
                                 ended=self.ended, dashboard_dir=tempfile.mkdtemp()).test_client()

    def test_get_and_ack(self):
        body = self.client.get("/api/alerts").get_json()
        self.assertEqual(body["items"][0]["category"], "hazard_warning")
        self.assertFalse(body["items"][0]["acknowledged"])
        r = self.client.post("/api/alerts/ack", json={"id": "urn:tor"})
        self.assertEqual((r.status_code, r.get_json()), (200, {"acknowledged": "urn:tor"}))
        self.assertTrue(self.client.get("/api/alerts").get_json()["items"][0]["acknowledged"])
        with open(self.store.path) as f:
            self.assertIn("urn:tor", json.load(f))

    def test_clear_hold_needs_a_lightning_item(self):
        r = self.client.post("/api/alerts/clear-hold")
        self.assertEqual((r.status_code, r.get_json()), (409, {"error": "No lightning hold to clear"}))

    def test_clear_hold(self):
        from hamops_console.app import create_app

        class Stormy:
            cleared = []

            def block(self):
                return prop()

            def blocks(self):
                l = dict(QUIET_LIGHTNING, within_10mi=True, closest_mi=8.0)
                return weather(lightning=l)

            def clear_hold(self):
                self.cleared.append(NOW)
                return NOW

        stormy = Stormy()
        client = create_app(SETTINGS, propagation=stormy, weather=stormy, acks=self.store,
                            ended=self.ended, dashboard_dir=tempfile.mkdtemp()).test_client()
        r = client.post("/api/alerts/clear-hold")
        self.assertEqual((r.status_code, r.get_json()), (200, {"cleared_at": "2026-11-01T14:52:00Z"}))
        self.assertEqual(stormy.cleared, [NOW])

    def test_alerts_reply_carries_ended_list(self):
        body = self.client.get("/api/alerts").get_json()
        self.assertEqual((body["ended"], body["nws_alerts_status"]), ([], "ok"))
        self.weather.value["alerts"]["items"] = []          # NWS ended the warning
        body = self.client.get("/api/alerts").get_json()
        self.assertEqual([e["title"] for e in body["ended"]], ["Tornado Warning"])
        self.assertEqual(body["items"][0]["category"], "all_quiet")

    def test_bad_acks(self):
        self.assertEqual(self.client.post("/api/alerts/ack", json={"id": "urn:nope"}).status_code, 404)
        self.assertEqual(self.client.post("/api/alerts/ack", json={"x": 1}).status_code, 400)
        self.assertEqual(self.client.post("/api/alerts/ack", data="junk").status_code, 400)


class EndedTest(unittest.TestCase):
    """NWS alerts that ended in the last 24 hours (Alerts page)."""

    def setUp(self):
        self.clock = [NOW]
        self.path = os.path.join(tempfile.mkdtemp(), "ended.json")
        self.store = alerts.EndedStore(self.path, clock=lambda: self.clock[0])

    def items(self, *nws_items, status="ok"):
        return build(w=weather(nws_items, alerts_status=status))

    def test_nws_items_carry_kind_and_instruction(self):
        item = self.items(dict(nws("Tornado Watch", "watch", "site_hazard"), instruction="Be ready."))[0]
        self.assertEqual((item["kind"], item["instruction"]), ("watch", "Be ready."))

    def test_ended_early_uses_the_time_seen_gone(self):
        watch = nws("Tornado Watch", "watch", "site_hazard", expires="2026-11-01T18:00:00Z")
        self.assertEqual(self.store.observe(self.items(watch), "ok"), [])
        self.clock[0] = NOW + timedelta(minutes=20)
        ended = self.store.observe(self.items(), "ok")
        self.assertEqual(len(ended), 1)
        e = ended[0]
        self.assertEqual((e["id"], e["title"], e["kind"], e["color"]),
                         ("urn:test:Tornado Watch", "Tornado Watch", "watch", "yellow"))
        self.assertEqual(e["ended_at"], "2026-11-01T15:12:00Z")

    def test_expired_uses_its_expiry(self):
        sws = nws("Special Weather Statement", "statement", "site_hazard", expires="2026-11-01T15:00:00Z")
        self.store.observe(self.items(sws), "ok")
        self.clock[0] = NOW + timedelta(hours=1)
        self.assertEqual(self.store.observe(self.items(), "ok")[0]["ended_at"], "2026-11-01T15:00:00Z")

    def test_nothing_concluded_while_nws_alerts_not_current(self):
        warn = nws("Tornado Warning", "warning", "site_hazard", expires="2026-11-01T18:00:00Z")
        self.store.observe(self.items(warn), "ok")
        for status in ("stale", "unavailable", "test"):
            self.assertEqual(self.store.observe([], status), [], status)
        # Back to current and still there: never ended.
        self.assertEqual(self.store.observe(self.items(warn), "ok"), [])

    def test_extended_alert_leaves_the_ended_list(self):
        warn = nws("Tornado Warning", "warning", "site_hazard", expires="2026-11-01T18:00:00Z")
        self.store.observe(self.items(warn), "ok")
        self.assertEqual(len(self.store.observe(self.items(), "ok")), 1)
        self.assertEqual(self.store.observe(self.items(warn), "ok"), [])

    def test_kept_24_hours_newest_first_and_survives_restart(self):
        a = nws("Tornado Watch", "watch", "site_hazard", id_="urn:a", expires="2026-11-02T18:00:00Z")
        b = nws("Wind Advisory", "advisory", "site_hazard", id_="urn:b", expires="2026-11-02T18:00:00Z")
        self.store.observe(self.items(a, b), "ok")
        self.store.observe(self.items(b), "ok")              # a ends now
        self.clock[0] = NOW + timedelta(hours=2)
        ended = self.store.observe(self.items(), "ok")       # b ends 2 h later
        self.assertEqual([e["id"] for e in ended], ["urn:b", "urn:a"])
        reloaded = alerts.EndedStore(self.path, clock=lambda: NOW + timedelta(hours=25))
        self.assertEqual([e["id"] for e in reloaded.observe([], "ok")], ["urn:b"])

    def test_non_nws_items_not_tracked(self):
        lightning = dict(QUIET_LIGHTNING, within_10mi=True, closest_mi=8.0)
        self.store.observe(build(w=weather(lightning=lightning)), "ok")
        self.assertEqual(self.store.observe(build(), "ok"), [])
        self.assertFalse(os.path.exists(self.path), "nothing to save")

    def test_broken_file_is_empty(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertEqual(alerts.EndedStore(self.path).observe([], "ok"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
