#!/usr/bin/env python3
"""Tests for screen brightness, Sleep, Shut down / Restart and page caching (2026-11-11).

No Pi needed: the backlight is a folder of files shaped like /sys/class/backlight/<name>/,
and systemctl is a stand-in. Nothing here touches the real screen or powers anything off.

    python server/tests/test_display.py      (or: python -m pytest server/tests)
"""

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from hamops_console import power, settings as settings_mod  # noqa: E402
from hamops_console.app import create_app  # noqa: E402
from hamops_console.display import Display, find_backlight, is_night, level  # noqa: E402


def fake_backlight(max_brightness=31, bl_power=True):
    root = Path(tempfile.mkdtemp())
    dev = root / "11-0045"
    dev.mkdir()
    (dev / "max_brightness").write_text(f"{max_brightness}\n")
    (dev / "brightness").write_text(f"{max_brightness}\n")
    if bl_power:
        (dev / "bl_power").write_text("0\n")
    return root, dev


def settings():
    return settings_mod.load(os.path.join(tempfile.mkdtemp(), "missing.json"))


def at(h, m=0):
    return lambda: datetime(2026, 11, 11, h, m)


class Schedule(unittest.TestCase):

    def test_night_across_midnight(self):
        self.assertTrue(is_night(datetime(2026, 11, 11, 23, 0), "22:00", "07:00"))
        self.assertTrue(is_night(datetime(2026, 11, 11, 6, 59), "22:00", "07:00"))
        self.assertFalse(is_night(datetime(2026, 11, 11, 7, 0), "22:00", "07:00"))
        self.assertFalse(is_night(datetime(2026, 11, 11, 12, 0), "22:00", "07:00"))

    def test_night_same_day_and_none(self):
        self.assertTrue(is_night(datetime(2026, 11, 11, 2, 0), "01:00", "05:00"))
        self.assertFalse(is_night(datetime(2026, 11, 11, 6, 0), "01:00", "05:00"))
        self.assertFalse(is_night(datetime(2026, 11, 11, 6, 0), "05:00", "05:00"))
        self.assertFalse(is_night(datetime(2026, 11, 11, 6, 0), None, "05:00"))

    def test_level_never_zero(self):
        self.assertEqual(level(100, 31), 31)
        self.assertEqual(level(20, 31), 6)
        self.assertEqual(level(5, 10), 1)
        self.assertEqual(level(0, 255), 3)      # clamped to 1 %


class Backlight(unittest.TestCase):

    def test_finds_device(self):
        root, dev = fake_backlight()
        self.assertEqual(find_backlight(root), dev)
        self.assertIsNone(find_backlight(Path(tempfile.mkdtemp())))
        self.assertIsNone(find_backlight(Path("/no/such/folder")))

    def test_day_and_night_brightness(self):
        _, dev = fake_backlight(max_brightness=255)
        d = Display(settings(), device=dev, clock=at(12))
        self.assertTrue(d.apply())
        self.assertEqual((dev / "brightness").read_text(), str(round(255 * 0.8)))
        d.clock = at(23)
        d.apply()
        self.assertEqual((dev / "brightness").read_text(), str(round(255 * 0.2)))
        self.assertTrue(d.block()["night"])

    def test_sleep_and_wake(self):
        _, dev = fake_backlight()
        d = Display(settings(), device=dev, clock=at(12))
        d.sleep()
        self.assertEqual((dev / "brightness").read_text(), "0")
        self.assertEqual((dev / "bl_power").read_text(), "4")
        self.assertTrue(d.block()["asleep"])
        self.assertEqual(d.block()["brightness_pct"], 0)
        d.apply()                                   # the schedule doesn't wake it
        self.assertEqual((dev / "brightness").read_text(), "0")
        d.wake()
        self.assertEqual((dev / "bl_power").read_text(), "0")
        self.assertEqual((dev / "brightness").read_text(), str(round(31 * 0.8)))

    def test_tick_writes_only_on_change(self):
        _, dev = fake_backlight()
        d = Display(settings(), device=dev, clock=at(12))
        d.tick()
        self.assertEqual((dev / "brightness").read_text(), str(round(31 * 0.8)))
        (dev / "brightness").write_text("7")            # unchanged target: left alone
        d.tick()
        self.assertEqual((dev / "brightness").read_text(), "7")
        d.clock = at(22, 30)                            # night begins
        d.tick()
        self.assertEqual((dev / "brightness").read_text(), str(round(31 * 0.2)))

    def test_no_bl_power_file(self):
        _, dev = fake_backlight(bl_power=False)
        d = Display(settings(), device=dev, clock=at(12))
        self.assertTrue(d.sleep())
        self.assertEqual((dev / "brightness").read_text(), "0")

    def test_bl_power_root_only(self):
        """As on the Pi before the udev rule: brightness writable, bl_power not."""
        if os.geteuid() == 0:
            self.skipTest("root can write read-only files")
        _, dev = fake_backlight()
        os.chmod(dev / "bl_power", 0o444)
        d = Display(settings(), device=dev, clock=at(12))
        self.assertTrue(d.sleep())
        self.assertEqual((dev / "brightness").read_text(), "0")
        self.assertTrue(d.wake())
        self.assertIsNone(d.block()["message"])

    def test_no_permission_is_reported_not_raised(self):
        if os.geteuid() == 0:
            self.skipTest("root can write read-only files")
        _, dev = fake_backlight()
        os.chmod(dev / "brightness", 0o444)
        d = Display(settings(), device=dev, clock=at(12))
        self.assertFalse(d.apply())
        self.assertIn("udev rule", d.block()["message"])
        self.assertFalse(d.block()["control"])

    def test_no_device(self):
        d = Display(settings(), device=None, clock=at(12))
        self.assertFalse(d.sleep())
        self.assertTrue(d.block()["asleep"])        # the page still covers itself
        self.assertFalse(d.block()["control"])


