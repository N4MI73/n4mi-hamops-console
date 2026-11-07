"""NOAA Weather Radio block (contract section 5), both modes: which transmitter
to tune for the console's location, and the county's SAME code.

The list comes from NWS's county coverage listing,
https://www.weather.gov/source/nwr/JS/CCL.js (about 2.5 MB), a JavaScript file
of 15 parallel arrays filled row by row:

    ST[996] = "GA"; COUNTY[996] = "Colquitt"; SAME[996] = "013071";
    CALLSIGN[996] = "WXK53"; FREQ[996] = "162.550"; LAT[996] = "31.1358"; ...

One row per county and transmitter that serves it (6,751 rows, about 1,035
transmitters, 3,368 county codes; checked 2026-11-07). A county with no
coverage has a row with an empty callsign. STATUS is NORMAL, DEGRADED or
OUT OF SERVICE. REMARKS may say which part of the county a transmitter
covers ("NW 1/2", "E").

The console keeps a compact copy on the Pi
(~/.local/state/hamops-console/nwr_stations.json, about 200 KB), so the
weather radio panel works in the field with no internet. It is downloaded in
the background when there is none yet, and then at most once a day; a failed
download keeps the copy already stored and is retried after an hour.

Choosing the transmitter (2026-11-07; Dan to validate the radio side):
  1. Transmitters NWS lists for the location's county come first, working
     ones before degraded, nearest first. A transmitter that isn't listed for
     the county won't carry that county's SAME-coded warnings.
  2. If none of those is working, the nearest working transmitters follow,
     marked `covers_county: false`.
  3. Out-of-service transmitters may be listed (as such) but never come first.
  4. With no county known yet (NWS hasn't answered at this location), the
     nearest working transmitters are listed with `covers_county: null`.
The county comes from the NWS points lookup the forecast already makes
(e.g. "VAC059" -> SAME 051059). Distances are from the centre of the station
grid.
"""

import json
import logging
import math
import os
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

from . import freshness, grid as grid_mod
from .forecast import user_agent

log = logging.getLogger(__name__)

SOURCE = "NWS NWR listing (stored on the Pi)"
URL = "https://www.weather.gov/source/nwr/JS/CCL.js"
TIMEOUT_SEC = 60
REFRESH_SEC = 24 * 3600          # at most once a day (contract)
RETRY_AFTER_FAIL_SEC = 3600
STALE_AFTER_SEC = 30 * 86400     # the list changes rarely; a month-old copy is still shown
FORMAT = 1
NEAREST = 3                      # entries in `nearest`
MIN_TRANSMITTERS = 500           # a smaller download is treated as broken
MIN_COUNTIES = 2000

NORMAL, DEGRADED, OUT = "NORMAL", "DEGRADED", "OUT OF SERVICE"
STATUS_RANK = {NORMAL: 0, DEGRADED: 1, OUT: 2}   # an unknown word ranks with DEGRADED

_ROW = re.compile(r'^(\w+)\[(\d+)\]\s*=\s*"(.*)";\s*$', re.MULTILINE)


# ---------------------------------------------------------------- parsing

def _float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def parse(text):
    """CCL.js text -> the compact list (without `downloaded_at`).
    Raises ValueError when the text doesn't look like a complete list."""
    rows = {}
    for name, index, value in _ROW.findall(text or ""):
        rows.setdefault(int(index), {})[name] = value.strip()

    transmitters, counties, states = {}, {}, {}
    for row in rows.values():
        same, st = row.get("SAME", ""), row.get("ST", "")
        if not re.fullmatch(r"\d{6}", same) or not st:
            continue
        key = same[1:]                      # state FIPS + county FIPS; the first digit is a county part
        if same[0] == "0":
            states.setdefault(st, same[1:3])
        county = counties.setdefault(key, {"name": row.get("COUNTY") or None, "state": st, "tx": []})
        call = row.get("CALLSIGN", "")
        mhz, lat, lon = _float(row.get("FREQ")), _float(row.get("LAT")), _float(row.get("LON"))
        if not call or mhz is None or lat is None or lon is None:
            continue                        # "No NWR coverage" rows, or a row missing its position
        status = row.get("STATUS") or DEGRADED
        known = transmitters.get(call)
        if known is None:
            transmitters[call] = {"mhz": mhz, "lat": lat, "lon": lon,
                                  "site": row.get("SITENAME") or None,
                                  "site_state": row.get("SITESTATE") or None, "status": status}
        elif STATUS_RANK.get(status, 1) > STATUS_RANK.get(known["status"], 1):
            known["status"] = status        # if rows ever disagree, believe the worse one
        part = row.get("REMARKS") or None
        if not any(c == call for c, _ in county["tx"]):
            county["tx"].append([call, part])

    if len(transmitters) < MIN_TRANSMITTERS or len(counties) < MIN_COUNTIES:
        raise ValueError(f"NWS weather radio list looks incomplete "
                         f"({len(transmitters)} transmitters, {len(counties)} counties)")
    return {"format": FORMAT, "source_url": URL, "states": states,
            "transmitters": transmitters, "counties": counties}


