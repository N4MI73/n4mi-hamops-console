#!/usr/bin/env python3
"""Tests for the settings API: checking, saving to the file, and Test connections.

No network: the connection test uses a stand-in. Files go in temporary folders.

    python server/tests/test_settings_api.py      (or: python -m pytest server/tests)
"""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from hamops_console import settings as settings_mod, settings_api  # noqa: E402

ON_DISK = {
    "_comment": "Real settings for this Pi.",
    "mode": "home",
    "station": {"home": {"callsign": "N0CALL", "grid_source": "manual", "grid": "FM18lv"}},
    "sources": {"propmon_url": "http://propmon.example:8076", "sentinel_url": "http://sentinel.example:8085"},
    "server": {"host": "127.0.0.1", "port": 8090},
    "nws_user_agent_contact": "n0call@example.com",
}


def setup_file(data=ON_DISK):
    path = os.path.join(tempfile.mkdtemp(), "settings.json")
    with open(path, "w") as f:
        json.dump(data, f)
    return path, settings_mod.load(path)


def read(path):
    with open(path) as f:
        return json.load(f)


class Check(unittest.TestCase):

    def setUp(self):
        _, self.current = setup_file()

    def errors(self, changes):
        return settings_api.check(changes, self.current)[0]

    def test_good_change_is_cleaned(self):
        errors, warnings, cleaned = settings_api.check(
            {"station": {"field": {"callsign": " w4abc ", "grid": "em84QA"}}, "mode": "field"}, self.current)
        self.assertEqual((errors, warnings), ([], []))
        self.assertEqual(cleaned["station"]["field"], {"callsign": "W4ABC", "grid": "EM84qa"})

    def test_four_character_grid_warns(self):
        errors, warnings, _ = settings_api.check({"station": {"field": {"grid": "EM84"}}}, self.current)
        self.assertEqual(errors, [])
        self.assertIn("too coarse", warnings[0])

    def test_rejections(self):
        cases = [
            ({"server": {"port": 80}}, "can't be changed"),
            ({"mode": "camping"}, "mode must be"),
            ({"station": {"home": {"callsign": "N0"}}}, "3 to 12"),
            ({"station": {"home": {"grid": "ZZ99"}}}, "4- or 6-character"),
            ({"station": {"home": {"grid_source": "gps"}}}, "Phase 4"),
            ({"station": {"mobile": {}}}, "isn't home or field"),
            ({"display": {"day_brightness_pct": 120}}, "5 to 100"),
            ({"display": {"night_start": "25:00"}}, "24-hour"),
            ({"sources": {"propmon_url": "ftp://x"}}, "an address"),
            ({"sources": {"weird_url": "http://x"}}, "isn't a source"),
            ({"thresholds": {"propagation_stale_min": 2}}, "5 to 1440"),
            ({"thresholds": {"propagation_stale_min": 30.5}}, "whole number"),
            ({"thresholds": {"prop_bad_xray_classes": ["Q"]}}, "flare classes"),
            ({"thresholds": {"lightning_high_mi": 12}}, "can't be more than"),
            ({"thresholds": {"made_up": 1}}, "isn't a threshold"),
            ({"nws_user_agent_contact": "<your email>"}, "email address or callsign"),
        ]
        for changes, expected in cases:
            errors = self.errors(changes)
            self.assertTrue(any(expected in e for e in errors), (changes, errors))

    def test_allowed_edges(self):
        self.assertEqual(self.errors({"sources": {"propmon_url": ""}}), [])
        self.assertEqual(self.errors({"thresholds": {"lightning_high_mi": 4.5,
                                                     "prop_bad_xray_classes": ["x", "M"]}}), [])
        self.assertEqual(self.errors("not an object"), ["send a JSON object with the settings to change"])

    def test_every_problem_reported(self):
        self.assertEqual(len(self.errors({"mode": "x", "display": {"night_style": "blue"}})), 2)


