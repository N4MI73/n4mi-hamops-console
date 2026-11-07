"""Settings from the touchscreen (contract section 8): check, save, test connections.

GET returns what the Settings screen may show and change (everything except
`server`, the listening address, which is never changed from the screen).

PUT takes only the fields being changed. Every value is checked first, and
if anything is wrong nothing is saved (the reply lists every problem).
Otherwise the change is written to the settings file on the Pi -- keeping
anything else in the file, such as comments -- by writing a new copy and
swapping it in, so a power cut mid-save can't leave a half-written file.
The running service uses the new values at once: the data sources read
their settings on every request, and a changed grid triggers a fresh NWS
lookup.

Test connections tries each source the console reads and reports ok or
failed with the response time. It can test addresses before they're saved.
"""

import copy
import json
import logging
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import forecast, grid as grid_mod, settings as settings_mod

log = logging.getLogger(__name__)

EDITABLE = ("mode", "station", "display", "sources", "thresholds", "nws_user_agent_contact")

# (low, high) for each numeric threshold; whole numbers unless listed in FLOATS.
THRESHOLD_RANGES = {
    "propagation_stale_min": (5, 1440), "propagation_fetch_min": (1, 60),
    "sentinel_fetch_min": (1, 10), "nws_alerts_stale_min": (2, 120),
    "nws_alerts_unavailable_min": (5, 240), "conditions_stale_min": (1, 120),
    "lightning_stale_min": (1, 60), "station_obs_stale_min": (15, 480),
    "forecast_fetch_min": (10, 240), "forecast_stale_hours": (1, 48),
    "lightning_notify_mi": (1, 30), "lightning_high_mi": (1, 30), "lightning_hold_min": (5, 120),
    "lightning_frequent_per_min": (1, 60), "storms_forecast_hours": (1, 12),
    "prop_bad_k_index": (1, 9), "prop_good_min_bands": (1, 10),
}
FLOATS = {"lightning_notify_mi", "lightning_high_mi"}
XRAY_CLASSES = {"A", "B", "C", "M", "X"}
CALLSIGN = re.compile(r"^[A-Z0-9/]{3,12}$")
HHMM = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


def public_view(settings):
    """The settings the screen may see (everything but `server`)."""
    return {k: copy.deepcopy(settings[k]) for k in EDITABLE if k in settings}


# ---------------------------------------------------------------- checking

def _number(value, low, high, whole=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if whole and value != int(value):
        return False
    return low <= value <= high


def check(changes, current):
    """Check a partial change against the current settings.
    Returns (errors, warnings, cleaned) -- cleaned has callsigns and grids in
    standard case. Nothing is changed here."""
    errors, warnings = [], []
    if not isinstance(changes, dict):
        return ["send a JSON object with the settings to change"], [], {}
    cleaned = copy.deepcopy(changes)
    for key in changes:
        if key not in EDITABLE:
            errors.append(f"'{key}' can't be changed from Settings")

    if "mode" in changes and changes["mode"] not in ("home", "field"):
        errors.append("mode must be 'home' or 'field'")

    station = changes.get("station")
    if station is not None:
        if not isinstance(station, dict):
            errors.append("station must be an object")
        else:
            for which, entry in station.items():
                if which not in ("home", "field"):
                    errors.append(f"station '{which}' isn't home or field")
                    continue
                if not isinstance(entry, dict):
                    errors.append(f"station.{which} must be an object")
                    continue
                for field, value in entry.items():
                    where = f"station.{which}.{field}"
                    if field == "callsign":
                        call = value.strip().upper() if isinstance(value, str) else ""
                        if not CALLSIGN.match(call):
                            errors.append(f"{where}: 3 to 12 letters, digits or '/'")
                        else:
                            cleaned["station"][which][field] = call
                    elif field == "grid_source":
                        if value == "gps":
                            errors.append(f"{where}: GPS grid isn't available yet (Phase 4)")
                        elif value != "manual":
                            errors.append(f"{where} must be 'manual'")
                    elif field == "grid":
                        if not grid_mod.is_valid(value):
                            errors.append(f"{where}: a 4- or 6-character grid such as FM18lv")
                        else:
                            g = value.strip()
                            g = g[:4].upper() + g[4:].lower()
                            cleaned["station"][which][field] = g
                            if len(g) == 4:
                                warnings.append(f"{which.capitalize()} grid: {grid_mod.COARSE_WARNING}")
                    else:
                        errors.append(f"{where} isn't a station setting")

    display = changes.get("display")
    if display is not None:
        if not isinstance(display, dict):
            errors.append("display must be an object")
        else:
            for field, value in display.items():
                where = f"display.{field}"
                if field == "night_style":
                    if value not in ("dim", "red"):
                        errors.append(f"{where} must be 'dim' or 'red'")
                elif field in ("day_brightness_pct", "night_brightness_pct"):
                    if not _number(value, 5, 100):
                        errors.append(f"{where}: a whole number from 5 to 100")
                elif field in ("night_start", "night_end"):
                    if not isinstance(value, str) or not HHMM.match(value):
                        errors.append(f"{where}: a 24-hour time such as 22:00")
                else:
                    errors.append(f"{where} isn't a display setting")

    sources = changes.get("sources")
    if sources is not None:
        if not isinstance(sources, dict):
            errors.append("sources must be an object")
        else:
            for field, value in sources.items():
                where = f"sources.{field}"
                if field not in ("propmon_url", "sentinel_url"):
                    errors.append(f"{where} isn't a source setting")
                elif not isinstance(value, str) or (value and not _url_ok(value)):
                    errors.append(f"{where}: an address such as http://host:8076, or empty")

    thresholds = changes.get("thresholds")
    if thresholds is not None:
        if not isinstance(thresholds, dict):
            errors.append("thresholds must be an object")
        else:
            for field, value in thresholds.items():
                where = f"thresholds.{field}"
                if field == "prop_bad_xray_classes":
                    if (not isinstance(value, list) or not value
                            or not all(isinstance(c, str) and c.upper() in XRAY_CLASSES for c in value)):
                        errors.append(f"{where}: a list of flare classes from A, B, C, M, X")
                    else:
                        cleaned["thresholds"][field] = [c.upper() for c in value]
                elif field in THRESHOLD_RANGES:
                    low, high = THRESHOLD_RANGES[field]
                    if not _number(value, low, high, whole=field not in FLOATS):
                        kind = "a number" if field in FLOATS else "a whole number"
                        errors.append(f"{where}: {kind} from {low} to {high}")
                else:
                    errors.append(f"{where} isn't a threshold setting")
            merged = dict(current["thresholds"], **thresholds)
            if (isinstance(merged.get("lightning_high_mi"), (int, float))
                    and isinstance(merged.get("lightning_notify_mi"), (int, float))
                    and merged["lightning_high_mi"] > merged["lightning_notify_mi"]):
                errors.append("thresholds: lightning_high_mi can't be more than lightning_notify_mi")

    if "nws_user_agent_contact" in changes:
        value = changes["nws_user_agent_contact"]
        if not isinstance(value, str) or len(value) > 100 or "<" in value:
            errors.append("nws_user_agent_contact: an email address or callsign, up to 100 characters")

    return errors, warnings, cleaned


def _url_ok(value):
    try:
        parts = urllib.parse.urlparse(value)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname) and not parts.query


