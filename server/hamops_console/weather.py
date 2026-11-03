"""Weather blocks (contract section 5), Home Mode: relayed from Weather Sentinel.

Weather Sentinel owns its JSON (its own project). One GET of
/api/conditions fills three of the console's blocks, each with its own
freshness:
  alerts      <- Sentinel's `nws`        (aged by Sentinel's `last_polled_at`)
  conditions  <- Sentinel's `tempest`    (aged by `observed_at`)
  lightning   <- Sentinel's `lightning`  (aged by when the console fetched it)

Consumer rules agreed with Weather Sentinel (2026-10-12), all applied here:
  1. `"simulation": true` -> every Sentinel block has status `test`, NO alert
     items pass through, the lightning flags are unknown (null), and simulated
     strikes never start the console's own 5-mile hold.
  2. `acknowledged`, `needs_alert`, `level` and `last_transition` are ignored
     and never passed on; the console classifies and sorts alerts itself and
     never calls Sentinel's POST /api/alerts/ack.
  3. Read defensively: unknown fields are ignored; a missing field is
     unknown (null) -- never an exception and never "all clear".

The NWS forecast (both modes) comes from forecast.py. Field Mode's own NWS
alerts and station observations come in a later step; until then, in Field
Mode the alerts and conditions blocks say they aren't built yet.
`weather_radio` is null until the stored NOAA Weather Radio list is built.

Fetching is lazy, as for propagation: at most once per `sentinel_fetch_min`,
keeping the last good reply.
"""

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from . import alert_classes, freshness, grid as grid_mod
from .forecast import ForecastSource

SOURCE = "Weather Sentinel"
PATH = "/api/conditions"
TIMEOUT_SEC = 5


# ---------------------------------------------------------------- helpers

def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _text(value):
    return value if isinstance(value, str) and value.strip() else None


def _bool(value):
    return value if isinstance(value, bool) else None


def _epoch(value):
    value = _num(value)
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _dict(value):
    return value if isinstance(value, dict) else None


# ---------------------------------------------------------------- translation

def translate_alert(raw):
    """One Sentinel alert -> a contract alert item, or None if unusable.
    Sentinel's bedside-unit fields are deliberately not copied (rule 2)."""
    if not isinstance(raw, dict):
        return None
    event = _text(raw.get("event"))
    if not event:
        return None
    return {
        "id": _text(raw.get("id")),
        "event": event,
        "kind": alert_classes.kind(event),
        "hazard_class": alert_classes.hazard_class(event),
        "severity": _text(raw.get("severity")),
        "headline": _text(raw.get("what")) or _text(raw.get("headline")),
        "hazard": _text(raw.get("hazard")),
        "instruction": _text(raw.get("instruction")),
        "onset": freshness.iso(freshness.parse_time(raw.get("onset"))),
        "expires": freshness.iso(freshness.parse_time(raw.get("expires"))),
    }


def sort_alerts(items):
    """Warning -> watch -> advisory -> statement, then soonest-expiring first
    (an alert with no expiry time sorts last within its kind)."""
    far = "9999-12-31T23:59:59Z"
    return sorted(items, key=lambda a: (alert_classes.KIND_ORDER[a["kind"]], a["expires"] or far))


def translate_alerts(nws, now):
    """Sentinel `nws.alerts[]` -> sorted contract items, dropping any whose
    expiry has passed (Sentinel removes them too, but kept data can age)."""
    items = []
    for raw in (nws or {}).get("alerts") or []:
        item = translate_alert(raw)
        if item is None:
            continue
        expires = freshness.parse_time(item["expires"])
        if expires is not None and expires <= now:
            continue
        items.append(item)
    return sort_alerts(items)


def translate_conditions(tempest):
    tempest = tempest or {}
    return {
        "station": None,  # your own Tempest; Field Mode names the NWS station
        "temperature_f": _num(tempest.get("temperature_f")),
        "feels_like_f": _num(tempest.get("feels_like_f")),
        "humidity_pct": _num(tempest.get("humidity_percent")),
        "wind_mph": _num(tempest.get("wind_mph")),
        "wind_dir": _text(tempest.get("wind_direction")),
        "gust_mph": _num(tempest.get("gust_mph")),
        "rain_rate_in_hr": _num(tempest.get("rain_rate_in_hr")),
        "pressure_inhg": _num(tempest.get("pressure_inhg_station")),
    }


EMPTY_CONDITIONS = translate_conditions({})
EMPTY_LIGHTNING = {
    "within_10mi": None, "within_5mi": None, "frequent": None, "closest_mi": None,
    "last_strike_at": None, "strikes_recent": None, "window_min": None,
    "hold_until_10mi": None, "hold_until_5mi": None, "hold_since_5mi": None,
}


# ---------------------------------------------------------------- fetching

