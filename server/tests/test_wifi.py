#!/usr/bin/env python3
"""Tests for Wi-Fi from the touchscreen (2026-11-17).

No NetworkManager needed: nmcli is replaced by a stand-in that answers like nmcli 1.52.1 did
on the Pi on 2026-11-17 (one home network heard from four access points, a saved hotspot),
with made-up network names.

    python server/tests/test_wifi.py      (or: python -m pytest server/tests)
"""

import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from hamops_console import wifi  # noqa: E402

SECRET = "correct horse battery"


class FakeNM:
    """Plays nmcli. Keeps saved connections; records every command."""

    def __init__(self, allowed=True):
        self.allowed = allowed
        self.calls = []
        self.conns = [  # name, uuid, type, priority, active, ssid
            ["netplan-wlan0-homenet", "u-home", "802-11-wireless", 0, True, "HomeNet"],
            ["lo", "u-lo", "loopback", 0, True, None],
            ["Wired connection 1", "u-wired", "802-3-ethernet", -999, False, None],
            ["hotspot", "u-hot", "802-11-wireless", 0, False, "PhoneHot"],
        ]
        self.heard = [("HomeNet", 100, "WPA2"), ("HomeNet", 76, "WPA2"), ("HomeNet", 55, "WPA2"),
                      ("Site Guest", 64, "WPA2"), ("Park:Public", 20, ""), ("", 40, "WPA2"),
                      ("Corp", 50, "WPA2 802.1X")]
        self.join_fails = None

    def _ok(self, out=""):
        return subprocess.CompletedProcess([], 0, out, "")

    def _err(self, msg):
        return subprocess.CompletedProcess([], 10, "", msg)

    @staticmethod
    def esc(s):
        return s.replace("\\", "\\\\").replace(":", "\\:")

    def __call__(self, args, **kw):
        assert args[0] == "nmcli"
        a = args[1:]
        self.calls.append(a)
        if a[:4] == ["-t", "-f", "PERMISSION,VALUE", "general"]:
            v = "yes" if self.allowed else "auth"
            return self._ok("\n".join(f"{p}:{v}" for p in wifi.PERMISSIONS.values()) +
                            "\norg.freedesktop.NetworkManager.enable-disable-wifi:no\n")
        if a[:3] == ["-t", "-f", "NAME,UUID,TYPE,AUTOCONNECT-PRIORITY,ACTIVE"]:
            return self._ok("".join(f"{self.esc(c[0])}:{c[1]}:{c[2]}:{c[3]}:{'yes' if c[4] else 'no'}\n"
                                    for c in self.conns))
        if a[:3] == ["-t", "-g", "802-11-wireless.ssid"]:
            c = next(c for c in self.conns if c[1] == a[-1])
            return self._ok(self.esc(c[5] or "") + "\n")
        if a[:3] == ["-t", "-f", "SSID,SIGNAL,SECURITY"]:
            return self._ok("".join(f"{self.esc(s)}:{g}:{x}\n" for s, g, x in self.heard))
        if a[:3] == ["device", "wifi", "rescan"]:
            return self._ok() if self.allowed else self._err("Error: Scanning not allowed: not authorized.")
        if a[:3] == ["device", "wifi", "connect"]:
            if not self.allowed:
                return self._err("Error: Failed to add/activate new connection: Not authorized to control networking.")
            ssid = a[3]
            self.conns.append([ssid, "u-new", "802-11-wireless", 0, False, ssid])   # NM saves first
            if self.join_fails:
                return self._err(self.join_fails)
            for c in self.conns:
                c[4] = c[1] in ("u-new", "u-lo")
            return self._ok("Device 'wlan0' successfully activated\n")
        if a[:2] == ["connection", "up"]:
            for c in self.conns:
                if c[2] == "802-11-wireless":
                    c[4] = c[1] == a[-1]
            return self._ok()
        if a[:2] == ["connection", "modify"]:
            c = next(c for c in self.conns if c[1] == a[3])
            if a[4] == "connection.autoconnect-priority":
                c[3] = int(a[5])
            return self._ok()
        if a[:2] == ["connection", "delete"]:
            self.conns = [c for c in self.conns if c[1] != a[-1]]
            return self._ok()
        raise AssertionError(f"unexpected nmcli {a}")


