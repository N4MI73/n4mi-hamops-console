#!/usr/bin/env python3
"""Tests for GPS as a location source (gps.py, location.py).

No gpsd and no receiver: reports are fed to GpsSource.handle() with a controlled
clock, and the reader loop is tested against a stand-in socket. Positions use
the neutral test location FM18lv (never the home grid).

    python server/tests/test_gps.py      (or: python -m pytest server/tests)
"""

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from hamops_console import gps as gps_mod, location, settings as settings_mod, settings_api  # noqa: E402
from hamops_console.gps import GpsSource, to_grid  # noqa: E402

T0 = datetime(2026, 11, 18, 20, 0, 0, tzinfo=timezone.utc)
# Inside FM18lv: lon -77.0833 .. -77.0, lat 38.875 .. 38.9167
IN_LV = (38.89, -77.04)
IN_LW = (38.93, -77.04)    # the subsquare to the north
EDGE_LV = (38.9165, -77.04)
EDGE_LW = (38.9169, -77.04)


def tpv(pos=None, mode=3, when=T0, gps_time=None):
    msg = {"class": "TPV", "device": "/dev/ttyUSB0", "mode": mode}
    if pos:
        msg["lat"], msg["lon"] = pos
    t = gps_time or when
    msg["time"] = t.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return msg


def source(**kw):
    kw.setdefault("state_file", os.path.join(tempfile.mkdtemp(), "gps.json"))
    kw.setdefault("device_path", "/nonexistent/ttyUSB0")
    return GpsSource(**kw)


def feed(src, pos, start, seconds, step=1, **kw):
    """One report a second from `start` for `seconds`; returns the last time."""
    t = start
    for i in range(0, seconds + 1, step):
        t = start + timedelta(seconds=i)
        src.handle(tpv(pos, when=t, **kw), now=t)
    return t


class Grid(unittest.TestCase):
    def test_known_points(self):
        self.assertEqual(to_grid(*IN_LV), "FM18lv")
        self.assertEqual(to_grid(*IN_LW), "FM18lw")
        self.assertEqual(to_grid(0.0, 0.0), "JJ00aa")
        self.assertEqual(to_grid(-34.98, 138.55), "PF95ga")
        self.assertEqual(to_grid(90, 180), "RR99xx")           # the corner stays in range
        self.assertEqual(to_grid(-90, -180), "AA00aa")

    def test_out_of_range(self):
        with self.assertRaises(ValueError):
            to_grid(91, 0)

    def test_round_trip_with_centre(self):
        from hamops_console import grid as grid_mod
        for g in ("FM18lv", "EN52wa", "JO01ab", "QF56od"):
            self.assertEqual(to_grid(*grid_mod.centre(g)), g)


class Hold(unittest.TestCase):
    def test_first_grid_after_two_minutes(self):
        src = source()
        t = feed(src, IN_LV, T0, 119)
        self.assertIsNone(src.grid())
        self.assertEqual(src.block(now=t)["grid_pending"], "FM18lv")
        t = feed(src, IN_LV, t + timedelta(seconds=1), 0)
        self.assertEqual(src.grid(), "FM18lv")
        self.assertIsNone(src.block(now=t)["grid_pending"])

    def test_edge_jitter_never_switches(self):
        src = source()
        t = feed(src, EDGE_LV, T0, 130)
        self.assertEqual(src.grid(), "FM18lv")
        # Alternate across the edge every 30 s for an hour: no 2-minute run in lw.
        for k in range(120):
            pos = EDGE_LW if k % 2 == 0 else EDGE_LV
            t = feed(src, pos, t + timedelta(seconds=1), 29)
        self.assertEqual(src.grid(), "FM18lv")

    def test_moved_site_switches_after_hold(self):
        src = source()
        t = feed(src, IN_LV, T0, 130)
        t = feed(src, IN_LW, t + timedelta(seconds=1), 125)
        self.assertEqual(src.grid(), "FM18lw")

    def test_no_fix_reports_change_nothing(self):
        src = source()
        feed(src, None, T0, 300, mode=1)
        self.assertIsNone(src.grid())
        feed(src, IN_LV, T0, 300, mode=1)      # a position without a fix isn't used
        self.assertIsNone(src.grid())

    def test_2d_fix_counts(self):
        src = source()
        t = feed(src, IN_LV, T0, 121, mode=2)
        self.assertEqual(src.grid(), "FM18lv")
        self.assertEqual(src.block(now=t)["fix"], "2d")


