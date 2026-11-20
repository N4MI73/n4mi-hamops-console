#!/usr/bin/env python3
"""Tests for Field Mode lightning from the GOES satellite (step 2, 2026-11-20).

A stand-in bucket answers the listing and file requests, so nothing is downloaded and
python3-netcdf4 isn't needed: the stand-in reader turns each file name into flashes.

    python server/tests/test_field_lightning.py      (or: python -m pytest server/tests)
"""

import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from hamops_console import alerts, field_lightning as fl, freshness, glm, grid, settings as settings_mod  # noqa: E402
from hamops_console import settings_api  # noqa: E402

FIELD_GRID = "FM18lv"                  # a neutral test location (Washington DC), not the home QTH
SITE = grid.centre(FIELD_GRID)
T0 = datetime(2026, 7, 4, 18, 30, 5, tzinfo=timezone.utc)
BASE = "https://bucket.example"


def key_for(start):
    prefix = glm.hour_prefix(start)
    stamp = f"{start:%Y}{start.timetuple().tm_yday:03d}{start:%H%M%S}0"
    return f"{prefix}OR_GLM-L2-LCFA_G19_s{stamp}_e{stamp}_c{stamp}.nc"


def point_at(miles, bearing_deg):
    """(lat, lon) about `miles` from SITE on a bearing (flat-earth, close enough here)."""
    import math
    lat = SITE[0] + miles * math.cos(math.radians(bearing_deg)) / 69.0
    lon = SITE[1] + miles * math.sin(math.radians(bearing_deg)) / (69.0 * math.cos(math.radians(SITE[0])))
    return lat, lon


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Bucket:
    """Files every 20 s up to the clock's time (each available 30 s after it starts).
    `flashes[key] = [(miles, bearing), ...]` puts lightning in a file."""

    def __init__(self, clock):
        self.clock = clock
        self.flashes = {}
        self.calls = []
        self.fail = None

    def keys_until(self, now):
        start = (now - timedelta(minutes=70)).replace(second=0, microsecond=0)
        out = []
        t = start
        while t + timedelta(seconds=30) <= now:
            out.append(key_for(t))
            t += timedelta(seconds=20)
        return out

    def fetch(self, url, timeout):
        self.calls.append(url)
        if self.fail:
            raise self.fail
        parts = urllib.parse.urlparse(url)
        if parts.path in ("", "/"):
            q = dict(urllib.parse.parse_qsl(parts.query))
            keys = [k for k in self.keys_until(self.clock()) if k.startswith(q["prefix"])
                    and ("start-after" not in q or k > q["start-after"])]
            body = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys)
            return (f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                    f"<IsTruncated>false</IsTruncated>{body}</ListBucketResult>").encode()
        return urllib.parse.unquote(parts.path.lstrip("/")).encode() + b"\0" * 1000

    def read(self, data):
        key = data.rstrip(b"\0").decode()
        start = glm.file_start(key)
        return [(start, *point_at(m, b)) for m, b in self.flashes.get(key, [])]

    def files_read(self):
        return [u for u in self.calls if "list-type" not in u]


def make_settings(mode="field"):
    s = settings_mod.load(os.path.join(tempfile.mkdtemp(), "missing.json"))
    s["mode"] = mode
    s["station"]["field"]["grid"] = FIELD_GRID
    s["station"]["home"]["grid"] = "FN20"
    s["sources"]["glm_east_url"] = BASE
    return s


def make(mode="field", path=None, clock=None, bucket=None, reader=True):
    clock = clock or Clock(T0)
    bucket = bucket or Bucket(clock)
    path = path or os.path.join(tempfile.mkdtemp(), "field_lightning.json")
    source = fl.FieldLightning(make_settings(mode), fetch=bucket.fetch, read=bucket.read, clock=clock,
                               state_path=path, reader_ok=lambda: reader)
    return source, clock, bucket, path


def forecast(outlook=None, status="ok"):
    return {"status": status, "lightning_outlook": outlook}