def make(**kw):
    nm = FakeNM(**kw)
    return wifi.WiFi(runner=nm, run=lambda job: job()), nm


class Parsing(unittest.TestCase):

    def test_split_terse(self):
        self.assertEqual(wifi.split_terse(r"Park\:Public:20:"), ["Park:Public", "20", ""])
        self.assertEqual(wifi.split_terse(r"a\\b:c"), ["a\\b", "c"])

    def test_checks(self):
        self.assertTrue(wifi.ssid_ok("Site Guest"))
        self.assertFalse(wifi.ssid_ok(""))
        self.assertFalse(wifi.ssid_ok("x" * 33))
        self.assertFalse(wifi.ssid_ok("bad\nname"))
        self.assertTrue(wifi.password_ok("12345678"))
        self.assertFalse(wifi.password_ok("short"))
        self.assertFalse(wifi.password_ok("x" * 64))
        self.assertFalse(wifi.password_ok("café-password"))


class Tab(unittest.TestCase):

    def test_block(self):
        w, _ = make()
        b = w.block()
        self.assertTrue(b["available"] and b["can_change"])
        self.assertEqual([(s["ssid"], s["rank"], s["active"], s["in_range"]) for s in b["saved"]],
                         [("HomeNet", 1, True, True), ("PhoneHot", 2, False, False)])
        self.assertEqual(b["saved"][0]["bars"], 4)
        names = [n["ssid"] for n in b["nearby"]]
        self.assertEqual(names, ["HomeNet", "Site Guest", "Corp", "Park:Public"])   # one row per name, no hidden
        park = next(n for n in b["nearby"] if n["ssid"] == "Park:Public")
        self.assertTrue(park["open"])
        self.assertTrue(next(n for n in b["nearby"] if n["ssid"] == "Corp")["enterprise"])
        self.assertEqual(b["nearby"][0]["saved_id"], "u-home")

    def test_without_the_polkit_rule(self):
        w, _ = make(allowed=False)
        b = w.block()
        self.assertFalse(b["can_change"])
        self.assertIn("polkit rule", b["message"])
        self.assertEqual(len(b["saved"]), 2)            # reading still works

    def test_nmcli_missing(self):
        def boom(*a, **k):
            raise FileNotFoundError("nmcli")
        b = wifi.WiFi(runner=boom, run=lambda j: j()).block()
        self.assertFalse(b["available"])


