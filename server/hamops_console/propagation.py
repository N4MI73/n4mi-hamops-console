"""Propagation block (contract section 4), Home Mode: relayed from PropMon.

PropMon owns its JSON (N4MI Desktop Instrument Series). This module only
reads it and translates it to the console's contract:
  - `status` per band becomes `rating` ("status" now means freshness);
  - `solar_wind` becomes `solar_wind_kms`, to show its unit;
  - bands are ordered good, then fair, then poor, then unknown, lowest
    frequency first within each group (Dan, 2026-11-01);
  - PropMon's `alerts[]` and `tower_status` are not used (Dan, 2026-11-01).

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
PATH = "/api/instrument/propagation"
RATING_ORDER = {"good": 0, "fair": 1, "poor": 2}
TIMEOUT_SEC = 5


def _band_mhz(name):
    """'160m' -> metres as a number, used to sort low frequency first
    (longer wavelength = lower frequency)."""
    try:
        return -float(str(name).lower().rstrip("m"))
    except ValueError:
        return 0.0


def translate(raw):
    """PropMon reply (dict) -> the contract's data fields. Missing or odd
    fields become None, never an exception."""
    raw = raw if isinstance(raw, dict) else {}

    def num(key):
        value = raw.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    bands = []
    for entry in raw.get("bands") or []:
        if not isinstance(entry, dict) or not entry.get("band"):
            continue
        rating = entry.get("status")
        rating = rating if rating in RATING_ORDER else None
        bands.append({"band": str(entry["band"]), "rating": rating})
    bands.sort(key=lambda b: (RATING_ORDER.get(b["rating"], 3), _band_mhz(b["band"])))

    xray = raw.get("xray")
    summary = raw.get("summary")
    return {
        "sfi": num("sfi"),
        "sunspots": num("sunspots"),
        "a_index": num("a_index"),
        "k_index": num("k_index"),
        "xray": xray if isinstance(xray, str) else None,
        "solar_wind_kms": num("solar_wind"),
        "bands": bands,
        "summary": summary if isinstance(summary, str) else None,
    }


def fetch_propmon(base_url, timeout=TIMEOUT_SEC):
    """GET PropMon's propagation JSON. Returns a dict; raises on failure."""
    url = base_url.rstrip("/") + PATH
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "HamOpsConsole"})
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        return json.load(reply)


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
            empty = {"sfi": None, "sunspots": None, "a_index": None, "k_index": None,
                     "xray": None, "solar_wind_kms": None, "bands": [], "summary": None}
            out.update(self._data or empty)
            return out