LIKELY = {"level": "likely", "start": "2026-07-04T19:00:00Z", "basis": "nws_forecast"}


def ranked(source, outlook=None, fstatus="ok"):
    weather = {"alerts": {"status": "ok", "items": []}, "lightning": source.block(),
               "forecast": forecast(outlook, fstatus)}
    return alerts.build(None, weather, source.settings, source._clock())


def categories(items):
    return [i["category"] for i in items]


class Switch(unittest.TestCase):

    def test_off_by_default_never_fetches_and_never_reads_as_all_clear(self):
        source, clock, bucket, _ = make()
        self.assertEqual(source.poll_once(), fl.IDLE_SEC)
        self.assertEqual(bucket.calls, [])
        b = source.block()
        self.assertEqual(b["status"], "off")
        self.assertIsNone(b["within_20mi"])
        self.assertIsNone(b["within_5mi"])
        self.assertFalse(b["detection"]["on"])
        self.assertNotIn("source_down", categories(ranked(source)))

    def test_turn_on_starts_with_the_newest_file_only(self):
        source, clock, bucket, _ = make()
        source.turn_on()
        source.poll_once()
        self.assertEqual(len(bucket.files_read()), 1)
        self.assertEqual(source.block()["status"], "ok")
        clock.now += timedelta(seconds=40)
        source.poll_once()
        self.assertEqual(len(bucket.files_read()), 3)       # two more 20-second files

    def test_economy_reads_one_file_a_minute(self):
        source, clock, bucket, _ = make()
        source.settings["field_lightning"]["level"] = "economy"
        source.turn_on()
        self.assertEqual(source.poll_once(), fl.POLL_SEC["economy"])
        clock.now += timedelta(minutes=3)
        source.poll_once()
        self.assertEqual(len(bucket.files_read()), 4)        # the first, then one for each minute

    def test_field_mode_only(self):
        source, clock, bucket, _ = make(mode="home")
        source.on = True
        self.assertEqual(source.poll_once(), fl.IDLE_SEC)
        self.assertEqual(bucket.calls, [])


class Tiers(unittest.TestCase):

    def setUp(self):
        self.source, self.clock, self.bucket, _ = make()
        self.source.turn_on()
        self.source.poll_once()

    def strike(self, miles, bearing=247.5):
        self.clock.now += timedelta(seconds=20)
        self.bucket.flashes[self.bucket.keys_until(self.clock.now)[-1]] = [(miles, bearing)]
        self.source.poll_once()

    def test_20_mile_notification(self):
        self.strike(14)
        b = self.source.block()
        self.assertTrue(b["within_20mi"])
        self.assertFalse(b["within_10mi"])
        self.assertEqual(b["closest_bearing"], "WSW")
        top = ranked(self.source)[0]
        self.assertEqual(top["category"], "lightning_20mi")
        self.assertEqual(top["title"], "Lightning within 20 mi")
        self.assertIn("14 mi WSW at", top["detail"])
        self.assertIn("GOES satellite", top["detail"])
        self.assertFalse(top["critical"])

    def test_10_and_5_mile_tiers(self):
        self.strike(8)
        self.assertEqual(ranked(self.source)[0]["category"], "lightning_10mi")
        self.strike(3)
        top = ranked(self.source)[0]
        self.assertEqual(top["category"], "lightning_5mi")
        self.assertTrue(top["critical"])
        since = self.source.block()["hold_since_5mi"]
        self.strike(4)                                         # extends the hold, same since
        b = self.source.block()
        self.assertEqual(b["hold_since_5mi"], since)
        self.assertEqual(b["hold_until_5mi"], freshness.iso(self.source.flashes[-1]["time"] + timedelta(minutes=30)))

    def test_one_flash_holds_for_30_minutes_then_clears(self):
        self.strike(14)
        self.clock.now += timedelta(minutes=29)
        self.source.poll_once()
        self.assertTrue(self.source.block()["within_20mi"])
        self.clock.now += timedelta(minutes=2)
        self.source.poll_once()
        b = self.source.block()
        self.assertFalse(b["within_20mi"])
        self.assertEqual(categories(ranked(self.source)), ["all_quiet"])

    def test_beyond_20_miles_is_ignored(self):
        self.strike(23)
        self.assertFalse(self.source.block()["within_20mi"])

    def test_operator_clear(self):
        self.strike(3)
        self.source.clear_hold()
        self.assertFalse(self.source.block()["within_5mi"])
        self.strike(9)
        self.assertTrue(self.source.block()["within_10mi"])

    def test_late_data_keeps_the_hold_and_says_so(self):
        self.strike(3)
        self.bucket.fail = urllib.error.URLError("offline")
        self.clock.now += timedelta(minutes=4)
        self.source.poll_once()
        b = self.source.block()
        self.assertEqual(b["status"], "stale")
        self.assertTrue(b["within_5mi"])
        items = ranked(self.source)
        self.assertEqual(items[0]["category"], "lightning_5mi")
        late = [i for i in items if i["category"] == "source_down"][0]
        self.assertEqual(late["title"], "Lightning data late")
        self.assertIn("Not the same as no lightning.", late["detail"])


