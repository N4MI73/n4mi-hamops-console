"""Forecast block (contract section 5), both modes: from NWS api.weather.gov.

NWS owns its API. The console finds the forecast for the centre of the
current mode's station grid:
  1. GET /points/{lat},{lon}  -> forecast URLs, office, zone, county.
     Re-checked when the grid changes and once a day.
  2. Every `forecast_fetch_min` (30): the 12-hour forecast, the hourly
     forecast and the gridpoint raw data (for thunderstorms and gusts).
     All three must succeed or the last good set is kept, so a missing
     gridpoint can never be read as "no thunderstorms".

Fetching runs in the background: a request always gets the last good
forecast at once and, if a refresh is due, starts one. Over a weak field
hotspot NWS can be slow, and the dashboard must not wait for it. After a
failure the next try comes after `RETRY_AFTER_FAIL_SEC`, not 30 minutes.

Freshness: `issued_at` is NWS `updateTime` (when the forecaster last
revised it), and the block is stale when that is older than
`forecast_stale_hours` (12). NWS revises a few times a day, so a 6-hour-old
`updateTime` is normal (live sample 2026-11-03).

Thunderstorms come from the gridpoint `weather` series. Its time values
are intervals ("2026-11-05T12:00:00+00:00/PT12H"), one interval can hold
several weather items, and coverage words use underscores
("slight_chance"); `thunder_next.coverage` keeps NWS's own word.
`probabilityOfThunder` is not used: it was empty for this office even with
thunderstorms forecast.

NWS requires a User-Agent naming the application and a contact; the
contact is the `nws_user_agent_contact` setting.
"""

import json
import re
import threading
import urllib.error
import urllib.request
from datetime import timedelta

from . import __version__, freshness, grid as grid_mod

SOURCE = "NWS"
API = "https://api.weather.gov"
TIMEOUT_SEC = 10
RETRY_AFTER_FAIL_SEC = 120
POINTS_RECHECK_SEC = 24 * 3600
HOURS = 12      # hourly entries kept, and the thunderstorm look-ahead
PERIODS = 4     # 12-hour periods kept (enough for a 24-hour Field Day)
KMH_TO_MPH = 0.621371

LIKELY = {"likely", "numerous", "widespread", "definite"}
POSSIBLE = {"slight_chance", "chance", "isolated", "scattered"}
COVERAGE_RANK = {"slight_chance": 1, "isolated": 1, "chance": 2, "scattered": 2,
                 "likely": 3, "numerous": 3, "widespread": 4, "definite": 4}


# ---------------------------------------------------------------- helpers

def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _text(value):
    return value if isinstance(value, str) and value.strip() else None


def _props(doc):
    doc = doc if isinstance(doc, dict) else {}
    props = doc.get("properties")
    return props if isinstance(props, dict) else {}


def _last_part(url):
    """'https://api.weather.gov/zones/forecast/VAZ054' -> 'VAZ054'."""
    return url.rstrip("/").rsplit("/", 1)[-1] if _text(url) else None


_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")


def parse_interval(value):
    """NWS validTime 'start/PT12H' -> (start, end) datetimes, or None."""
    if not isinstance(value, str) or "/" not in value:
        return None
    start_text, duration = value.split("/", 1)
    start = freshness.parse_time(start_text)
    match = _DURATION.match(duration)
    if start is None or not match or not any(match.groups()):
        return None
    days, hours, minutes = (int(g or 0) for g in match.groups())
    return start, start + timedelta(days=days, hours=hours, minutes=minutes)