def county_key(zone, states):
    """NWS county zone 'VAC059' -> '51059' using the list's own state codes,
    or None (a missing zone, a marine zone, a state not in the list)."""
    if not isinstance(zone, str) or not zone:
        return None
    m = re.fullmatch(r"([A-Z]{2})C(\d{3})", zone.strip().upper())
    if not m or m.group(1) not in states:
        return None
    return states[m.group(1)] + m.group(2)


def distance_mi(lat1, lon1, lat2, lon2):
    """Great-circle distance in statute miles."""
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def choose(data, lat, lon, zone):
    """-> (county, same_code, nearest, message) for a point and NWS county zone.
    The order follows the rules in the module docstring."""
    tx = data["transmitters"]
    key = county_key(zone, data.get("states") or {})
    county = data["counties"].get(key) if key else None

    def entry(call, covers, part=None):
        t = tx[call]
        return {"callsign": call, "frequency_mhz": t["mhz"], "site": t["site"],
                "distance_mi": round(distance_mi(lat, lon, t["lat"], t["lon"]), 1),
                "covers_county": covers, "county_part": part, "status": t["status"]}

    listed = []
    if county:
        listed = [entry(call, True, part) for call, part in county["tx"] if call in tx]
        listed.sort(key=lambda e: (STATUS_RANK.get(e["status"], 1), e["distance_mi"]))
    working_listed = [e for e in listed if e["status"] != OUT]
    names = {e["callsign"] for e in listed}
    covers = None if county is None else False
    others = sorted((entry(call, covers) for call, t in tx.items()
                     if call not in names and t["status"] != OUT),
                    key=lambda e: (STATUS_RANK.get(e["status"], 1), e["distance_mi"]))

    if working_listed:
        nearest = (working_listed + [e for e in listed if e["status"] == OUT])[:NEAREST]
        message = None
    else:
        # Nothing working for the county: the nearest working ones, then any
        # out-of-service county transmitter shown as such.
        nearest = others[:NEAREST] + listed[:2]
        if county is None:
            message = ("County not known yet: nearest transmitters, county coverage not confirmed"
                       if not zone else "County not in the NWS weather radio list: nearest transmitters")
        elif listed:
            message = "The county's own transmitter is out of service"
        else:
            message = "NWS lists no weather radio coverage for this county"

    name = f"{county['name']}, {county['state']}" if county and county.get("name") else None
    same = ("0" + key) if county else None
    return name, same, nearest, message


# ---------------------------------------------------------------- fetching and storage

def fetch_text(url, contact, timeout=TIMEOUT_SEC):
    """GET the NWS listing. Returns text; raises on failure."""
    request = urllib.request.Request(url, headers={"Accept": "*/*", "User-Agent": user_agent(contact)})
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        return reply.read().decode("utf-8", errors="replace")


def _run_in_thread(job):
    threading.Thread(target=job, name="nwr-refresh", daemon=True).start()


def _valid(data):
    return (isinstance(data, dict) and data.get("format") == FORMAT
            and isinstance(data.get("transmitters"), dict) and isinstance(data.get("counties"), dict)
            and freshness.parse_time(data.get("downloaded_at")) is not None)