class Starting(unittest.TestCase):

    def test_no_late_card_while_starting_then_unavailable(self):
        source, clock, bucket, _ = make()
        bucket.fail = urllib.error.URLError("offline")
        source.turn_on()
        source.poll_once()
        b = source.block()
        self.assertEqual(b["status"], "unavailable")
        self.assertIsNone(b["within_20mi"])
        self.assertNotIn("source_down", categories(ranked(source)))
        clock.now += timedelta(minutes=3)
        source.poll_once()
        down = [i for i in ranked(source) if i["category"] == "source_down"]
        self.assertEqual(down[0]["title"], "Lightning data unavailable")

    def test_missing_reader_downloads_nothing(self):
        source, clock, bucket, _ = make(reader=False)
        source.turn_on()
        source.poll_once()
        self.assertEqual(bucket.calls, [])
        clock.now += timedelta(minutes=3)
        self.assertIn("python3-netcdf4", source.block()["message"])


class Rules(unittest.TestCase):

    def test_on_until_the_outlook_clears(self):
        source, clock, bucket, _ = make()
        source.turn_on()
        source.observe(forecast(LIKELY))
        self.assertTrue(source.block()["detection"]["outlook_seen"])
        clock.now += timedelta(hours=8)                        # no time limit with an outlook
        source.observe(forecast(LIKELY))
        self.assertTrue(source.on)
        source.observe(forecast(None, status="unavailable"))  # a missing forecast says nothing
        source.observe(forecast(None, status="stale"))
        self.assertTrue(source.on)
        source.observe(forecast(None), "ok")
        self.assertFalse(source.on)
        self.assertEqual(source.off_reason, "outlook_cleared")

    def test_automatic_off_waits_while_lightning_is_near(self):
        source, clock, bucket, _ = make()
        source.turn_on()
        source.poll_once()
        source.observe(forecast(LIKELY))
        clock.now += timedelta(seconds=20)
        bucket.flashes[bucket.keys_until(clock.now)[-1]] = [(12, 90)]
        source.poll_once()
        source.observe(forecast(None), "ok")
        self.assertTrue(source.on)
        self.assertTrue(source.detection()["off_waiting_for_hold"])
        clock.now += timedelta(minutes=31)
        source.observe(forecast(None), "ok")
        self.assertFalse(source.on)

    def test_hand_turn_on_asks_at_30_minutes_and_3_hours_then_stops_at_6(self):
        source, clock, bucket, _ = make()
        source.turn_on()
        clock.now += timedelta(minutes=29)
        self.assertIsNone(source.detection()["question"])
        clock.now += timedelta(minutes=1)
        self.assertEqual(source.detection()["question"]["stage"], "30min")
        self.assertTrue(source.keep_on())
        self.assertIsNone(source.detection()["question"])
        self.assertFalse(source.keep_on())
        clock.now = source.turned_on_at + timedelta(hours=3)
        self.assertEqual(source.detection()["question"]["stage"], "3h")
        source.keep_on()
        clock.now = source.turned_on_at + timedelta(hours=5, minutes=59)
        source.poll_once()
        self.assertTrue(source.on)
        clock.now = source.turned_on_at + timedelta(hours=6)
        source.poll_once()
        self.assertFalse(source.on)
        self.assertEqual(source.off_reason, "time_limit")

    def test_unanswered_question_stays_on(self):
        source, clock, bucket, _ = make()
        source.turn_on()
        clock.now += timedelta(hours=2)
        source.poll_once()
        self.assertTrue(source.on)
        self.assertEqual(source.detection()["question"]["stage"], "30min")
        clock.now += timedelta(hours=1)
        self.assertEqual(source.detection()["question"]["stage"], "3h")   # one question, now the 3-hour one

    def test_outlook_arriving_first_ends_the_questions_and_the_limit(self):
        source, clock, bucket, _ = make()
        source.turn_on()
        clock.now += timedelta(minutes=40)
        source.observe(forecast(LIKELY))
        d = source.detection()
        self.assertIsNone(d["question"])
        self.assertIsNone(d["auto_off_at"])

    def test_turn_off_answers_the_question(self):
        source, clock, bucket, _ = make()
        source.turn_on()
        clock.now += timedelta(minutes=31)
        source.turn_off()
        self.assertIsNone(source.detection()["question"])


