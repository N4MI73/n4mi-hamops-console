"""Propagation block (contract section 4), Field Mode: the console rates the bands itself.

Built 2026-11-17 on Ham Shack Automation's field engine (copy in fieldengine/, from
n4mi-shack-server 4872101; model 2026-11-17-v3, Field Day scale 2026-11-17-fd1). At home
the console relays PropMon (propagation.py); in the field it fetches PropMon's own inputs
and runs the same model for the operating site, so the time of day follows the sun where
the station is:

  HamQSL  solarxml.php   SFI, K, A, sunspots, X-ray, solar wind (the element names PropMon
                         reads, verified live by Ham Shack Automation 2026-10-20)
  NOAA    planetary K    the last 3-hour Kp readings, for the 12-hour storm-recovery hold
                         (missing: no hold is applied, and the block says so)

Both are fetched in the background every `field_propagation_fetch_min` (10), so the screen
never waits on a hotspot; the bands are re-rated on every request from the last good figures,
because the time of day moves. Freshness counts from the last good HamQSL reply.

Scale: the DX scale (all ten bands) normally; the Field Day scale (North America reach,
160/80/40/20/15/10/6 m) while the Field Day operating profile is on. Missing SFI or K gives
"Band ratings unavailable", never ten POOR bands. 6m on the Field Day scale is always poor
with a note (sporadic E can't be forecast). The block says which scale and model rated it,
and gives the scale's own meanings for the Help panel.

Location: the centre of the Field grid. Time zone (for the local date of sunrise/sunset):
the site's own, from the NWS forecast lookup, else the Pi's.
"""

import json
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import os

from . import freshness, grid as grid_mod, location as location_mod
try:                                    # needs python3-astral (Raspberry Pi OS package)
    from .fieldengine import field_engine as engine
except ImportError:                     # the service still runs; Field propagation says why
    engine = None
from .propagation import PERIODS, RATING_ORDER, _band_mhz

SOURCE = "HamQSL + NOAA SWPC"
HAMQSL_URL = "https://www.hamqsl.com/solarxml.php"
NOAA_KP_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
TIMEOUT_SEC = 10
KP_OLD_SEC = 3 * 3600                   # one 3-hour Kp interval
RETRY_AFTER_FAIL_SEC = 120
USER_AGENT = "HamOpsConsole (github.com/N4MI73/n4mi-hamops-console)"

# What each word means on the DX scale (Ham Shack Automation, decided by Dan 2026-11-07/14).
DX_MEANINGS = {
    "good": "DX is workable on SSB.",
    "fair": "DX on FT8 only, or North America only on SSB. DX is often workable on FT8 even "
            "when SSB is quiet or North America only.",
    "poor": "North America only, or nothing, even on FT8.",
}


def _run_in_thread(job):
    threading.Thread(target=job, name="field-propagation", daemon=True).start()


def _get(url, timeout=TIMEOUT_SEC):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        return reply.read()


# ---------------------------------------------------------------- reading the sources

def parse_hamqsl(data):
    """solarxml.php bytes -> dict of figures (None where missing or unreadable)."""
    root = ET.fromstring(data)
    solar = root.find("solardata")
    if solar is None:
        raise ValueError("HamQSL sent no <solardata>")

    def value(tag, cast):
        el = solar.find(tag)
        if el is None or el.text is None:
            return None
        try:
            return cast(el.text.strip())
        except (TypeError, ValueError):
            return None

    xray = value("xray", str)
    return {
        "sfi": value("solarflux", int),
        "k_index": value("kindex", int),
        "a_index": value("aindex", int),
        "sunspots": value("sunspots", int),
        "xray": xray if xray and xray[0].upper() in "ABCMX" else None,
        "solar_wind_kms": value("solarwind", float),
        "updated": parse_hamqsl_time(value("updated", str)),
    }


