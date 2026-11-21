#!/usr/bin/env python3
"""Replay a past storm through the console's satellite lightning (Ham Ops Console diagnostics, 2026-11-21).

NOAA keeps every GOES lightning (GLM) file since 2017, so the Field Mode lightning code
can be tested against a real storm without waiting for one. This runs the console's
own code -- the same fetching loop (`field_lightning.py`), file reader and tiers
(`glm.py`) and alert wording (`alerts.py`) the service uses -- against the archived
files for a time window, with a clock that steps through the storm 20 seconds at a
time. It prints a timeline of what the Priority Card would have said, then a summary.

Nothing on the Pi changes: the running console, its settings and its saved state are
not touched (this keeps its own state in a temporary folder and deletes it). Files
are read in memory and discarded. It downloads what the console would have: about
0.35 MB per 20-second file, so about 60 MB per hour of storm on Full (20 MB on
Economy). Needs python3-netcdf4 (installed on the Pi 2026-11-20).

Run on the Pi, in the SSH window, from the repository folder:
    python3 diagnostics/glm_replay.py --grid AB12cd --start 2025-07-14T20:00Z --hours 2
    python3 diagnostics/glm_replay.py --grid AB12cd --start 2024-06-05T21:30Z --hours 1.5 --satellite goes16
    python3 diagnostics/glm_replay.py ... --economy          # one file a minute, as Economy would

--grid      the 6-character grid to measure from (your own; it is not saved anywhere)
--start     start of the window, UTC (e.g. 2025-07-14T20:00Z)
--hours     length of the window (default 2; at most 6)
--satellite goes19 (GOES-East since April 2025, the default), goes16 (GOES-East before
            that, archive back to 2017), or goes18 (GOES-West)

Times in the timeline follow the console's main clock setting style: UTC first, local
in brackets (local = this computer's time zone). To compare with the Tempest, look at
the Tempest app's lightning history for the same day: GLM counts flashes (cloud and
ground, ~8 km resolution), the Tempest counts the strikes it hears, so expect the same
storm and similar timing, not identical counts or distances.
"""

import argparse
import os
import shutil
import sys
import tempfile
import urllib.parse
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "server"))

from hamops_console import alerts, field_lightning as fl, freshness, glm, grid as grid_mod  # noqa: E402
from hamops_console import settings as settings_mod  # noqa: E402

BUCKETS = {"goes19": "https://noaa-goes19.s3.amazonaws.com",
           "goes16": "https://noaa-goes16.s3.amazonaws.com",
           "goes18": "https://noaa-goes18.s3.amazonaws.com"}
STEP = timedelta(seconds=20)
ARRIVAL = timedelta(seconds=30)        # a file is listed about 30 s after it starts (median 9 s after it ends)
OUTLOOK = {"level": "likely", "start": None, "basis": "nws_forecast"}   # keeps detection on for the whole replay
LISTING = '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated>{}</ListBucketResult>'


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Archive:
    """Fetches from NOAA's real bucket, but hides files that wouldn't have been listed
    yet at the replay clock's time, so the console's loop sees the storm unfold as it
    happened. Counts what is downloaded."""

    def __init__(self, clock, fetch=fl.fetch_bytes):
        self.clock, self._fetch = clock, fetch
        self.bytes = self.files = 0

    def __call__(self, url, timeout):
        data = self._fetch(url, timeout)
        self.bytes += len(data)
        if "list-type=" not in url:
            self.files += 1
            return data
        keys, _ = glm.listing_keys(data)
        seen = [k for k in keys if (glm.file_start(k) or self.clock.now) + ARRIVAL <= self.clock.now]
        return LISTING.format("".join(f"<Contents><Key>{k}</Key></Contents>" for k in seen)).encode()


def parse_start(text):
    t = freshness.parse_time(text)          # "2025-07-14T20:00Z" or with seconds; no zone = UTC
    if t is None:
        raise argparse.ArgumentTypeError("use UTC like 2025-07-14T20:00Z")
    return t


def lightning_card(items):
    for i in items:
        if i["category"] in alerts.LIGHTNING_HOLDS:
            return i
    return None