class ReviewFindings(unittest.TestCase):
    """The code review of 2026-11-20: holds must never be lost."""

    def with_flash(self, miles=4, bearing=90):
        source, clock, bucket, path = make()
        source.turn_on()
        source.poll_once()
        clock.now += timedelta(seconds=20)
        bucket.flashes[bucket.keys_until(clock.now)[-1]] = [(miles, bearing)]
        source.poll_once()
        return source, clock, bucket, path

    def test_hold_shown_after_a_restart_before_new_data(self):
        source, clock, bucket, path = self.with_flash()
        bucket.fail = urllib.error.URLError("offline")
        clock.now += timedelta(minutes=10)
        again, _, _, _ = make(path=path, clock=clock, bucket=bucket)
        again.poll_once()
        b = again.block()
        self.assertEqual(b["status"], "unavailable")
        self.assertTrue(b["within_5mi"])
        self.assertEqual(ranked(again)[0]["category"], "lightning_5mi")
        clock.now += timedelta(minutes=5)                       # past the start grace: still shown
        self.assertEqual(ranked(again)[0]["category"], "lightning_5mi")

    def test_unknown_not_false_without_data(self):
        source, clock, bucket, path = self.with_flash(miles=15)
        bucket.fail = urllib.error.URLError("offline")
        source.turn_off()
        source.turn_on()
        b = source.block()
        self.assertTrue(b["within_20mi"])
        self.assertIsNone(b["within_5mi"])                    # no data: not "no lightning at 5 mi"

    def test_hold_survives_a_grid_change(self):
        source, clock, bucket, path = self.with_flash(miles=3)
        source.settings["station"]["field"]["grid"] = "FM18lu"   # the next subsquare south
        source.poll_once()
        b = source.block()
        self.assertTrue(b["within_10mi"])
        self.assertEqual(len(source.flashes), 1)

    def test_nws_alerts_outage_is_not_the_outlook_clearing(self):
        source, clock, bucket, _ = make()
        source.turn_on()
        source.observe(forecast(LIKELY), "ok")
        source.observe(forecast(None), "unavailable")
        self.assertTrue(source.on)
        source.observe(forecast(None), "stale")
        self.assertFalse(source.on)

    def test_damaged_file_is_skipped_not_retried(self):
        source, clock, bucket, _ = make()
        source.turn_on()
        source.poll_once()
        bad = None
        real_read = bucket.read

        def read(data):
            if data.rstrip(b"\0").decode() == bad:
                raise OSError("HDF error")
            return real_read(data)
        source._read = read
        clock.now += timedelta(seconds=40)
        keys = bucket.keys_until(clock.now)
        bad = keys[-2]
        bucket.flashes[keys[-1]] = [(8, 0)]
        source.poll_once()
        self.assertEqual(source.last_key, keys[-1])
        self.assertTrue(source.block()["within_10mi"])
        self.assertIn("couldn't be read", source.block()["message"] or source._error)

    def test_damaged_state_values(self):
        path = os.path.join(tempfile.mkdtemp(), "fl.json")
        with open(path, "w") as f:
            json.dump({"on": True, "turned_on_at": "2026-07-04T18:00:00Z", "flashes": 7}, f)
        source, _, _, _ = make(path=path)
        self.assertTrue(source.on)
        self.assertEqual(source.flashes, [])
        with open(path, "w") as f:
            json.dump({"on": True, "turned_on_at": 5, "answered": [1]}, f)
        source, _, _, _ = make(path=path)
        self.assertFalse(source.on)

    def test_switching_satellite_starts_from_the_newest_file(self):
        source, clock, bucket, _ = make()
        source.turn_on()
        source.poll_once()
        source.last_key = source.last_key.replace("_G19_", "_G99_")   # sorts after everything
        source.settings["field_lightning"]["satellite"] = "west"
        source.settings["sources"]["glm_west_url"] = BASE
        clock.now += timedelta(seconds=20)
        before = len(bucket.files_read())
        source.poll_once()
        self.assertEqual(len(bucket.files_read()), before + 1)

    def test_a_flash_read_after_the_clear_counts(self):
        source, clock, bucket, _ = make()
        source.turn_on()
        source.poll_once()
        source.clear_hold()
        clock.now += timedelta(seconds=20)                     # the file began before the Clear
        bucket.flashes[bucket.keys_until(clock.now)[-1]] = [(4, 0)]
        source.poll_once()
        self.assertTrue(source.block()["within_5mi"])

    def test_economy_has_a_minute_more_before_late(self):
        source, clock, bucket, _ = make()
        source.settings["field_lightning"]["level"] = "economy"
        source.turn_on()
        source.poll_once()
        bucket.fail = urllib.error.URLError("offline")
        clock.now += timedelta(minutes=3, seconds=10)
        self.assertEqual(source.block()["status"], "ok")


