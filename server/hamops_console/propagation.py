"""Propagation block (contract section 4), Home Mode: relayed from PropMon.

PropMon's server is owned by Ham Shack Automation (code in n4mi-shack-server;
the PropMon ESP32 display's firmware is N4MI Desktop Instrument Series). Since
2026-11-08 the console reads PropMon's v2 endpoint, `/api/v2/conditions`,
which only gains fields (renames or removals are checked with this project
first). This module only reads it and translates it to the console's contract:
  - `status` per band becomes `rating` ("status" now means freshness);
  - the solar figures come from v2's `solar` block; `solar_wind` becomes
    `solar_wind_kms`, to show its unit;
  - bands are ordered good, then fair, then poor, then unknown, lowest
    frequency first within each group (Dan, 2026-11-01);
  - `period` (dawn / day / dusk / night: the 40m-6m time of day where PropMon
    rates) is passed through for the "NOW · DAYTIME" heading;
  - **NOAA fallback:** when HamQSL is down PropMon rates from NOAA data,
    every band reads poor and `summary` is "Data unavailable". That is not a
    real all-poor state, so `ratings_available` becomes False and every
    band's rating null; the solar figures are still shown;
  - `recovery_active` / `recovery_ends`: PropMon holds ratings down for 12
    hours after a 3-hour Kp of 5 or more, so an all-poor screen after a storm
    can be explained rather than look like a fault;
  - HTTP 503 `no_data_yet` just after a PropMon restart means "no data yet",
    not an outage, and says so;
  - PropMon's `tower` block is not used (Dan, 2026-11-01).

Fetching is lazy: the service asks PropMon only when a request comes in and
the last fetch is older than the fetch interval, and it keeps the last good
reply, so the dashboard can poll often without loading PropMon.
"""

import json
import threading
import urllib.error
import urllib.request

from . import freshness

SOURCE = "PropMon"
PATH = "/api/v2/conditions"
PERIODS = ("dawn", "day", "dusk", "night")
RATING_ORDER = {"good": 0, "fair": 1, "poor": 2}
TIMEOUT_SEC = 5


def _band_mhz(name):
    """'160m' -> metres as a number, used to sort low frequency first
    (longer wavelength = lower frequency)."""
    try:
        return -float(str(name).lower().rstrip("m"))
    except ValueError:
        return 0.0


def ratings_available(raw):
    """False on PropMon's NOAA fallback (HamQSL down), where every band reads
    poor whatever the real conditions are."""
    raw = raw if isinstance(raw, dict) else {}
    return not (raw.get("source") == "noaa" or raw.get("summary") == "Data unavailable")


def translate(raw):
    """PropMon v2 reply (dict) -> the contract's data fields. Missing or odd
    fields become None, never an exception."""
    raw = raw if isinstance(raw, dict) else {}
    solar = raw.get("solar") if isinstance(raw.get("solar"), dict) else {}
    geo = raw.get("geomagnetic") if isinstance(raw.get("geomagnetic"), dict) else {}

    def num(key):
        value = solar.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    available = ratings_available(raw)
    bands = []
    for entry in raw.get("bands") or []:
        if not isinstance(entry, dict) or not entry.get("band"):
            continue
        rating = entry.get("status") if available else None
        rating = rating if rating in RATING_ORDER else None
        bands.append({"band": str(entry["band"]), "rating": rating})
    bands.sort(key=lambda b: (RATING_ORDER.get(b["rating"], 3), _band_mhz(b["band"])))

    xray = solar.get("xray")
    summary = raw.get("summary")
    period = raw.get("period")
    recovery = geo.get("recovery_active")
    ends = freshness.parse_time(geo.get("recovery_ends")) if recovery is True else None
    return {
        "sfi": num("sfi"),
        "sunspots": num("sunspots"),
        "a_index": num("a_index"),
        "k_index": num("k_index"),
        "xray": xray if isinstance(xray, str) else None,
        "solar_wind_kms": num("solar_wind"),
        "bands": bands,
        "summary": summary if isinstance(summary, str) and available else None,
        "ratings_available": available,
        "period": period if period in PERIODS else None,
        "recovery_active": recovery if isinstance(recovery, bool) else None,
        "recovery_ends": freshness.iso(ends),
    }


class NoDataYet(Exception):
    """PropMon answered 503 `no_data_yet`: it has just restarted."""


def fetch_propmon(base_url, timeout=TIMEOUT_SEC):
    """GET PropMon's propagation JSON. Returns a dict; raises on failure."""
    url = base_url.rstrip("/") + PATH
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "HamOpsConsole"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return json.load(reply)
    except urllib.error.HTTPError as e:
        if e.code == 503:
            raise NoDataYet() from e
        raise


class PropagationSource:
    """Keeps the last good PropMon reply and builds the contract block."""

    def __init__(self, settings, fetch=fetch_propmon, clock=freshness.utcnow):
        self.settings = settings
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._data = None           # last good translated data
        self._issued_at = None      # PropMon's own `updated`
        self._fetched_at = None     # when we last got a good reply
        self._attempted_at = None   # when we last tried at all
        self._error = None          # reason the last attempt failed

    def _thresholds(self):
        t = self.settings["thresholds"]
        return t["propagation_fetch_min"] * 60, t["propagation_stale_min"] * 60

    def _refresh_if_due(self, now):
        fetch_every, _ = self._thresholds()
        if self._attempted_at and (now - self._attempted_at).total_seconds() < fetch_every:
            return
        self._attempted_at = now
        base = self.settings["sources"].get("propmon_url")
        if not base:
            self._error = "PropMon address not set in Settings"
            return
        try:
            raw = self._fetch(base)
        except NoDataYet:
            self._error = f"{SOURCE} has just restarted and has no data yet"
            return
        except (urllib.error.URLError, OSError, ValueError) as e:
            self._error = f"{SOURCE} not answering ({e.__class__.__name__})"
            return
        self._data = translate(raw)
        self._issued_at = freshness.parse_time(raw.get("updated") if isinstance(raw, dict) else None)
        self._fetched_at = now
        self._error = None

    def block(self):
        now = self._clock()
        with self._lock:
            self._refresh_if_due(now)
            _, stale_after = self._thresholds()
            out = freshness.block(SOURCE, self._fetched_at, self._issued_at, stale_after,
                                  error=self._error, now=now)
            empty = translate({})
            empty["ratings_available"] = None
            out.update(self._data or empty)
            return out