class ReviewFixes(unittest.TestCase):
    """Points from the code review, 2026-11-18."""

    def test_gap_restarts_the_hold(self):
        src = source()
        feed(src, IN_LV, T0, 130)
        src.handle(tpv(IN_LW, when=T0 + timedelta(seconds=200)), now=T0 + timedelta(seconds=200))
        later = T0 + timedelta(seconds=600)       # long gap with no reports at all
        src.handle(tpv(IN_LW, when=later), now=later)
        self.assertEqual(src.grid(), "FM18lv")    # one fix after a gap isn't 2 minutes held

    def test_no_fix_report_restarts_the_hold(self):
        src = source()
        t = feed(src, IN_LV, T0, 130)
        t = feed(src, IN_LW, t + timedelta(seconds=1), 100)
        src.handle(tpv(None, mode=1, when=t), now=t)
        t = feed(src, IN_LW, t + timedelta(seconds=1), 60)
        self.assertEqual(src.grid(), "FM18lv")
        feed(src, IN_LW, t + timedelta(seconds=1), 120)
        self.assertEqual(src.grid(), "FM18lw")

    def test_saved_grid_unconfirmed_until_a_fix(self):
        path = os.path.join(tempfile.mkdtemp(), "gps.json")
        feed(source(state_file=path), IN_LV, T0, 121)
        again = source(state_file=path)
        self.assertFalse(again.confirmed())
        self.assertFalse(again.block(now=T0)["grid_confirmed"])
        again.handle(tpv(None, mode=1), now=T0)
        self.assertFalse(again.confirmed())
        again.handle(tpv(IN_LW), now=T0)          # any fix confirms the GPS is working this run
        self.assertTrue(again.confirmed())
        self.assertEqual(again.grid(), "FM18lv")  # the new subsquare still waits out its hold

    def test_offset_cleared_when_fix_has_no_time(self):
        src = source()
        src.handle(tpv(IN_LV, gps_time=T0 + timedelta(seconds=40)), now=T0)
        msg = tpv(IN_LV)
        del msg["time"]
        src.handle(msg, now=T0)
        self.assertIsNone(src.block(now=T0)["time_check"])


class Saved(unittest.TestCase):
    def test_saved_and_reloaded_without_coordinates(self):
        path = os.path.join(tempfile.mkdtemp(), "sub", "gps.json")
        src = source(state_file=path)
        feed(src, IN_LV, T0, 121)
        doc = json.loads(open(path).read())
        self.assertEqual(doc["grid"], "FM18lv")
        self.assertNotIn("lat", json.dumps(doc))
        again = source(state_file=path)
        self.assertEqual(again.grid(), "FM18lv")     # used after a restart, before any fix
        self.assertFalse(os.path.exists(path[:-5] + ".tmp"))

    def test_damaged_file_ignored(self):
        path = os.path.join(tempfile.mkdtemp(), "gps.json")
        for text in ("not json", "[]", '{"grid": 7}', '{"grid": "FM18"}', '{"grid": "hello!"}', '{"grid": "ZZ99aa"}'):
            with open(path, "w") as f:
                f.write(text)
            self.assertIsNone(source(state_file=path).grid(), text)


class Block(unittest.TestCase):
    def test_nothing_heard(self):
        b = source().block(now=T0)
        self.assertFalse(b["present"])
        self.assertIsNone(b["fix"])
        self.assertEqual(b["message"], "gpsd isn't answering")

    def test_receiver_without_fix(self):
        src = source()
        src._connected = True
        src.handle({"class": "SKY", "nSat": 0, "uSat": 0}, now=T0)
        src.handle(tpv(None, mode=1), now=T0)
        b = src.block(now=T0)
        self.assertTrue(b["present"] and b["reporting"])
        self.assertIsNone(b["fix"])
        self.assertEqual(b["satellites_seen"], 0)
        self.assertEqual(b["message"], "No fix yet")
        self.assertIsNone(b["time_check"])    # no fix: GPS time isn't even compared

    def test_fix_and_satellites(self):
        src = source()
        src._connected = True
        sats = [{"PRN": n, "used": n < 7} for n in range(1, 12)]
        src.handle({"class": "SKY", "satellites": sats}, now=T0)
        src.handle(tpv(IN_LV), now=T0)
        b = src.block(now=T0)
        self.assertEqual((b["fix"], b["satellites_used"], b["satellites_seen"]), ("3d", 6, 11))
        self.assertEqual(b["position_grid"], "FM18lv")
        self.assertIsNone(b["message"])

    def test_receiver_unplugged(self):
        src = source()
        src._connected = True
        feed(src, IN_LV, T0, 121)
        later = T0 + timedelta(seconds=200)
        b = src.block(now=later)
        self.assertFalse(b["reporting"])
        self.assertIsNone(b["fix"])
        self.assertEqual(b["grid"], "FM18lv")     # last accepted grid kept
        self.assertEqual(b["message"], "No GPS receiver reporting")
        self.assertIsNotNone(b["last_fix_at"])

    def test_time_check(self):
        src = source()
        src.handle(tpv(IN_LV, gps_time=T0 + timedelta(seconds=0.4)), now=T0)
        self.assertEqual(src.block(now=T0)["time_check"], "agrees")
        src.handle(tpv(IN_LV, gps_time=T0 + timedelta(seconds=40)), now=T0)
        b = src.block(now=T0)
        self.assertEqual((b["time_check"], b["time_offset_sec"]), ("differs", 40.0))
        # This receiver's made-up date before a fix, if it ever came with one:
        src.handle(tpv(IN_LV, gps_time=datetime(2031, 6, 29, tzinfo=timezone.utc)), now=T0)
        self.assertEqual(src.block(now=T0)["time_check"], "gps_wrong")

    def test_odd_reports_ignored(self):
        src = source()
        for msg in (None, [], {"class": "TPV", "mode": "3", "lat": "x"},
                    {"class": "TPV", "mode": 3, "lat": True, "lon": 1},
                    {"class": "TPV", "mode": 3, "lat": 95.0, "lon": 0.0},
                    {"class": "SKY", "satellites": "none"}):
            src.handle(msg, now=T0)
        self.assertIsNone(src.grid())