class Result:
    def __init__(self, code, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


class Power(unittest.TestCase):

    def test_commands(self):
        seen = []
        ok, msg = power.run("shutdown", runner=lambda c, **k: seen.append(c) or Result(0))
        self.assertTrue(ok)
        power.run("restart", runner=lambda c, **k: seen.append(c) or Result(0))
        self.assertEqual(seen, [["systemctl", "poweroff"], ["systemctl", "reboot"]])

    def test_refused_without_polkit_rule(self):
        ok, msg = power.run("shutdown", runner=lambda c, **k: Result(
            1, err="Call to PowerOff failed: Interactive authentication required."))
        self.assertFalse(ok)
        self.assertIn("polkit rule", msg)

    def test_other_failures(self):
        self.assertEqual(power.run("explode"), (False, "action must be 'shutdown' or 'restart'"))
        def boom(c, **k):
            raise subprocess.TimeoutExpired(c, 15)
        ok, msg = power.run("restart", runner=boom)
        self.assertFalse(ok)
        self.assertIn("TimeoutExpired", msg)


class Endpoints(unittest.TestCase):

    def setUp(self):
        dashboard = tempfile.mkdtemp()
        Path(dashboard, "index.html").write_text("<title>home</title>")
        Path(dashboard, "common.js").write_text("// js")
        self.root, self.dev = fake_backlight()
        self.calls = []
        s = settings()
        self.display = Display(s, device=self.dev, clock=at(12))
        self.client = create_app(s, dashboard_dir=dashboard, display=self.display,
                                 power_runner=lambda c, **k: self.calls.append(c) or Result(0)).test_client()

    def test_pages_revalidate(self):
        with self.client.get("/") as r:
            self.assertEqual(r.headers["Cache-Control"], "no-cache")
            etag = r.headers.get("ETag")
        with self.client.get("/static/common.js") as r:
            self.assertEqual(r.headers["Cache-Control"], "no-cache")
        if etag:
            with self.client.get("/", headers={"If-None-Match": etag}) as r:
                self.assertEqual(r.status_code, 304)
        self.assertEqual(self.client.get("/api/settings").headers["Cache-Control"], "no-store")

    def test_sleep_wake(self):
        r = self.client.post("/api/display", json={"action": "sleep"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["display"]["asleep"])
        self.assertEqual((self.dev / "brightness").read_text(), "0")
        self.assertTrue(self.client.get("/api/system").get_json()["display"]["asleep"])
        r = self.client.post("/api/display", json={"action": "wake"})
        self.assertFalse(r.get_json()["display"]["asleep"])
        self.assertEqual(self.client.post("/api/display", json={"action": "dance"}).status_code, 400)

    def test_settings_change_applies(self):
        self.client.put("/api/settings", json={"display": {"day_brightness_pct": 50}})
        self.display.apply()       # what the poked background check does
        self.assertEqual((self.dev / "brightness").read_text(), str(round(31 * 0.5)))

    def test_power(self):
        self.assertEqual(self.client.post("/api/power", json={"action": "restart"}).get_json(), {"started": "restart"})
        self.assertEqual(self.calls, [["systemctl", "reboot"]])
        self.assertEqual(self.client.post("/api/power", json={}).status_code, 400)

    def test_power_refused(self):
        client = create_app(settings(), dashboard_dir=tempfile.mkdtemp(), display=self.display,
                            power_runner=lambda c, **k: Result(1, err="Access denied")).test_client()
        r = client.post("/api/power", json={"action": "shutdown"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("polkit", r.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
