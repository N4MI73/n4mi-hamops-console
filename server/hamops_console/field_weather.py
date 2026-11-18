"""Field Mode weather: NWS alerts and the nearest NWS station's observation (contract section 5).

Built 2026-11-16 from live samples (Claude Project file live_samples_2026-11-16_nws_field.md).
In Home Mode these blocks come from Weather Sentinel (weather.py); in Field Mode the console asks
NWS itself, for the centre of the Field station grid:

  alerts      GET /alerts/active?point={lat},{lon}       every `nws_alerts_fetch_min` (2)
  conditions  the observation-station list from /points  (taken from the forecast's /points lookup)
              then GET /stations/{id}/observations/latest every `station_obs_fetch_min` (15)

Fetching runs in the background, like the forecast: a request always gets the last good data at
once, so the screen never waits for NWS over a weak hotspot. After a failure the next try comes
after RETRY_AFTER_FAIL_SEC.

Alerts -- `expires` is not the end of the hazard. In NWS CAP, `expires` is when the *message* is
due to be replaced; `ends` (or `parameters.eventEndingTime`) is when the hazard ends. On
2026-11-16, 189 of 588 alerts NWS still listed as active had a past `expires` (a Winter Storm
Watch "expired" at 04:00 but ran to Friday). So every alert NWS lists is kept, "until" uses
`ends`, and an alert is dropped by time only once its `ends` has passed. An alert without `ends`
stays while NWS lists it. Only `status: Actual` alerts are used (no exercises or tests).

Conditions -- NWS lists stations nearest first, with its own distance. A small field site can sit
near a station that reports irregularly, so if the nearest one's latest report is older than
FALLBACK_AFTER_MIN or has no temperature, the next is tried (up to STATIONS_TRIED). If none is
good, the freshest report found is shown (and marked stale by its age). NWS reports in metric
units; values that failed NWS quality control (`qualityControl: "X"`) count as missing.

Reviewed 2026-11-16 (code review): missing `status` now kept; exact unit match.
"""

import threading
import urllib.error
from datetime import timedelta

from . import alert_classes, freshness, grid as grid_mod, location as location_mod
from .forecast import API, fetch_json
from .glm import compass, distance_mi
from .nws_text import extract_reason_fields

SOURCE = "NWS"
RETRY_AFTER_FAIL_SEC = 120
STATIONS_RECHECK_SEC = 24 * 3600
STATIONS_TRIED = 3
FALLBACK_AFTER_MIN = 120
M_PER_MI = 1609.344
PA_PER_INHG = 3386.389


def _run_in_thread(job):
    threading.Thread(target=job, name="field-weather", daemon=True).start()


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _dict(value):
    return value if isinstance(value, dict) else {}


def _describe(e):
    if isinstance(e, urllib.error.HTTPError):
        return f"NWS answered with an error ({e.code})"
    if isinstance(e, ValueError) and str(e).startswith("NWS"):
        return str(e)
    if isinstance(e, (urllib.error.URLError, OSError)):
        return f"NWS not answering ({e.__class__.__name__})"
    return f"NWS sent an unexpected reply ({e.__class__.__name__})"


# ---------------------------------------------------------------- alerts

def alert_ends(props):
    """When the hazard ends: `ends`, else parameters.eventEndingTime, else None."""
    ends = freshness.parse_time(props.get("ends"))
    if ends is None:
        values = _dict(props.get("parameters")).get("eventEndingTime")
        if isinstance(values, list) and values:
            ends = freshness.parse_time(values[0])
    return ends


def translate_alert(props):
    """One NWS alert's `properties` -> a contract alert item, or None if unusable."""
    props = _dict(props)
    event = _text(props.get("event"))
    # Exercises, tests and drafts are left out; a missing status counts as real (a missing
    # field is unknown, never a reason to hide an alert).
    if not event or props.get("status") not in ("Actual", None) or props.get("messageType") == "Cancel":
        return None
    what, hazard = extract_reason_fields(props)
    return {
        "id": _text(props.get("id")),
        "event": event,
        "kind": alert_classes.kind(event),
        "hazard_class": alert_classes.hazard_class(event),
        "severity": _text(props.get("severity")),
        "headline": what or _text(props.get("headline")),
        "hazard": hazard,
        "instruction": _text(props.get("instruction")),
        "onset": freshness.iso(freshness.parse_time(props.get("onset"))),
        "expires": freshness.iso(freshness.parse_time(props.get("expires"))),
        "ends": freshness.iso(alert_ends(props)),
    }


def translate_alerts(doc, now):
    """An /alerts/active reply -> contract items (unsorted), dropping only alerts whose `ends`
    has passed (never by `expires`)."""
    items = []
    for feature in _dict(doc).get("features") or []:
        item = translate_alert(_dict(feature).get("properties"))
        if item is None:
            continue
        ends = freshness.parse_time(item["ends"])
        if ends is not None and ends <= now:
            continue
        items.append(item)
    return items


