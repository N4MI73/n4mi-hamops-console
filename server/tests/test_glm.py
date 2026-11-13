#!/usr/bin/env python3
"""Tests for reading GOES GLM lightning files and the field tiers (2026-11-13).

The file-reading tests need python3-netcdf4 and are skipped without it (it isn't installed
on the Pi until Field Mode lightning is set up). A real GLM file in server/data/glm/
(ignored by git) is read too when one is there.

    python server/tests/test_glm.py      (or: python -m pytest server/tests)
"""

import builtins
import glob
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from hamops_console import glm  # noqa: E402

try:
    import netCDF4  # noqa: F401
    HAVE_NETCDF = True
except ImportError:
    HAVE_NETCDF = False

T0 = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)
SITE = (38.9, -77.0)                   # a neutral test location (not the home QTH)


def flash_at(miles, bearing_deg, minutes_ago=0):
    """A nearby-flash record as nearby() makes it."""
    return {"time": T0 - timedelta(minutes=minutes_ago), "distance_mi": miles,
            "bearing": glm.compass(bearing_deg)}


class Geometry(unittest.TestCase):

    def test_distance(self):
        self.assertAlmostEqual(glm.distance_mi(38.0, -77.0, 39.0, -77.0), 69.1, delta=0.2)
        self.assertAlmostEqual(glm.distance_mi(*SITE, *SITE), 0.0)

    def test_bearing_and_compass(self):
        self.assertAlmostEqual(glm.bearing_deg(38.0, -77.0, 39.0, -77.0), 0.0, delta=0.1)
        self.assertAlmostEqual(glm.bearing_deg(38.0, -77.0, 38.0, -76.0), 90.0, delta=0.5)
        self.assertEqual([glm.compass(d) for d in (0, 11, 12, 247.5, 350, 359)],
                         ["N", "N", "NNE", "WSW", "N", "N"])

    def test_nearby_filters_and_sorts(self):
        lat, lon = SITE
        flashes = [(T0, lat + 10 / 69.0, lon), (T0, lat - 3 / 69.0, lon), (T0, lat + 1.0, lon),
                   (T0, -20.0, -40.0)]
        near = glm.nearby(flashes, lat, lon)
        self.assertEqual([round(n["distance_mi"]) for n in near], [3, 10])
        self.assertEqual([n["bearing"] for n in near], ["S", "N"])


class Tiers(unittest.TestCase):

    def test_quiet(self):
        s = glm.Recent().summary(T0)
        self.assertIsNone(s["tier_mi"])
        self.assertEqual(s["flashes_10_min"], 0)

    def test_tiers(self):
        for miles, tier in [(18, 20), (20, 20), (9.9, 10), (4.5, 5)]:
            r = glm.Recent()
            r.add([flash_at(miles, 250)])
            self.assertEqual(r.summary(T0)["tier_mi"], tier, miles)
        r = glm.Recent()
        r.add([flash_at(22, 0)])                 # watched, but outside every tier
        self.assertIsNone(r.summary(T0)["tier_mi"])

    def test_one_flash_is_enough_and_closest_wins(self):
        r = glm.Recent()
        r.add([flash_at(18, 250, 2), flash_at(14, 247, 1), flash_at(4, 90, 3)])
        s = r.summary(T0)
        self.assertEqual((s["tier_mi"], s["closest_mi"], s["closest_bearing"]), (5, 4, "E"))
        self.assertEqual(s["flashes_10_min"], 3)

    def test_thirty_minute_clear(self):
        r = glm.Recent()
        r.add([flash_at(4, 0, 29), flash_at(15, 0, 5)])
        s = r.summary(T0)
        self.assertEqual(s["tier_mi"], 5)
        self.assertEqual(s["hold_until"], T0 + timedelta(minutes=1))   # 30 min after the 5-mile flash
        s = r.summary(T0 + timedelta(minutes=2))                       # the 5-mile flash has aged out
        self.assertEqual(s["tier_mi"], 20)
        self.assertEqual(s["hold_until"], T0 + timedelta(minutes=25))
        self.assertIsNone(r.summary(T0 + timedelta(minutes=26))["tier_mi"])