def replay(grid, start, hours, satellite="goes19", economy=False, fetch=fl.fetch_bytes, out=print):
    if not grid_mod.is_valid(grid) or len(grid) != 6:
        raise SystemExit("--grid must be a 6-character grid, e.g. AB12cd")
    if not 0 < hours <= 6:
        raise SystemExit("--hours must be more than 0 and at most 6")
    if not fl.reader_available():
        raise SystemExit("python3-netcdf4 isn't installed (sudo apt install python3-netcdf4)")
    settings = settings_mod.load(os.path.join(tempfile.gettempdir(), "glm-replay-no-settings.json"))
    settings["mode"] = "field"
    settings["station"]["field"]["grid"] = grid
    settings["station"]["field"]["grid_source"] = "manual"
    settings["sources"]["glm_east_url"] = BUCKETS[satellite]
    settings["field_lightning"] = {"level": "economy" if economy else "full", "satellite": "east"}
    clock = Clock(start)
    archive = Archive(clock, fetch)
    work = tempfile.mkdtemp(prefix="glm-replay-")
    try:
        source = fl.FieldLightning(settings, fetch=archive, clock=clock,
                                   state_path=os.path.join(work, "field_lightning.json"))
        source.turn_on()
        source.observe({"status": freshness.OK, "lightning_outlook": dict(OUTLOOK, start=freshness.iso(start))},
                       freshness.OK)
        end = start + timedelta(hours=hours)
        out(f"Replaying {satellite} {start:%Y-%m-%d %H:%MZ} for {hours:g} h from {grid} "
            f"({'Economy' if economy else 'Full'}); one line per change.")
        last_card, last_count, cleared_at, firsts, peak = None, 0, None, {}, (0, None)
        last_status = freshness.OK
        outer = settings["thresholds"]["lightning_field_outer_mi"]
        while clock.now <= end:
            source.poll_once()
            block = source.block()
            forecast = {"status": freshness.OK, "lightning_outlook": None}
            items = alerts.build(None, {"alerts": {"status": freshness.OK, "items": []}, "lightning": block,
                                        "forecast": forecast}, settings, clock.now)
            card = lightning_card(items)
            count = sum(1 for f in source.flashes if f["distance_mi"] <= outer)
            for tier, key in ((outer, "within_20mi"), (10, "within_10mi"), (5, "within_5mi")):
                if block.get(key) and tier not in firsts:
                    firsts[tier] = (clock.now, block.get("closest_mi"), block.get("closest_bearing"))
            minute = sum(1 for f in source.flashes if f["distance_mi"] <= 10
                         and (clock.now - f["time"]).total_seconds() <= 60)
            if minute > peak[0]:
                peak = (minute, clock.now)
            key = (card["category"], card["title"]) if card else None
            stamp = alerts.clock_text(clock.now)
            if key != last_card or count > last_count:
                if card:
                    new = f" · {count - last_count} new" if count > last_count else ""
                    out(f"{stamp}  [{card['category'].replace('lightning_', '')}{' CRITICAL' if card['critical'] else ''}]"
                        f"  {card['title']}  ·  {card['detail']}{new}")
                elif last_card:
                    out(f"{stamp}  holds cleared: no lightning card")
                    cleared_at = clock.now
                last_card = key
            status = block["status"] if clock.now - start > timedelta(minutes=2) else freshness.OK
            if status != last_status:
                out(f"{stamp}  satellite data {status}" + (f": {block.get('message')}" if status != freshness.OK else ""))
                last_status = status
            last_count = count
            clock.now += STEP
        out("")
        out("Summary")
        out(f"  Files read: {archive.files}   Downloaded: {archive.bytes / 1e6:.1f} MB "
            f"({archive.bytes / 1e6 / hours:.0f} MB an hour)")
        if not firsts:
            out(f"  No flashes within {outer:g} mi of {grid} in this window.")
        for tier in sorted(firsts, reverse=True):
            t, mi, brg = firsts[tier]
            out(f"  First within {tier:g} mi: {alerts.clock_text(t)}, {mi} mi {brg or ''}")
        if peak[0]:
            out(f"  Most flashes inside 10 mi in one minute: {peak[0]} at {alerts.clock_text(peak[1])}"
                f" (\"frequent\" is more than {settings['thresholds']['lightning_frequent_per_min']})")
        if last_card:
            hold = block.get("hold_until_5mi") or block.get("hold_until_10mi") or block.get("hold_until_20mi")
            out(f"  Still held at the end of the window" + (f" (until {alerts.clock_text(freshness.parse_time(hold))})" if hold else ""))
        elif cleared_at:
            out(f"  All holds cleared at {alerts.clock_text(cleared_at)} (30 minutes after the last flash).")
        return firsts
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    import logging
    logging.getLogger("hamops_console").setLevel(logging.ERROR)   # no "no settings file" notice
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--grid", required=True)
    p.add_argument("--start", required=True, type=parse_start)
    p.add_argument("--hours", type=float, default=2.0)
    p.add_argument("--satellite", choices=sorted(BUCKETS), default="goes19")
    p.add_argument("--economy", action="store_true")
    a = p.parse_args()
    try:
        replay(a.grid, a.start, a.hours, a.satellite, a.economy)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