# ---------------------------------------------------------------- conditions

def _value(obs, key, unit):
    """A quantity in the expected WMO unit, or None (missing, failed QC, or another unit)."""
    q = _dict(obs.get(key))
    v = q.get("value")
    if not isinstance(v, (int, float)) or isinstance(v, bool) or q.get("qualityControl") == "X":
        return None
    code = q.get("unitCode") or ""
    return float(v) if code.split(":")[-1] == unit else None


def _c_to_f(c):
    return None if c is None else round(c * 9 / 5 + 32, 1)


def _kmh_to_mph(k):
    return None if k is None else round(k * 0.621371, 1)


def translate_observation(obs):
    """An observation's `properties` -> the contract's conditions data fields (minus station)."""
    obs = _dict(obs)
    temp = _value(obs, "temperature", "degC")
    chill, heat = _value(obs, "windChill", "degC"), _value(obs, "heatIndex", "degC")
    feels = chill if chill is not None else heat if heat is not None else temp
    rh = _value(obs, "relativeHumidity", "percent")
    wdir = _value(obs, "windDirection", "degree_(angle)")
    wind = _kmh_to_mph(_value(obs, "windSpeed", "km_h-1"))
    pa = _value(obs, "barometricPressure", "Pa")
    return {
        "temperature_f": _c_to_f(temp),
        "feels_like_f": _c_to_f(feels),
        "humidity_pct": None if rh is None else round(rh),
        "wind_mph": wind,
        "wind_dir": None if wdir is None or not wind else compass(wdir),
        "gust_mph": _kmh_to_mph(_value(obs, "windGust", "km_h-1")),
        "rain_rate_in_hr": None,     # NWS stations give no rain rate
        "pressure_inhg": None if pa is None else round(pa / PA_PER_INHG, 2),
    }


EMPTY_CONDITIONS = dict(translate_observation({}), station=None)


def station_list(doc, site):
    """The /stations reply -> [{id, name, distance_mi}], nearest first.
    NWS gives the distance; it is worked out from the station's position if not."""
    out = []
    for i, feature in enumerate(_dict(doc).get("features") or []):
        p = _dict(_dict(feature).get("properties"))
        sid = _text(p.get("stationIdentifier"))
        if not sid:
            continue
        d = _value(p, "distance", "m")
        miles = None if d is None else d / M_PER_MI
        coords = _dict(_dict(feature).get("geometry")).get("coordinates")
        if miles is None and isinstance(coords, list) and len(coords) >= 2 and site:
            try:
                miles = distance_mi(site[0], site[1], float(coords[1]), float(coords[0]))
            except (TypeError, ValueError):
                miles = None
        out.append({"id": sid, "name": _text(p.get("name")),
                    "distance_mi": None if miles is None else round(miles, 1), "_order": i})
    out.sort(key=lambda s: (s["distance_mi"] is None, s["distance_mi"] or 0, s["_order"]))
    for s in out:
        s.pop("_order")
    return out


# ---------------------------------------------------------------- the source