class TurnedOffWithLightningRecent(unittest.TestCase):

    def setUp(self):
        self.source, self.clock, self.bucket, _ = make()
        self.source.turn_on()
        self.source.poll_once()
        self.clock.now += timedelta(seconds=20)
        self.bucket.flashes[self.bucket.keys_until(self.clock.now)[-1]] = [(16, 0)]
        self.source.poll_once()

    def test_card_until_30_minutes_after_the_last_flash(self):
        self.source.turn_off()
        items = ranked(self.source)
        card = [i for i in items if i["category"] == "lightning_off_recent"][0]
        self.assertIn("strikes within 20 mi at", card["title"])
        self.assertIn("not an all-clear", card["detail"])
        self.assertEqual(card["action"], "lightning_on")
        self.assertIsNone(self.source.block()["within_20mi"])   # off: unknown, never false
        self.clock.now += timedelta(minutes=31)
        self.assertNotIn("lightning_off_recent", categories(ranked(self.source)))

    def test_dismiss_and_turn_back_on(self):
        self.source.turn_off()
        self.assertTrue(self.source.dismiss_recent())
        self.assertNotIn("lightning_off_recent", categories(ranked(self.source)))
        self.assertFalse(self.source.dismiss_recent())
        self.source.turn_on()
        self.assertIsNone(self.source.detection()["recent_at_off"])
        self.assertTrue(self.source.block()["within_20mi"] in (None, True))

    def test_no_card_without_recent_lightning(self):
        source, clock, bucket, _ = make()
        source.turn_on()
        source.poll_once()
        source.turn_off()
        self.assertNotIn("lightning_off_recent", categories(ranked(source)))