# ---------------------------------------------------------------- saving

class SettingsStore:
    """Owns the live settings dict and the settings file on the Pi."""

    def __init__(self, settings, path=None):
        self.settings = settings
        self.path = path or settings_mod.settings_path()
        self._lock = threading.Lock()

    def view(self):
        with self._lock:
            return public_view(self.settings)

    def update(self, changes):
        """Returns (ok, body). On success the live settings and the file both
        hold the change; on failure neither changes."""
        with self._lock:
            errors, warnings, cleaned = check(changes, self.settings)
            if errors:
                return False, {"errors": errors}
            try:
                self._write(cleaned)
            except OSError as e:
                log.error("could not save settings to %s (%s)", self.path, e)
                return False, {"errors": [f"couldn't save the settings file ({e.__class__.__name__})"]}
            # Change the live dict in place: the data sources hold a reference to it.
            merged = settings_mod._merge(self.settings, cleaned)
            for key in EDITABLE:
                self.settings[key] = merged[key]
            log.info("settings changed: %s", ", ".join(sorted(cleaned)))
            body = public_view(self.settings)
            body["warnings"] = warnings
            return True, body

    def _write(self, cleaned):
        try:
            with open(self.path, encoding="utf-8") as f:
                on_disk = json.load(f)
            if not isinstance(on_disk, dict):
                on_disk = {}
        except FileNotFoundError:
            on_disk = {}
        except ValueError:
            # A broken file: keep it for inspection rather than overwrite it blindly.
            shutil.copy2(self.path, str(self.path) + ".broken")
            on_disk = {}
        new = _merge_keep_comments(on_disk, cleaned)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(new, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        if self.path.exists():
            shutil.copymode(self.path, tmp)
        os.replace(tmp, self.path)


def _merge_keep_comments(base, override):
    """Like settings._merge, but keeps keys starting with '_' (comments)."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge_keep_comments(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


# ---------------------------------------------------------------- test connections

def _try(name, url, headers, timeout):
    start = time.monotonic()
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            json.load(reply)
        ok, message = True, None
    except urllib.error.HTTPError as e:
        if e.code == 503:
            # PropMon v2 answers 503 `no_data_yet` for a short while after a restart.
            ok, message = True, "answering, but no data yet (just restarted?)"
        else:
            ok, message = False, f"answered with an error ({e.code})"
    except (urllib.error.URLError, OSError) as e:
        ok, message = False, f"not answering ({e.__class__.__name__})"
    except ValueError:
        ok, message = False, "answered, but not with JSON"
    return {"source": name, "url": url, "ok": ok,
            "ms": int((time.monotonic() - start) * 1000), "message": message}


def test_connections(settings, sources=None, fetch=_try, timeout=5):
    """Try PropMon, Weather Sentinel and NWS. `sources` (optional) tests
    addresses that haven't been saved yet."""
    use = dict(settings["sources"], **(sources or {}))
    contact = settings.get("nws_user_agent_contact")
    json_headers = {"Accept": "application/json", "User-Agent": "HamOpsConsole"}
    plan = [("PropMon", use.get("propmon_url"), "/api/v2/conditions", json_headers),
            ("Weather Sentinel", use.get("sentinel_url"), "/api/conditions", json_headers),
            # NWS's home page is HTML, not JSON (found on the Pi, 2026-11-03), so test a
            # small JSON endpoint instead: the national count of active alerts.
            ("NWS", forecast.API, "/alerts/active/count", {"Accept": "application/geo+json",
                                                         "User-Agent": forecast.user_agent(contact)})]
    results = []
    for name, base, path, headers in plan:
        if not base:
            results.append({"source": name, "url": None, "ok": False, "ms": None,
                            "message": "address not set"})
        elif not _url_ok(base):
            results.append({"source": name, "url": base, "ok": False, "ms": None,
                            "message": "not a valid address"})
        else:
            results.append(fetch(name, base.rstrip("/") + path, headers, timeout))
    return results
