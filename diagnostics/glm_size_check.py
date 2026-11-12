#!/usr/bin/env python3
"""GLM data-size check (Ham Ops Console diagnostics, 2026-11-12).

How much data would Field Mode lightning from NOAA's GOES Geostationary Lightning
Mapper (GLM) cost over a phone hotspot? This lists NOAA's public GLM files on AWS
(the GLM-L2-LCFA product: one file per 20 seconds, covering the satellite's whole
view) and reports, for each recent hour, how many files there were and how big they
were in total. It also reports how long after each file's end time it appeared
(the delay before the console could see a strike).

It downloads only the file listings (a few tens of KB per hour checked), never the
files themselves, and writes nothing. Standard library only.

Run on the Pi or any computer with internet (SSH window on the Pi):
    python3 glm_size_check.py                 # last 24 hours, GOES-East (GOES-19)
    python3 glm_size_check.py --hours 6
    python3 glm_size_check.py --satellite goes18   # GOES-West
"""

import argparse
import statistics
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


def list_hour(satellite, hour):
    """All GLM-L2-LCFA files for one UTC hour -> [(key, size_bytes, last_modified)]."""
    prefix = f"GLM-L2-LCFA/{hour:%Y}/{hour.timetuple().tm_yday:03d}/{hour:%H}/"
    files, token = [], None
    while True:
        url = f"https://noaa-{satellite}.s3.amazonaws.com/?list-type=2&prefix={prefix}"
        if token:
            url += "&continuation-token=" + urllib.parse.quote(token)
        req = urllib.request.Request(url, headers={"User-Agent": "HamOpsConsole-glm-size-check"})
        with urllib.request.urlopen(req, timeout=30) as r:
            root = ET.fromstring(r.read())
        files += parse_listing(root)
        if root.findtext("s3:IsTruncated", default="false", namespaces=NS) != "true":
            return files
        token = root.findtext("s3:NextContinuationToken", namespaces=NS)


def parse_listing(root):
    out = []
    for c in root.findall("s3:Contents", NS):
        key = c.findtext("s3:Key", namespaces=NS)
        size = int(c.findtext("s3:Size", default="0", namespaces=NS))
        modified = datetime.fromisoformat(c.findtext("s3:LastModified", namespaces=NS).replace("Z", "+00:00"))
        out.append((key, size, modified))
    return out


def end_time(key):
    """OR_GLM-L2-LCFA_G19_s20263161800000_e20263161800200_c....nc -> the 'e' time (UTC)."""
    try:
        part = next(p for p in key.split("/")[-1].split("_") if p.startswith("e"))
        return datetime.strptime(part[1:14], "%Y%j%H%M%S").replace(tzinfo=timezone.utc)
    except (StopIteration, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--satellite", default="goes19", help="goes19 (GOES-East, default) or goes18 (GOES-West)")
    ap.add_argument("--hours", type=int, default=24, help="how many recent complete hours to check (default 24)")
    args = ap.parse_args()

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    hours = [now - timedelta(hours=h) for h in range(args.hours, 0, -1)]
    print(f"GLM-L2-LCFA on noaa-{args.satellite}, {len(hours)} complete hours ending {now:%Y-%m-%d %H:%M} UTC\n")
    print(f"{'Hour (UTC)':<17} {'Files':>5} {'Total MB':>9} {'Avg KB':>7} {'Max KB':>7}")
    totals, lags, all_sizes = [], [], []
    for h in hours:
        try:
            files = list_hour(args.satellite, h)
        except Exception as e:  # report and carry on
            print(f"{h:%Y-%m-%d %H:00}  could not list: {e.__class__.__name__}: {e}")
            continue
        sizes = [s for _, s, _ in files]
        all_sizes += sizes
        total = sum(sizes)
        totals.append(total)
        for key, _, modified in files:
            e = end_time(key)
            if e:
                lags.append((modified - e).total_seconds())
        avg = (total / len(sizes) / 1024) if sizes else 0
        print(f"{h:%Y-%m-%d %H:00}  {len(sizes):>5} {total / 1e6:>9.1f} {avg:>7.0f} {max(sizes, default=0) / 1024:>7.0f}")

    if not totals:
        print("\nNo listings could be read (no internet, or the bucket name is wrong).")
        return 1
    print(f"\nPer hour: average {statistics.mean(totals) / 1e6:.1f} MB, busiest {max(totals) / 1e6:.1f} MB, "
          f"quietest {min(totals) / 1e6:.1f} MB")
    print(f"Per file: average {statistics.mean(all_sizes) / 1024:.0f} KB over {len(all_sizes)} files")
    if lags:
        lags.sort()
        print(f"Delay from a file's end time to its appearance: median {statistics.median(lags):.0f} s, "
              f"90 % within {lags[int(len(lags) * 0.9) - 1]:.0f} s")
    print("\nFor comparison: the console would download files only while the NWS lightning outlook says "
          "'possible' or 'likely'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