def _floor_hour(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def _wind_mph(text):
    """'7 mph' -> 7; '6 to 9 mph' -> 9 (the upper figure); else None."""
    numbers = re.findall(r"\d+", text) if isinstance(text, str) else []
    return int(numbers[-1]) if numbers else None


def _percent(value):
    return _num(value.get("value")) if isinstance(value, dict) else None


# ---------------------------------------------------------------- gridpoint series

def thunder_intervals(gridpoint_props):
    """[(start, end, coverage)] for every interval with a thunderstorm item.
    When an interval lists more than one, the highest coverage is kept."""
    out = []
    series = gridpoint_props.get("weather")
    values = series.get("values") if isinstance(series, dict) else None
    for entry in values or []:
        if not isinstance(entry, dict):
            continue
        span = parse_interval(entry.get("validTime"))
        if span is None:
            continue
        found, best = False, None
        for item in entry.get("value") or []:
            if not isinstance(item, dict) or item.get("weather") != "thunderstorms":
                continue
            found = True
            coverage = _text(item.get("coverage"))
            if best is None or COVERAGE_RANK.get(coverage, 0) > COVERAGE_RANK.get(best, 0):
                best = coverage
        if found:
            out.append((span[0], span[1], best))
    return out


def gust_series(gridpoint_props):
    """[(start, end, mph)] from the gridpoint windGust series (km/h)."""
    series = gridpoint_props.get("windGust")
    if not isinstance(series, dict):
        return []
    to_mph = KMH_TO_MPH if "km_h" in str(series.get("uom", "km_h")) else 1.0
    out = []
    for entry in series.get("values") or []:
        if not isinstance(entry, dict):
            continue
        span, value = parse_interval(entry.get("validTime")), _num(entry.get("value"))
        if span and value is not None:
            out.append((span[0], span[1], round(value * to_mph)))
    return out


def _at(series, when):
    for start, end, value in series:
        if start <= when < end:
            return value
    return None


def thunder_outlook(intervals, now, hours=HOURS):
    """(thunder_next, lightning_outlook) from the thunderstorm intervals.

    thunder_next: the first hour in the next `hours` with thunderstorms and
    its coverage word, or None.
    lightning_outlook: `likely` if any coverage in the window is likely or
    stronger, else `possible` if any is weaker; `start` is the first hour at
    that level, so "likely within 2 hours" isn't triggered by an earlier
    slight chance. None when no thunderstorms are forecast."""
    window_end = now + timedelta(hours=hours)
    hits = []
    for start, end, coverage in sorted(intervals, key=lambda i: i[0]):
        if end <= now or start >= window_end:
            continue
        hits.append((_floor_hour(max(start, now)), coverage))
    if not hits:
        return None, None
    first_time, first_cov = hits[0]
    thunder_next = {"start": freshness.iso(first_time), "coverage": first_cov}
    likely = [t for t, c in hits if c in LIKELY]
    if likely:
        outlook = {"level": "likely", "start": freshness.iso(likely[0]), "basis": "forecast"}
    else:
        # Thunderstorms with an unrecognised coverage word still count as possible.
        outlook = {"level": "possible", "start": freshness.iso(first_time), "basis": "forecast"}
    return thunder_next, outlook


# ---------------------------------------------------------------- translation

def translate(forecast, hourly, gridpoint, now):
    """The three NWS documents -> the contract's forecast data fields."""
    gp = _props(gridpoint)
    thunder = thunder_intervals(gp)
    gusts = gust_series(gp)

    hours = []
    for period in _props(hourly).get("periods") or []:
        if not isinstance(period, dict):
            continue
        start = freshness.parse_time(period.get("startTime"))
        end = freshness.parse_time(period.get("endTime"))
        if start is None or (end is not None and end <= now):
            continue
        hours.append({
            "time": freshness.iso(start),
            "temperature_f": _num(period.get("temperature")),
            "rain_chance_pct": _percent(period.get("probabilityOfPrecipitation")),
            "wind_mph": _wind_mph(period.get("windSpeed")),
            "gust_mph": _at(gusts, start),
            "wind_dir": _text(period.get("windDirection")),
            "thunder": _at(thunder, start),
            "short": _text(period.get("shortForecast")),
        })
        if len(hours) == HOURS:
            break

    periods = []
    for period in _props(forecast).get("periods") or []:
        if not isinstance(period, dict):
            continue
        end = freshness.parse_time(period.get("endTime"))
        if end is not None and end <= now:
            continue
        wind = " ".join(t for t in (_text(period.get("windSpeed")), _text(period.get("windDirection"))) if t)
        periods.append({
            "name": _text(period.get("name")),
            "start": freshness.iso(freshness.parse_time(period.get("startTime"))),
            "end": freshness.iso(end),
            "temperature_f": _num(period.get("temperature")),
            "rain_chance_pct": _percent(period.get("probabilityOfPrecipitation")),
            "wind": wind or None,
            "short": _text(period.get("shortForecast")),
            "detail": _text(period.get("detailedForecast")),
        })
        if len(periods) == PERIODS:
            break

    thunder_next, outlook = thunder_outlook(thunder, now)
    return {"thunder_next": thunder_next, "lightning_outlook": outlook,
            "hourly": hours, "periods": periods}


EMPTY = {"thunder_next": None, "lightning_outlook": None, "hourly": [], "periods": []}


# ---------------------------------------------------------------- fetching

def user_agent(contact):
    contact = contact if isinstance(contact, str) and contact.strip() and "<" not in contact else None
    base = f"HamOpsConsole/{__version__}"
    return f"{base} ({contact.strip()})" if contact else base


def fetch_json(url, contact, timeout=TIMEOUT_SEC):
    """GET one NWS document. Returns a dict; raises on failure."""
    request = urllib.request.Request(url, headers={"Accept": "application/geo+json",
                                                   "User-Agent": user_agent(contact)})
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        return json.load(reply)


def _run_in_thread(job):
    threading.Thread(target=job, name="forecast-refresh", daemon=True).start()


class ForecastSource:
    """Keeps the last good NWS forecast and refreshes it in the background."""

    def __init__(self, settings, fetch=fetch_json, clock=freshness.utcnow, run=_run_in_thread):
        self.settings = settings
        self._fetch = fetch
        self._clock = clock
        self._run = run
        self._lock = threading.Lock()
        self._busy = False
        self._grid = None           # grid the stored data belongs to
        self._points = None         # {"forecast", "hourly", "gridpoint", "office", "zone", "county"}
        self._points_at = None
        self._docs = None           # (forecast, hourly, gridpoint) last good
        self._issued_at = None
        self._fetched_at = None
        self._attempted_at = None
        self._failed = False
        self._error = None

    def _station_grid(self):
        mode = self.settings["mode"]
        return (self.settings["station"].get(mode) or {}).get("grid")

    # -- refresh (runs in the background)
    def _due(self, now, grid):
        if self._busy:
            return False
        if grid != self._grid or self._attempted_at is None:
            return True
        wait = RETRY_AFTER_FAIL_SEC if self._failed else self.settings["thresholds"]["forecast_fetch_min"] * 60
        return (now - self._attempted_at).total_seconds() >= wait

    def _refresh(self, grid):
        contact = self.settings.get("nws_user_agent_contact")
        now = self._clock()
        try:
            points, points_at = self._points, self._points_at
            if grid != self._grid or points is None or (now - points_at).total_seconds() >= POINTS_RECHECK_SEC:
                lat, lon = grid_mod.centre(grid)
                p = _props(self._fetch(f"{API}/points/{lat},{lon}", contact))
                points = {"forecast": _text(p.get("forecast")), "hourly": _text(p.get("forecastHourly")),
                          "gridpoint": _text(p.get("forecastGridData")), "office": _text(p.get("cwa")),
                          "zone": _last_part(p.get("forecastZone")), "county": _last_part(p.get("county"))}
                if not (points["forecast"] and points["hourly"] and points["gridpoint"]):
                    raise ValueError("NWS has no forecast for this location")
                points_at = now
            docs = tuple(self._fetch(points[k], contact) for k in ("forecast", "hourly", "gridpoint"))
            issued = freshness.parse_time(_props(docs[0]).get("updateTime"))
            if not _props(docs[0]).get("periods") or not _props(docs[1]).get("periods"):
                raise ValueError("NWS sent an empty forecast")
        except Exception as e:  # noqa: BLE001 -- any failure must end the attempt, or
            # _busy would stay set and the forecast would never refresh again.
            with self._lock:
                if grid != self._grid:      # a different place: old data no longer applies
                    self._grid, self._points, self._docs = grid, None, None
                    self._issued_at = self._fetched_at = None
                self._failed = True
                self._error = self._describe(e)
                self._busy = False
            return
        with self._lock:
            self._grid, self._points, self._points_at = grid, points, points_at
            self._docs, self._issued_at, self._fetched_at = docs, issued, self._clock()
            self._failed, self._error, self._busy = False, None, False

    @staticmethod
    def _describe(e):
        if isinstance(e, urllib.error.HTTPError):
            return f"NWS answered with an error ({e.code})"
        if isinstance(e, ValueError) and str(e).startswith(("NWS", "not a")):
            return str(e)
        if isinstance(e, (urllib.error.URLError, OSError)):
            return f"NWS not answering ({e.__class__.__name__})"
        return f"NWS sent an unexpected reply ({e.__class__.__name__})"

    # -- block
    def location(self):
        """NWS office, zone and county for the location block (None until known)."""
        with self._lock:
            p = self._points or {}
            return {"nws_office": p.get("office"), "nws_zone": p.get("zone"), "nws_county": p.get("county")}

    def block(self):
        now = self._clock()
        grid = self._station_grid()
        with self._lock:
            if not grid_mod.is_valid(grid):
                out = freshness.block(SOURCE, None, None, 0, error="Station grid not set or not valid in Settings")
                out.update(EMPTY)
                return out
            if self._due(now, grid):
                self._busy = True
                self._attempted_at = now
                start = True
            else:
                start = False
        if start:
            self._run(lambda: self._refresh(grid))
        with self._lock:
            have = self._docs is not None and self._grid == grid
            stale_after = self.settings["thresholds"]["forecast_stale_hours"] * 3600
            out = freshness.block(SOURCE, self._fetched_at if have else None,
                                  self._issued_at if have else None, stale_after,
                                  error=self._error or ("Fetching the NWS forecast" if self._busy else None),
                                  now=now)
            out.update(translate(*self._docs, now) if have else EMPTY)
            return out