class Store(unittest.TestCase):

    def setUp(self):
        self.path, self.settings = setup_file()
        self.store = settings_api.SettingsStore(self.settings, path=Path(self.path))

    def test_saves_file_and_live_settings(self):
        thresholds = self.settings["thresholds"]
        ok, body = self.store.update({"station": {"home": {"grid": "fm18LV"}},
                                      "thresholds": {"propagation_stale_min": 45}})
        self.assertTrue(ok)
        self.assertEqual(body["warnings"], [])
        self.assertNotIn("server", body)
        disk = read(self.path)
        self.assertEqual(disk["_comment"], "Real settings for this Pi.")      # comment kept
        self.assertEqual(disk["server"], {"host": "127.0.0.1", "port": 8090})  # untouched
        self.assertEqual(disk["station"]["home"]["callsign"], "N0CALL")       # sibling kept
        self.assertEqual(disk["thresholds"], {"propagation_stale_min": 45})   # only what changed
        self.assertEqual(self.settings["thresholds"]["propagation_stale_min"], 45)   # live
        self.assertEqual(self.settings["thresholds"]["nws_alerts_stale_min"], 10)    # defaults intact
        self.assertIsNot(self.settings["thresholds"], thresholds)  # replaced, read fresh by sources
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_rejected_change_saves_nothing(self):
        before = Path(self.path).read_text()
        ok, body = self.store.update({"mode": "field", "display": {"night_style": "blue"}})
        self.assertFalse(ok)
        self.assertEqual(len(body["errors"]), 1)
        self.assertEqual(Path(self.path).read_text(), before)
        self.assertEqual(self.settings["mode"], "home")

    def test_file_permissions_kept(self):
        os.chmod(self.path, 0o600)
        self.store.update({"mode": "field"})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_broken_file_kept_aside(self):
        with open(self.path, "w") as f:
            f.write("{ broken")
        ok, _ = self.store.update({"mode": "field"})
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(self.path + ".broken"))
        self.assertEqual(read(self.path)["mode"], "field")

    def test_missing_file_created(self):
        path = Path(tempfile.mkdtemp()) / "sub" / "settings.json"
        store = settings_api.SettingsStore(settings_mod.load(str(path)), path=path)
        self.assertTrue(store.update({"mode": "field"})[0])
        self.assertEqual(read(path), {"mode": "field"})

    def test_grid_change_reaches_forecast(self):
        from hamops_console.forecast import ForecastSource
        calls = []
        source = ForecastSource(self.settings, fetch=lambda url, contact: calls.append(url) or {},
                                run=lambda job: job())
        source.block()
        self.store.update({"station": {"home": {"grid": "EM84qa"}}})
        source.block()
        self.assertEqual(calls[-1], "https://api.weather.gov/points/34.0208,-82.625")


class Connections(unittest.TestCase):

    def test_plan_and_results(self):
        _, settings = setup_file()
        seen = []

        def fake(name, url, headers, timeout):
            seen.append((name, url, headers["User-Agent"]))
            return {"source": name, "url": url, "ok": True, "ms": 12, "message": None}

        results = settings_api.test_connections(settings, {"sentinel_url": "http://other.example:8085/"},
                                                fetch=fake)
        self.assertEqual([r["source"] for r in results], ["PropMon", "Weather Sentinel", "NWS"])
        self.assertEqual(seen[0][1], "http://propmon.example:8076/api/instrument/propagation")
        self.assertEqual(seen[1][1], "http://other.example:8085/api/conditions")   # unsaved address
        self.assertEqual(seen[2][1], "https://api.weather.gov/alerts/active/count")
        self.assertTrue(seen[2][2].endswith("(n0call@example.com)"))

    def test_not_set_and_invalid(self):
        _, settings = setup_file()
        results = settings_api.test_connections(settings, {"propmon_url": "", "sentinel_url": "nonsense"},
                                                fetch=lambda *a: {"ok": True})
        self.assertEqual((results[0]["ok"], results[0]["message"]), (False, "address not set"))
        self.assertEqual((results[1]["ok"], results[1]["message"]), (False, "not a valid address"))

    def test_real_failure_is_reported(self):
        result = settings_api._try("X", "http://127.0.0.1:9/", {}, 2)
        self.assertFalse(result["ok"])
        self.assertIn("not answering", result["message"])


class Endpoints(unittest.TestCase):

    def setUp(self):
        try:
            from hamops_console.app import create_app
        except ImportError:
            self.skipTest("Flask not installed")
        self.path, self.settings = setup_file()

        class Quiet:
            def block(self):
                return {}

            def blocks(self):
                return {}

        fake = lambda settings, sources=None: [{"source": "PropMon", "ok": True, "sources": sources}]
        self.client = create_app(self.settings, propagation=Quiet(), weather=Quiet(),
                                 dashboard_dir=tempfile.mkdtemp(),
                                 settings_file=Path(self.path),
                                 test_connections=fake).test_client()

    def test_get(self):
        r = self.client.get("/api/settings")
        body = r.get_json()
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertNotIn("server", body)
        self.assertEqual(body["station"]["home"]["grid"], "FM18lv")

    def test_put(self):
        r = self.client.put("/api/settings", json={"station": {"field": {"grid": "EM84"}}})
        self.assertEqual(r.status_code, 200)
        self.assertIn("too coarse", r.get_json()["warnings"][0])
        self.assertEqual(read(self.path)["station"]["field"]["grid"], "EM84")
        r = self.client.put("/api/settings", json={"mode": "camping"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["errors"], ["mode must be 'home' or 'field'"])
        self.assertEqual(self.client.put("/api/settings", data="junk").status_code, 400)

    def test_connections(self):
        r = self.client.post("/api/settings/test", json={"sources": {"propmon_url": "http://x:1"}})
        self.assertEqual(r.get_json()["results"][0]["sources"], {"propmon_url": "http://x:1"})
        self.assertEqual(self.client.post("/api/settings/test").status_code, 200)
        self.assertEqual(self.client.post("/api/settings/test", json={"sources": {"evil": 1}}).status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