class ForecastCard(unittest.TestCase):

    def test_turn_on_action_only_while_off(self):
        source, clock, bucket, _ = make()
        card = [i for i in ranked(source, LIKELY) if i["category"].startswith("lightning_")][0]
        self.assertEqual(card["action"], "lightning_on")
        source.turn_on()
        source.poll_once()
        card = [i for i in ranked(source, LIKELY) if i["category"].startswith("lightning_")][0]
        self.assertNotIn("action", card)


class Saved(unittest.TestCase):

    def test_survives_a_restart(self):
        source, clock, bucket, path = make()
        source.turn_on()
        source.poll_once()
        clock.now += timedelta(seconds=20)
        bucket.flashes[bucket.keys_until(clock.now)[-1]] = [(4, 180)]
        source.poll_once()
        clock.now += timedelta(minutes=31)
        source.keep_on()
        again, _, _, _ = make(path=path, clock=clock, bucket=bucket)
        self.assertTrue(again.on)
        self.assertEqual(again.answered, 1)
        self.assertEqual(again.turned_on_at, source.turned_on_at)
        self.assertEqual(len(again.flashes), 1)
        self.assertEqual(again.last_key, source.last_key)

    def test_damaged_file_starts_off(self):
        path = os.path.join(tempfile.mkdtemp(), "fl.json")
        with open(path, "w") as f:
            f.write("{ nope")
        source, _, _, _ = make(path=path)
        self.assertFalse(source.on)
        with open(path, "w") as f:
            json.dump({"on": True}, f)                         # on, but no turn-on time
        source, _, _, _ = make(path=path)
        self.assertFalse(source.on)


class HomeTestAndCounter(unittest.TestCase):

    def test_home_test_runs_at_home_and_reports_on_the_system_page(self):
        source, clock, bucket, _ = make(mode="home")
        self.assertIsNone(source.system_block()["running"])
        source.set_home_test(True)
        source.poll_once()
        s = source.system_block()
        self.assertEqual(s["running"], "home_test")
        self.assertEqual(s["status"], "ok")
        self.assertIsNotNone(s["test"])
        self.assertGreater(source.bytes_today, 1000)
        self.assertEqual(s["files_today"], 1)
        source.set_home_test(False)
        self.assertIsNone(source.system_block()["running"])

    def test_a_new_site_starts_afresh(self):
        source, clock, bucket, _ = make()
        source.turn_on()
        source.poll_once()
        lat, lon = point_at(3, 0)
        source.flashes.append({"time": clock.now, "seen": clock.now, "distance_mi": 3.0, "bearing": "N",
                               "lat": lat, "lon": lon})
        source.settings["station"]["field"]["grid"] = "EM12aa"     # hundreds of miles away
        source.poll_once()
        self.assertEqual(source.flashes, [])


class SettingsChecks(unittest.TestCase):

    def test_lightning_settings(self):
        current = make_settings()
        errors, _, _ = settings_api.check({"field_lightning": {"level": "economy", "satellite": "west"}}, current)
        self.assertEqual(errors, [])
        errors, _, _ = settings_api.check({"field_lightning": {"level": "max", "colour": 1}}, current)
        self.assertEqual(len(errors), 2)
        errors, _, _ = settings_api.check({"sources": {"glm_west_url": "https://noaa-goes18.s3.amazonaws.com"}}, current)
        self.assertEqual(errors, [])
        errors, _, _ = settings_api.check({"thresholds": {"glm_ask_first_min": 200}}, current)
        self.assertTrue(any("ask first" in e for e in errors))
        errors, _, _ = settings_api.check({"thresholds": {"lightning_field_outer_mi": 8}}, current)
        self.assertTrue(any("lightning_field_outer_mi" in e for e in errors))


if __name__ == "__main__":
    unittest.main(verbosity=2)