def parse_hamqsl_time(text):
    """HamQSL's '17 Nov 2026 2030 GMT' -> aware UTC datetime, or None."""
    if not text:
        return None
    try:
        return datetime.strptime(text.strip(), "%d %b %Y %H%M GMT").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_kp(data):
    """NOAA planetary K JSON -> [(time_utc, kp)] oldest first. Accepts the array-of-objects
    shape and the older array-of-arrays shape (as PropMon does)."""
    out = []
    for row in json.loads(data):
        if isinstance(row, dict):
            tag, kp = row.get("time_tag"), row.get("Kp")
        elif isinstance(row, list) and len(row) >= 2:
            tag, kp = row[0], row[1]
        else:
            continue
        try:
            t = datetime.fromisoformat(str(tag).replace("Z", "")[:19]).replace(tzinfo=timezone.utc)
            out.append((t, float(kp)))
        except (TypeError, ValueError):
            continue                     # header row or malformed entry
    if not out:
        raise ValueError("NOAA sent no Kp readings")
    out.sort(key=lambda r: r[0])
    return out


# ---------------------------------------------------------------- the time zone

def local_zone():
    """The Pi's own zone: an IANA name from /etc/timezone when there is one, else the
    system's current offset."""
    try:
        with open("/etc/timezone", encoding="utf-8") as f:
            name = f.read().strip()
        if name:
            return name
    except OSError:
        pass
    return datetime.now().astimezone().tzinfo


# ---------------------------------------------------------------- rating

def recovery(kp_history, now, model):
    """(active, ends): the 12-hour hold after a 3-hour Kp at storm level."""
    g = model["geomagnetic"]
    hold = timedelta(hours=g["recovery_hours"])
    storms = [t for t, kp in kp_history if round(kp) >= g["recovery_storm_kp"] and now - t <= hold]
    if not storms:
        return False, None
    return True, max(storms) + hold


def rate(figures, kp_history, now, lat, lon, tz, model, scale):
    """Figures + Kp history -> the contract's data fields for this site and moment."""
    r = engine.rate_all(figures.get("sfi"), figures.get("k_index"), kp_history, now, lat, lon, tz,
                        model, scale)
    bands = []
    period = None
    for b in r["bands"]:
        entry = {"band": b["band"], "rating": b["status"] if b["status"] in RATING_ORDER else None}
        if b.get("note"):
            entry["note"] = b["note"]
        bands.append(entry)
        if b["band"] == "20m" and b["tod"] in PERIODS:
            period = b["tod"]          # the 40m-6m time of day, as PropMon's `period`
    bands.sort(key=lambda b: (RATING_ORDER.get(b["rating"], 3), _band_mhz(b["band"])))
    active, ends = recovery(kp_history, now, model)
    meanings = model["field_day"]["meanings"] if scale == "field_day" else DX_MEANINGS
    return {
        "sfi": figures.get("sfi"), "sunspots": figures.get("sunspots"),
        "a_index": figures.get("a_index"), "k_index": figures.get("k_index"),
        "xray": figures.get("xray"), "solar_wind_kms": figures.get("solar_wind_kms"),
        "bands": bands,
        "summary": None,
        "ratings_available": r["available"],
        "period": period,
        "recovery_active": active,
        "recovery_ends": freshness.iso(ends),
        "scale": scale,
        "scale_version": r["scale_version"],
        "model_version": r["model_version"],
        "meanings": dict(meanings),
    }


# ---------------------------------------------------------------- the source