def fetch_sentinel(base_url, timeout=TIMEOUT_SEC):
    """GET Weather Sentinel's /api/conditions. Returns a dict; raises on failure.
    Read-only: the console never POSTs to Weather Sentinel."""
    url = base_url.rstrip("/") + PATH
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "HamOpsConsole"})
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        return json.load(reply)


class WeatherSource:
    """Keeps the last good Weather Sentinel reply and builds the weather blocks."""

    def __init__(self, settings, fetch=fetch_sentinel, clock=freshness.utcnow, forecast=None):
        self.settings = settings
        self.forecast = forecast or ForecastSource(settings, clock=clock)
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._raw = None            # last good reply (dict)
        self._fetched_at = None
        self._attempted_at = None
        self._error = None
        self._hold_5mi_until = None  # the console's own 5-mile lightning hold
        self._hold_5mi_since = None  # when the current hold began (stable id for Acknowledge)

    # -- settings
    def _t(self, key):
        return self.settings["thresholds"][key]

    # -- fetching
    def _refresh_if_due(self, now):
        if self._attempted_at and (now - self._attempted_at).total_seconds() < self._t("sentinel_fetch_min") * 60:
            return
        self._attempted_at = now
        base = self.settings["sources"].get("sentinel_url")
        if not base:
            self._error = "Weather Sentinel address not set in Settings"
            return
        try:
            raw = self._fetch(base)
        except (urllib.error.URLError, OSError, ValueError) as e:
            self._error = f"{SOURCE} not answering ({e.__class__.__name__})"
            return
        if not isinstance(raw, dict):
            self._error = f"{SOURCE} sent an unexpected reply"
            return
        self._raw = raw
        self._fetched_at = now
        self._error = None
        self._note_close_strikes(raw, now)

    def _simulation(self):
        # Only an explicit true counts as test mode. Sentinel always sends the
        # flag; if a future reply lacked it, the data is treated as real.
        return (self._raw or {}).get("simulation") is True

    def _note_close_strikes(self, raw, now):
        """The console's own 5-mile hold (contract section 5): each strike at or
        inside `lightning_high_mi` holds `within_5mi` for `lightning_hold_min`.
        Uses Sentinel's most recent close strike (distance and time) when it is
        inside 5 mi; if only the window's closest strike is inside 5 mi and its
        time isn't given, the hold starts now (the longer, safer choice)."""
        if raw.get("simulation") is True:
            return
        lightning = _dict(raw.get("lightning")) or {}
        close = _dict(lightning.get("close")) or {}
        high = self._t("lightning_high_mi")
        hold = timedelta(minutes=self._t("lightning_hold_min"))
        strike_time = None
        last_distance = _num(close.get("last_distance_mi"))
        if last_distance is not None and last_distance <= high:
            strike_time = _epoch(close.get("last_strike_epoch"))
        closest = _num(lightning.get("closest_distance_mi"))
        if strike_time is None and closest is not None and closest <= high:
            strike_time = now
        if strike_time is not None:
            until = strike_time + hold
            if self._hold_5mi_until is None or strike_time >= self._hold_5mi_until:
                self._hold_5mi_since = strike_time   # a new hold, not an extension
            if self._hold_5mi_until is None or until > self._hold_5mi_until:
                self._hold_5mi_until = until

    # -- blocks
    def _alerts_block(self, now):
        raw = self._raw or {}
        nws = _dict(raw.get("nws"))
        issued = freshness.parse_time(nws.get("last_polled_at")) if nws else None
        error = self._error
        force_stale = False
        if self._raw is not None and nws is None:
            error, force_stale = f"{SOURCE} sent no NWS alert information", True
        elif nws is not None and nws.get("available") is not True:
            error, force_stale = f"{SOURCE} can't reach NWS", True
        test = self._simulation()
        out = freshness.block(
            SOURCE, self._fetched_at if nws is not None or self._raw is None else None,
            issued, self._t("nws_alerts_stale_min") * 60, error=error, now=now, test=test,
            force_stale=force_stale, unavailable_after_sec=self._t("nws_alerts_unavailable_min") * 60)
        if test:
            out["message"] = "Home weather station in test mode"
        show = out["status"] in (freshness.OK, freshness.STALE) and nws is not None
        out["items"] = translate_alerts(nws, now) if show else []
        return out

    def _conditions_block(self, now):
        raw = self._raw or {}
        tempest = _dict(raw.get("tempest"))
        issued = freshness.parse_time(tempest.get("observed_at")) if tempest else None
        error = self._error
        force_stale = False
        if tempest is not None and tempest.get("available") is not True:
            error, force_stale = "Home weather station (Tempest) not reporting", True
        test = self._simulation()
        out = freshness.block(
            SOURCE, self._fetched_at if tempest is not None or self._raw is None else None,
            issued, self._t("conditions_stale_min") * 60, error=error, now=now, test=test,
            force_stale=force_stale)
        if self._raw is not None and tempest is None:
            out["message"] = f"{SOURCE} sent no Tempest information"
        if test:
            out["message"] = "Home weather station in test mode"
        show = out["status"] != freshness.UNAVAILABLE and tempest is not None
        out.update(translate_conditions(tempest) if show else EMPTY_CONDITIONS)
        return out

    def _lightning_block(self, now):
        raw = self._raw or {}
        lightning = _dict(raw.get("lightning"))
        test = self._simulation()
        out = freshness.block(
            SOURCE, self._fetched_at if lightning is not None or self._raw is None else None,
            None, self._t("lightning_stale_min") * 60, error=self._error, now=now, test=test)
        if self._raw is not None and lightning is None:
            out["message"] = f"{SOURCE} sent no lightning information"
        if test:
            out["message"] = "Home weather station in test mode"
        out.update(EMPTY_LIGHTNING)
        if lightning is None or out["status"] == freshness.UNAVAILABLE:
            return out
        close = _dict(lightning.get("close")) or {}
        last_close = _epoch(close.get("last_strike_epoch"))
        hold_10 = None
        if close.get("active") is True and last_close is not None and _num(close.get("hold_minutes")) is not None:
            hold_10 = last_close + timedelta(minutes=close["hold_minutes"])
        per_minute = _num(close.get("strikes_last_minute"))
        out.update({
            "closest_mi": _num(lightning.get("closest_distance_mi")),
            "last_strike_at": freshness.iso(freshness.parse_time(lightning.get("most_recent_strike_at"))),
            "strikes_recent": _num(lightning.get("strike_count_recent")),
            "window_min": _num(lightning.get("window_minutes")),
        })
        if test:
            return out  # flags stay null: a simulated strike must not raise an alert
        within_5 = self._hold_5mi_until is not None and now < self._hold_5mi_until
        out.update({
            "within_10mi": _bool(close.get("active")),
            "within_5mi": within_5,
            "frequent": None if per_minute is None else per_minute > self._t("lightning_frequent_per_min"),
            "hold_until_10mi": freshness.iso(hold_10),
            "hold_until_5mi": freshness.iso(self._hold_5mi_until) if within_5 else None,
            # Extra field (not in contracts 0.3): when this 5-mile hold began, so the
            # alert list can flash once per hold and key Acknowledge on it.
            "hold_since_5mi": freshness.iso(self._hold_5mi_since) if within_5 else None,
        })
        return out

    def _not_built(self, message):
        out = freshness.block("NWS", None, None, 0, error=message)
        return out

    def _location(self):
        mode = self.settings["mode"]
        station = self.settings["station"].get(mode, {})
        grid = station.get("grid")
        location = {"label": mode.capitalize(), "grid": grid,
                    "grid_source": station.get("grid_source")}
        location.update(self.forecast.location())
        location["grid_warning"] = (grid_mod.COARSE_WARNING
                                    if grid_mod.is_valid(grid) and len(grid.strip()) == 4 else None)
        return location

    def blocks(self):
        """The weather blocks for GET /api/weather (everything but the envelope)."""
        now = self._clock()
        forecast = self.forecast.block()
        if self.settings["mode"] != "home":
            msg = "Field Mode weather is not built yet"
            alerts = self._not_built(msg)
            alerts["items"] = []
            conditions = self._not_built(msg)
            conditions.update(EMPTY_CONDITIONS)
            return {"location": self._location(), "alerts": alerts, "conditions": conditions,
                    "lightning": None, "weather_radio": None, "forecast": forecast}
        with self._lock:
            self._refresh_if_due(now)
            alerts = self._alerts_block(now)
            out = {
                "location": self._location(),
                "alerts": alerts,
                "conditions": self._conditions_block(now),
                "lightning": self._lightning_block(now),
                "weather_radio": None,   # stored NOAA Weather Radio list: later step
                "forecast": forecast,
            }
        apply_storm_alerts(forecast, alerts, now)
        return out


# Event names that make lightning "likely" on their own (contract section 5).
STORM_ALERT_EVENTS = ("severe thunderstorm warning", "severe thunderstorm watch")
STORM_WORDS = ("thunderstorm", "lightning")


def storm_alert(item):
    """True for a Severe Thunderstorm Warning/Watch, or a Special Weather
    Statement whose text mentions thunderstorms or lightning."""
    event = (item.get("event") or "").lower()
    if event in STORM_ALERT_EVENTS:
        return True
    if event == "special weather statement":
        text = " ".join(item.get(k) or "" for k in ("headline", "hazard")).lower()
        return any(w in text for w in STORM_WORDS)
    return False


def apply_storm_alerts(forecast, alerts, now):
    """An active storm alert sets lightning_outlook to likely, starting now,
    with basis nws_alert -- even when the forecast itself shows nothing.
    Only alerts the console is actually showing (ok or stale) count."""
    if not forecast or alerts.get("status") not in (freshness.OK, freshness.STALE):
        return
    if any(storm_alert(i) for i in alerts.get("items") or []):
        forecast["lightning_outlook"] = {"level": "likely", "start": freshness.iso(now),
                                         "basis": "nws_alert"}