LISTING = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
<Contents><Key>GLM-L2-LCFA/2026/317/14/OR_GLM-L2-LCFA_G19_s20263171459200_e20263171459400_c20263171459419.nc</Key></Contents>
<Contents><Key>GLM-L2-LCFA/2026/317/14/OR_GLM-L2-LCFA_G19_s20263171459000_e20263171459200_c20263171459219.nc</Key></Contents>
<Contents><Key>GLM-L2-LCFA/2026/317/14/OR_GLM-L2-LCFA_G19_s20263171459400_e20263171500000_c20263171500021.nc</Key></Contents>
<IsTruncated>false</IsTruncated>
</ListBucketResult>"""


class Files(unittest.TestCase):

    def test_hour_prefix(self):
        self.assertEqual(glm.hour_prefix(T0), "GLM-L2-LCFA/2026/317/15/")

    def test_listing_and_start(self):
        keys, truncated = glm.listing_keys(LISTING)
        self.assertFalse(truncated)
        self.assertEqual([glm.file_start(k).second for k in keys], [0, 20, 40])
        self.assertEqual(glm.file_start(keys[0]), datetime(2026, 11, 13, 14, 59, tzinfo=timezone.utc))
        self.assertIsNone(glm.file_start("not-a-glm-file.nc"))

    def test_choose_full_and_economy(self):
        keys, _ = glm.listing_keys(LISTING)
        self.assertEqual(glm.choose(keys, None), keys)
        self.assertEqual(glm.choose(keys, keys[0]), keys[1:])
        self.assertEqual(glm.choose(keys, None, economy=True), keys[:1])     # one a minute
        self.assertEqual(glm.choose(keys, keys[0], economy=True), [])        # minute already read
        nxt = keys[2].replace("s20263171459400", "s20263171500000")
        self.assertEqual(glm.choose(keys + [nxt], keys[0], economy=True), [nxt])


@unittest.skipUnless(HAVE_NETCDF, "python3-netcdf4 not installed")
class Reading(unittest.TestCase):

    def make_file(self, flashes):
        path = os.path.join(tempfile.mkdtemp(), "glm.nc")
        with netCDF4.Dataset(path, "w") as d:
            d.time_coverage_start = "2026-11-13T15:00:00.0Z"
            d.createDimension("number_of_flashes", len(flashes))
            for name, i in (("flash_lat", 0), ("flash_lon", 1), ("flash_time_offset_of_first_event", 2)):
                v = d.createVariable(name, "f4", ("number_of_flashes",))
                v[:] = [f[i] for f in flashes]
        with open(path, "rb") as f:
            return f.read()

    def test_read(self):
        data = self.make_file([(38.95, -77.0, 3.5), (-20.0, -40.0, -1.0)])
        got = glm.read_flashes(data)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0][0], T0 + timedelta(seconds=3.5))
        self.assertAlmostEqual(got[0][1], 38.95, places=4)
        self.assertEqual(got[1][0], T0 - timedelta(seconds=1))

    def test_no_flashes(self):
        path = os.path.join(tempfile.mkdtemp(), "empty.nc")
        with netCDF4.Dataset(path, "w") as d:
            d.time_coverage_start = "2026-11-13T15:00:00.0Z"
        with open(path, "rb") as f:
            self.assertEqual(glm.read_flashes(f.read()), [])

    def test_real_files_if_present(self):
        found = sorted(glob.glob(os.path.join(os.path.dirname(HERE), "data", "glm", "*.nc")))
        if not found:
            self.skipTest("no real GLM file in server/data/glm/")
        for path in found:
            with open(path, "rb") as f:
                flashes = glm.read_flashes(f.read())
            self.assertTrue(flashes)
            start = glm.file_start(path)
            for t, la, lo in flashes:
                self.assertTrue(-90 <= la <= 90 and -180 <= lo <= 180)
                self.assertLess(abs((t - start).total_seconds()), 60)


class WithoutReader(unittest.TestCase):

    def test_missing_package_is_reported(self):
        real = builtins.__import__

        def no_netcdf(name, *a, **k):
            if name == "netCDF4":
                raise ImportError("no")
            return real(name, *a, **k)
        builtins.__import__ = no_netcdf
        try:
            with self.assertRaises(glm.ReaderMissing):
                glm.read_flashes(b"")
        finally:
            builtins.__import__ = real


if __name__ == "__main__":
    unittest.main()