class FieldPropagationSource:
    """Keeps the last good HamQSL figures and NOAA Kp history; rates on request."""

    def __init__(self, settings, forecast=None, fetch=_get, clock=freshness.utcnow,
                 run=_run_in_thread, model=None, zone=local_zone):
        self.settings = settings
        self.forecast = forecast        # for the site's time zone (NWS /points)
        self._fetch = fetch
        self._clock = clock
        self._run = run
        self._zone = zone
        self.model = model or load_model()
        self._lock = threading.Lock()
        self._figures = None            # last good HamQSL figures
        self._kp = []                   # last good Kp history (kept through a failed fetch)
        self._kp_at = None              # when it was fetched
        self._fetched_at = None
        self._attempted_at = None
        self._failed = False
        self._error = None
        self._busy = False

    def _grid(self):
        return location_mod.station_grid(self.settings, "field")

    def _scale(self):
        return "field_day" if self.settings.get("profile") == "field_day" else "dx"

    def _due(self, now):
        if self._busy:
            return False
        if self._attempted_at is None:
            return True
        wait = RETRY_AFTER_FAIL_SEC if self._failed else \
            self.settings["thresholds"]["field_propagation_fetch_min"] * 60
        return (now - self._attempted_at).total_seconds() >= wait

    def _refresh(self):
        try:
            figures = parse_hamqsl(self._fetch(HAMQSL_URL))
            error = None
        except Exception as e:  # noqa: BLE001 -- any failure must end the attempt (busy flag)
            figures, error = None, _describe("HamQSL", e)
        try:
            kp = parse_kp(self._fetch(NOAA_KP_URL))
        except Exception:  # noqa: BLE001 -- Kp only feeds the recovery hold
            kp = None
        with self._lock:
            now = self._clock()
            if figures is not None:
                self._figures, self._fetched_at = figures, now
            if kp is not None:
                self._kp, self._kp_at = kp, now
            self._failed = figures is None
            self._error = error
            self._busy = False

    def block(self):
        now = self._clock()
        start = False
        with self._lock:
            if self._due(now):
                self._busy, self._attempted_at, start = True, now, True
        if start:
            self._run(self._refresh)
        grid = self._grid()
        with self._lock:
            if engine is None:
                out = freshness.block(SOURCE, None, None, 0, error="python3-astral isn't installed on this Pi")
                out.update(_empty(self._scale()))
                return out
            if not grid_mod.is_valid(grid):
                out = freshness.block(SOURCE, None, None, 0, error="Field station grid not set or not valid in Settings")
                out.update(_empty(self._scale()))
                return out
            stale_after = self.settings["thresholds"]["propagation_stale_min"] * 60
            error = self._error or ("Fetching HamQSL" if self._busy and self._figures is None else None)
            out = freshness.block(SOURCE, self._fetched_at, None, stale_after, error=error, now=now)
            if self._figures is None or out["status"] == freshness.UNAVAILABLE:
                out.update(_empty(self._scale()))
                return out
            lat, lon = grid_mod.centre(grid)
            tz = _usable_zone((self.forecast.time_zone(grid) if self.forecast else None) or self._zone())
            # The last good Kp history is used even after a failed fetch: a storm already in it
            # still holds the ratings down. Only newer readings are unknown, and the page says so.
            out.update(rate(self._figures, self._kp, now, lat, lon, tz, self.model, self._scale()))
            out["rated_for"] = grid.strip().upper()[:4]
            if self._kp_at is None:
                out["note"] = "NOAA storm history not available: no storm-recovery hold applied"
                out["recovery_active"] = None
            elif (now - self._kp_at).total_seconds() > KP_OLD_SEC:
                hours = int((now - self._kp_at).total_seconds() // 3600)
                out["note"] = f"NOAA storm history {hours} h old: a newer storm may not be counted"
            return out


def load_model():
    """The engine's checked loader when astral is there; otherwise just the file, so the
    version check still works."""
    if engine is not None:
        return engine.load_model()
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fieldengine", "rating_model.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _usable_zone(tz):
    """An IANA name the system knows, else the Pi's current offset (never an exception)."""
    if isinstance(tz, str):
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(tz)
        except Exception:  # noqa: BLE001 -- unknown or broken zone name
            return datetime.now().astimezone().tzinfo
    return tz


def _empty(scale):
    return {"sfi": None, "sunspots": None, "a_index": None, "k_index": None, "xray": None,
            "solar_wind_kms": None, "bands": [], "summary": None, "ratings_available": None,
            "period": None, "recovery_active": None, "recovery_ends": None, "scale": scale,
            "scale_version": None, "model_version": None, "meanings": None}


def _describe(name, e):
    if isinstance(e, urllib.error.HTTPError):
        return f"{name} answered with an error ({e.code})"
    if isinstance(e, (urllib.error.URLError, OSError)):
        return f"{name} not answering ({e.__class__.__name__})"
    return f"{name} sent an unexpected reply ({e.__class__.__name__})"


class ModePropagation:
    """The propagation block for the current data source: PropMon at home, the field engine
    in the field. Both carry `engine`, the field engine's version check (engine_check.py),
    which is checked at home -- where PropMon's model_version is visible -- and shown in
    both modes."""

    def __init__(self, settings, home, field, check):
        self.settings, self.home, self.field, self.check = settings, home, field, check

    def block(self):
        if self.settings["mode"] == "field":
            out = self.field.block()
        else:
            out = self.home.block()
            self.check.note_propmon(out.get("model_version"))
        out["engine"] = self.check.block()
        return out