class NwrSource:
    """Keeps the stored weather radio list and refreshes it in the background."""

    def __init__(self, settings, fetch=fetch_text, clock=freshness.utcnow, run=_run_in_thread, path=None):
        default = Path.home() / ".local" / "state" / "hamops-console" / "nwr_stations.json"
        self.path = Path(path or os.environ.get("HAMOPS_STATE_NWR") or default)
        self.settings = settings
        self._fetch = fetch
        self._clock = clock
        self._run = run
        self._lock = threading.Lock()
        self._busy = False
        self._attempted_at = None
        self._failed = False
        self._error = None
        self._data = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            log.error("could not read %s (%s); will download the weather radio list again", self.path, e)
            return None
        if not _valid(data):
            log.error("%s is not a weather radio list this version reads; will download again", self.path)
            return None
        return data

    def _save(self, data):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError as e:
            log.error("could not save the weather radio list to %s (%s); kept in memory", self.path, e)

    # -- refresh (runs in the background)
    def _due(self, now):
        if self._busy:
            return False
        if self._attempted_at is not None:
            wait = RETRY_AFTER_FAIL_SEC if self._failed else REFRESH_SEC
            if (now - self._attempted_at).total_seconds() < wait:
                return False
        if self._data is None:
            return True
        downloaded = freshness.parse_time(self._data["downloaded_at"])
        return (now - downloaded).total_seconds() >= REFRESH_SEC

    def _refresh(self):
        try:
            data = parse(self._fetch(URL, self.settings.get("nws_user_agent_contact")))
        except Exception as e:  # noqa: BLE001 -- any failure must end the attempt and keep the stored copy
            with self._lock:
                self._failed, self._busy = True, False
                self._error = self._describe(e)
            log.warning("weather radio list not refreshed: %s", self._error)
            return
        data["downloaded_at"] = freshness.iso(self._clock())
        self._save(data)
        with self._lock:
            self._data = data
            self._failed, self._error, self._busy = False, None, False
        log.info("weather radio list refreshed: %d transmitters", len(data["transmitters"]))

    @staticmethod
    def _describe(e):
        if isinstance(e, urllib.error.HTTPError):
            return f"NWS answered with an error ({e.code})"
        if isinstance(e, ValueError) and str(e).startswith("NWS"):
            return str(e)
        if isinstance(e, (urllib.error.URLError, OSError)):
            return f"NWS not answering ({e.__class__.__name__})"
        return f"NWS sent an unexpected reply ({e.__class__.__name__})"

    # -- block
    def block(self, grid, county_zone):
        """The weather_radio block for a station grid and NWS county zone (or None)."""
        now = self._clock()
        with self._lock:
            start = self._due(now)
            if start:
                self._busy, self._attempted_at = True, now
        if start:
            self._run(self._refresh)
        with self._lock:
            data = self._data
            if data is None:
                error = self._error or "Downloading the NWS weather radio list"
                out = freshness.block(SOURCE, None, None, STALE_AFTER_SEC, error=error, now=now)
                out.update({"county": None, "same_code": None, "nearest": []})
                return out
            downloaded = freshness.parse_time(data["downloaded_at"])
            out = freshness.block(SOURCE, downloaded, None, STALE_AFTER_SEC, error=self._error, now=now)
        if not grid_mod.is_valid(grid):
            out.update({"status": freshness.UNAVAILABLE, "message": "Station grid not set or not valid in Settings",
                        "county": None, "same_code": None, "nearest": []})
            return out
        lat, lon = grid_mod.centre(grid)
        county, same, nearest, message = choose(data, lat, lon, county_zone)
        if message:
            out["message"] = message if out["status"] == freshness.OK else f"{out['message']}. {message}"
        out.update({"county": county, "same_code": same, "nearest": nearest})
        return out


def tune_words(radio):
    """'Tune NOAA Weather Radio KHB36, 162.550 MHz.' for the recommended
    transmitter, or the plain 'Tune NOAA Weather Radio.' when there is none
    (no list yet, or nothing confirmed for the county)."""
    nearest = (radio or {}).get("nearest") or []
    if radio and radio.get("status") != freshness.UNAVAILABLE and nearest:
        first = nearest[0]
        if first.get("status") != OUT and first.get("covers_county") is not False:
            return f"Tune NOAA Weather Radio {first['callsign']}, {first['frequency_mhz']:.3f} MHz."
    return "Tune NOAA Weather Radio."