class FieldWeather:
    """Keeps the last good Field Mode alerts and observation, refreshed in the background."""

    def __init__(self, settings, forecast, fetch=fetch_json, clock=freshness.utcnow, run=_run_in_thread):
        self.settings = settings
        self.forecast = forecast        # supplies the observation-station list URL from /points
        self._fetch = fetch
        self._clock = clock
        self._run = run
        self._lock = threading.Lock()
        self._alerts = {"grid": None, "doc": None, "fetched": None, "issued": None,
                        "attempted": None, "failed": False, "error": None, "busy": False}
        self._obs = {"grid": None, "obs": None, "station": None, "fetched": None,
                     "attempted": None, "failed": False, "error": None, "busy": False}
        self._stations = {"url": None, "list": None, "at": None}

    def _grid(self):
        return location_mod.station_grid(self.settings, "field")

    def _t(self, key):
        return self.settings["thresholds"][key]

    def _contact(self):
        return self.settings.get("nws_user_agent_contact")

    @staticmethod
    def _due(state, now, grid, every_sec):
        if state["busy"]:
            return False
        if grid != state["grid"] or state["attempted"] is None:
            return True
        wait = RETRY_AFTER_FAIL_SEC if state["failed"] else every_sec
        return (now - state["attempted"]).total_seconds() >= wait

    def _start_if_due(self, state, grid, every_sec, job):
        now = self._clock()
        with self._lock:
            if not self._due(state, now, grid, every_sec):
                return
            state["busy"], state["attempted"] = True, now
        self._run(lambda: job(grid))

    def _fail(self, state, grid, e):
        with self._lock:
            if grid != state["grid"]:           # a different place: old data no longer applies
                for k in state:
                    if k not in ("attempted", "busy"):
                        state[k] = None
                state["grid"] = grid
            state["failed"], state["error"], state["busy"] = True, _describe(e), False

    # -- alerts
    def _refresh_alerts(self, grid):
        try:
            lat, lon = grid_mod.centre(grid)
            doc = self._fetch(f"{API}/alerts/active?point={lat},{lon}", self._contact())
            if not isinstance(doc, dict) or not isinstance(doc.get("features"), list):
                raise ValueError("NWS sent no alert list")
        except Exception as e:  # noqa: BLE001 -- any failure must end the attempt (busy flag)
            self._fail(self._alerts, grid, e)
            return
        with self._lock:
            a = self._alerts
            a.update(grid=grid, doc=doc, fetched=self._clock(), issued=freshness.parse_time(doc.get("updated")),
                     failed=False, error=None, busy=False)

    def alerts_block(self):
        now = self._clock()
        grid = self._grid()
        if not grid_mod.is_valid(grid):
            out = freshness.block(SOURCE, None, None, 0, error="Field station grid not set or not valid in Settings")
            out["items"] = []
            return out
        self._start_if_due(self._alerts, grid, self._t("nws_alerts_fetch_min") * 60, self._refresh_alerts)
        with self._lock:
            a = self._alerts
            have = a["doc"] is not None and a["grid"] == grid
            out = freshness.block(
                SOURCE, a["fetched"] if have else None, a["issued"] if have else None,
                self._t("nws_alerts_stale_min") * 60,
                error=a["error"] or ("Fetching NWS alerts" if a["busy"] else None), now=now,
                unavailable_after_sec=self._t("nws_alerts_unavailable_min") * 60)
            show = have and out["status"] in (freshness.OK, freshness.STALE)
            out["items"] = translate_alerts(a["doc"], now) if show else []
            return out

    # -- conditions
    def _stations_for(self, url, site, now):
        st = self._stations
        if st["url"] == url and st["list"] and (now - st["at"]).total_seconds() < STATIONS_RECHECK_SEC:
            return st["list"]
        found = station_list(self._fetch(url, self._contact()), site)
        if not found:
            raise ValueError("NWS lists no observation station near here")
        st.update(url=url, list=found, at=now)
        return found

    def _refresh_obs(self, grid):
        try:
            url = self.forecast.stations_url(grid)
            if not url:
                raise ValueError("NWS location not known yet (waiting for the forecast lookup)")
            now = self._clock()
            site = grid_mod.centre(grid)
            stations = self._stations_for(url, site, now)
            best = None                     # (obs, station, time) freshest found
            error = None
            for station in stations[:STATIONS_TRIED]:
                try:
                    doc = self._fetch(f"{API}/stations/{station['id']}/observations/latest", self._contact())
                except Exception as e:  # noqa: BLE001 -- try the next station
                    error = e
                    continue
                obs = _dict(_dict(doc).get("properties"))
                t = freshness.parse_time(obs.get("timestamp"))
                if t is None:
                    continue
                if best is None or t > best[2]:
                    best = (obs, station, t)
                good = (now - t) <= timedelta(minutes=FALLBACK_AFTER_MIN) and \
                    translate_observation(obs)["temperature_f"] is not None
                if good:
                    best = (obs, station, t)
                    break
            if best is None:
                raise error or ValueError("NWS has no recent report from the nearest stations")
        except Exception as e:  # noqa: BLE001
            self._fail(self._obs, grid, e)
            return
        with self._lock:
            self._obs.update(grid=grid, obs=best[0], station=best[1], fetched=self._clock(),
                             failed=False, error=None, busy=False)

    def conditions_block(self):
        now = self._clock()
        grid = self._grid()
        if not grid_mod.is_valid(grid):
            out = freshness.block(SOURCE, None, None, 0, error="Field station grid not set or not valid in Settings")
            out.update(EMPTY_CONDITIONS)
            return out
        self._start_if_due(self._obs, grid, self._t("station_obs_fetch_min") * 60, self._refresh_obs)
        with self._lock:
            o = self._obs
            have = o["obs"] is not None and o["grid"] == grid
            issued = freshness.parse_time(o["obs"].get("timestamp")) if have else None
            out = freshness.block(
                SOURCE, o["fetched"] if have else None, issued, self._t("station_obs_stale_min") * 60,
                error=o["error"] or ("Fetching the nearest NWS station" if o["busy"] else None), now=now)
            if have and out["status"] != freshness.UNAVAILABLE:
                out.update(translate_observation(o["obs"]), station=dict(o["station"]))
            else:
                out.update(EMPTY_CONDITIONS)
            return out