class EffectiveGrid(unittest.TestCase):
    def setUp(self):
        self.settings = settings_mod.load("/nonexistent/settings.json")
        self.settings["station"]["home"]["grid"] = "FM18lv"
        self.settings["station"]["field"]["grid"] = "FM19aa"
        self.gps = source()
        location.use_gps(self.gps)

    def tearDown(self):
        location.use_gps(None)

    def test_manual(self):
        self.assertEqual(location.station_grid(self.settings, "field"), "FM19aa")
        self.assertEqual(location.grid_from(self.settings, "field"), "manual")

    def test_saved_grid_shows_unconfirmed(self):
        path = os.path.join(tempfile.mkdtemp(), "gps.json")
        feed(source(state_file=path), IN_LW, T0, 121)
        location.use_gps(source(state_file=path))
        self.settings["station"]["field"]["grid_source"] = "gps"
        self.assertEqual(location.station_grid(self.settings, "field"), "FM18lw")
        self.assertEqual(location.grid_from(self.settings, "field"), "gps_unconfirmed")

    def test_gps_waiting_then_gps(self):
        self.settings["station"]["field"]["grid_source"] = "gps"
        self.assertEqual(location.station_grid(self.settings, "field"), "FM19aa")
        self.assertEqual(location.grid_from(self.settings, "field"), "gps_waiting")
        feed(self.gps, IN_LW, T0, 121)
        self.assertEqual(location.station_grid(self.settings, "field"), "FM18lw")
        self.assertEqual(location.grid_from(self.settings, "field"), "gps")

    def test_home_never_from_gps(self):
        feed(self.gps, IN_LW, T0, 121)
        self.settings["station"]["home"]["grid_source"] = "gps"   # even if a file said so
        self.assertEqual(location.station_grid(self.settings, "home"), "FM18lv")
        self.assertEqual(location.grid_from(self.settings, "home"), "manual")

    def test_settings_saves_grid_source(self):
        path = os.path.join(tempfile.mkdtemp(), "settings.json")
        with open(path, "w") as f:
            json.dump({"station": {"field": {"grid": "FM19aa"}}}, f)
        live = settings_mod.load(path)
        ok, body = settings_api.SettingsStore(live, Path(path)).update({"station": {"field": {"grid_source": "gps"}}})
        self.assertTrue(ok, body)
        self.assertEqual(live["station"]["field"]["grid_source"], "gps")
        self.assertEqual(json.loads(open(path).read())["station"]["field"]["grid_source"], "gps")


class Reader(unittest.TestCase):
    """The loop against a stand-in gpsd on a local socket."""

    def test_reads_reports_from_gpsd(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        got = {}

        def gpsd():
            conn, _ = server.accept()
            got["watch"] = conn.recv(100)
            lines = [{"class": "VERSION", "release": "3.25"},
                     {"class": "SKY", "nSat": 9, "uSat": 7},
                     tpv(IN_LV, when=gps_mod.freshness.utcnow())]
            conn.sendall(b"".join(json.dumps(m).encode() + b"\n" for m in lines) + b"{broken\n")
            time.sleep(0.3)
            conn.close()

        threading.Thread(target=gpsd, daemon=True).start()
        src = source(address=("127.0.0.1", port))
        with self.assertRaises(ConnectionError):
            src._read_once()          # returns by raising when gpsd closes
        server.close()
        self.assertIn(b"WATCH", got["watch"])
        b = src.block()
        self.assertEqual((b["fix"], b["satellites_used"], b["position_grid"]), ("3d", 7, "FM18lv"))
        self.assertEqual(b["message"], "gpsd isn't answering")   # connection closed now


if __name__ == "__main__":
    unittest.main(verbosity=2)
