"""Console settings: read once at start-up from a JSON file outside the repository.

Where the file is looked for, in order:
  1. the path in the HAMOPS_SETTINGS environment variable, if set;
  2. ~/.config/hamops-console/settings.json (the normal place on the Pi).

Anything missing from the file falls back to the defaults below, so a short
settings file (or none at all, on a development machine) still works. The
defaults match config/settings.example.json in the repository.
"""

import copy
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_PATH = Path.home() / ".config" / "hamops-console" / "settings.json"

DEFAULTS = {
    "mode": "home",
    "station": {
        "home": {"callsign": "N0CALL", "grid_source": "manual", "grid": "AA00"},
        "field": {"callsign": "N0CALL", "grid_source": "manual", "grid": "AA00aa"},
    },
    "display": {
        "main_clock": "utc",          # "utc" or "local": shown large and first everywhere (2026-11-11)
        "night_style": "dim",
        "day_brightness_pct": 80,
        "night_brightness_pct": 20,
        "night_start": "22:00",
        "night_end": "07:00",
    },
    "sources": {
        "propmon_url": "",
        "sentinel_url": "",
    },
    "thresholds": {
        "propagation_stale_min": 15,
        "propagation_fetch_min": 5,
        "sentinel_fetch_min": 1,
        "nws_alerts_stale_min": 10,
        "nws_alerts_unavailable_min": 30,
        "conditions_stale_min": 5,
        "lightning_stale_min": 5,
        "station_obs_stale_min": 90,
        "forecast_fetch_min": 30,
        "forecast_stale_hours": 12,
        "lightning_notify_mi": 10,
        "lightning_high_mi": 5,
        "lightning_hold_min": 30,
        "lightning_frequent_per_min": 5,
        "storms_forecast_hours": 6,
        "prop_bad_k_index": 5,
        "prop_bad_xray_classes": ["M", "X"],
        "prop_good_min_bands": 2,
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8090,
    },
    "nws_user_agent_contact": "",
}


def _merge(base, override):
    """Recursively overlay `override` onto a copy of `base` (dicts only)."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if key.startswith("_"):
            continue  # comments such as "_comment"
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def settings_path():
    env = os.environ.get("HAMOPS_SETTINGS")
    return Path(env) if env else DEFAULT_PATH


def load(path=None):
    """Return the settings as a dict. Never raises: a missing or unreadable
    file gives the defaults, with a warning in the log."""
    path = Path(path) if path else settings_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        log.info("settings loaded from %s", path)
    except FileNotFoundError:
        log.warning("no settings file at %s; using defaults", path)
        data = {}
    except (OSError, ValueError) as e:
        log.error("could not read settings file %s (%s); using defaults", path, e)
        data = {}
    if not isinstance(data, dict):
        log.error("settings file %s is not a JSON object; using defaults", path)
        data = {}
    settings = _merge(DEFAULTS, data)
    if settings.get("mode") not in ("home", "field"):
        log.error("unknown mode %r in settings; using 'home'", settings.get("mode"))
        settings["mode"] = "home"
    return settings