class Actions(unittest.TestCase):

    def test_scan(self):
        w, nm = make()
        self.assertEqual(w.scan(), (True, None))
        self.assertIn(["device", "wifi", "rescan"], nm.calls)
        b = w.block()
        self.assertFalse(b["scanning"])
        self.assertIsNotNone(b["scanned_at"])

    def test_scan_refused(self):
        w, _ = make(allowed=False)
        w.scan()
        self.assertIn("polkit rule", w.block()["scan_error"])

    def test_join_new_goes_last_and_password_never_returned_or_logged(self):
        w, nm = make()
        with self.assertLogs("hamops_console.wifi", level="INFO") as logs:
            self.assertEqual(w.join("Site Guest", SECRET), (True, None))
        b = w.block()
        self.assertEqual(b["job"], {"ssid": "Site Guest", "state": "joined", "message": None})
        self.assertEqual([s["ssid"] for s in b["saved"]], ["HomeNet", "PhoneHot", "Site Guest"])
        self.assertTrue(b["saved"][2]["active"])
        self.assertNotIn(SECRET, repr(b))
        self.assertFalse(any(SECRET in line for line in logs.output))
        self.assertFalse(any("Site Guest" in line for line in logs.output))
        new = next(c for c in nm.conns if c[1] == "u-new")
        self.assertEqual(new[3], -1)                    # below every saved network

    def test_wrong_password_saves_nothing(self):
        w, nm = make()
        nm.join_fails = "Error: Connection activation failed: Secrets were required, but not provided."
        w.join("Site Guest", SECRET)
        b = w.block()
        self.assertEqual(b["job"]["state"], "failed")
        self.assertEqual(b["job"]["message"], "The password wasn't accepted.")
        self.assertNotIn("Site Guest", [s["ssid"] for s in b["saved"]])
        self.assertIn(["connection", "up", "uuid", "u-home"], nm.calls)     # back to the network it was on

    def test_saved_network_is_just_brought_up(self):
        w, nm = make()
        w.join("PhoneHot")
        self.assertIn(["connection", "up", "uuid", "u-hot"], nm.calls)
        self.assertFalse(any(c[:3] == ["device", "wifi", "connect"] for c in nm.calls))

    def test_open_network_without_password(self):
        w, nm = make()
        w.join("Park:Public")
        connect = next(c for c in nm.calls if c[:3] == ["device", "wifi", "connect"])
        self.assertEqual(connect, ["device", "wifi", "connect", "Park:Public"])

    def test_join_refused_without_rule(self):
        w, _ = make(allowed=False)
        w.join("Site Guest", SECRET)
        self.assertIn("polkit rule", w.block()["job"]["message"])

    def test_bad_input(self):
        w, _ = make()
        self.assertFalse(w.join("", SECRET)[0])
        self.assertFalse(w.join("Site Guest", "short")[0])

    def test_one_join_at_a_time(self):
        jobs = []
        w = wifi.WiFi(runner=FakeNM(), run=jobs.append)
        self.assertTrue(w.join("Site Guest", SECRET)[0])
        self.assertEqual(w.join("Park:Public"), (False, "Already joining a network"))

    def test_reorder(self):
        w, nm = make()
        self.assertIsNone(w.reorder(["u-hot", "u-home"]))
        self.assertEqual([s["ssid"] for s in w.block()["saved"]], ["PhoneHot", "HomeNet"])
        self.assertEqual(w.reorder(["u-hot"]), "Send every saved network once, first choice first")
        self.assertEqual(w.reorder(["u-hot", "u-wired"]), "Send every saved network once, first choice first")

    def test_forget(self):
        w, nm = make()
        self.assertIsNone(w.forget("u-hot"))
        self.assertEqual([s["ssid"] for s in w.block()["saved"]], ["HomeNet"])
        self.assertEqual(w.forget("u-wired"), "That network isn't saved")   # only Wi-Fi can be forgotten here


class Endpoints(unittest.TestCase):

    def setUp(self):
        import tempfile
        from hamops_console import settings as settings_mod
        from hamops_console.app import create_app
        from hamops_console.display import Display
        s = settings_mod.load(os.path.join(tempfile.mkdtemp(), "missing.json"))
        self.w, self.nm = make()
        self.client = create_app(s, display=Display(s, device=None), wifi=self.w,
                                 settings_file=os.path.join(tempfile.mkdtemp(), "s.json")).test_client()

    def test_get_join_and_no_secret_in_replies(self):
        r = self.client.get("/api/wifi")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        r = self.client.post("/api/wifi/join", json={"ssid": "Site Guest", "password": SECRET})
        self.assertEqual(r.status_code, 202)
        self.assertNotIn(SECRET, self.client.get("/api/wifi").get_data(as_text=True))
        r = self.client.post("/api/wifi/join", json={"ssid": "Site Guest", "password": "short"})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("short", r.get_data(as_text=True).replace("8 to 63", ""))

    def test_order_forget_scan(self):
        self.assertEqual(self.client.post("/api/wifi/order", json={"ids": ["u-hot", "u-home"]}).status_code, 200)
        self.assertEqual(self.client.post("/api/wifi/order", json={"ids": []}).status_code, 400)
        self.assertEqual(self.client.post("/api/wifi/forget", json={"id": "u-hot"}).status_code, 200)
        self.assertEqual(self.client.post("/api/wifi/scan").status_code, 202)


if __name__ == "__main__":
    unittest.main(verbosity=2)
