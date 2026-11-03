"""The ranked alert list behind the Priority Card and the Alerts page (contract section 6).

Built from the other blocks, so the ranking lives in this one tested place
rather than in screen code (the Pi version of the ESP32 series'
find_worst_alert() lesson). Order approved 2026-10-12, extended by Dan
2026-11-01:

  card tier (Priority Card, < > through them)
     1 hazard_warning            NWS warning, site hazard             red     critical
     2 lightning_5mi             strike inside 5 mi (30-min hold)     red     critical, once per hold
     3 alerts_unavailable_storms NWS alerts not current + storms forecast within 6 h   red
     4 hazard_alert              NWS watch/advisory/statement, site hazard             yellow
     5 lightning_likely_soon     lightning likely within 2 h          yellow
     6 lightning_frequent        > 5 strikes a minute inside 10 mi    yellow
     7 lightning_10mi            strike inside 10 mi                  yellow
     8 lightning_outlook         lightning possible, or likely later  yellow
     9 source_down               a source stale, unavailable or in test mode           yellow
    10 propagation               notable propagation                  green (good) / yellow (bad)
    11 all_quiet                 nothing else on the card             green
  badge tier (counted on the Alerts button)
     - temperature_alert         heat/cold product                    yellow
     - other_warning             other NWS warning                    blue
     - other_notice              other NWS advisory or statement      blue

One detected-lightning item at a time (5 mi beats frequent beats 10 mi), and
one forecast-lightning item; one of each can show together. Within a
category the newest comes first. The list is never empty.

Acknowledge is the console's own, stored on the Pi by AckStore, keyed by
`ref.id`, and never sent to Weather Sentinel.
"""

import json
import logging
import os
import threading
from datetime import timedelta
from pathlib import Path

from . import freshness

log = logging.getLogger(__name__)

LIKELY_SOON_HOURS = 2
ACK_KEEP_HOURS = 48

ORDER = ["hazard_warning", "lightning_5mi", "alerts_unavailable_storms", "hazard_alert",
         "lightning_likely_soon", "lightning_frequent", "lightning_10mi", "lightning_outlook",
         "source_down", "propagation", "all_quiet",
         "temperature_alert", "other_warning", "other_notice"]
BADGE = {"temperature_alert", "other_warning", "other_notice"}
CRITICAL = {"hazard_warning", "lightning_5mi"}


# ---------------------------------------------------------------- wording helpers

def clock_text(dt):
    """'10:30 AM (15:30 UTC)': the Pi's local time first, UTC beside it."""
    local = dt.astimezone()
    return f"{local.strftime('%I:%M %p').lstrip('0')} ({dt.strftime('%H:%M')} UTC)"


def _until(iso_text):
    dt = freshness.parse_time(iso_text)
    return f"Until {clock_text(dt)}." if dt else ""


def _join(*parts):
    return " ".join(p for p in parts if p)


def _band_list(names):
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _item(category, title, detail, since, block, id_, color):
    return {"category": category, "tier": "badge" if category in BADGE else "card",
            "color": color, "critical": category in CRITICAL, "acknowledged": False,
            "title": title, "detail": detail or None, "since": since,
            "ref": {"block": block, "id": id_}}


# ---------------------------------------------------------------- the pieces

def _nws_items(alerts):
    out = []
    if alerts.get("status") not in (freshness.OK, freshness.STALE):
        return out
    for a in alerts.get("items") or []:
        kind, cls = a.get("kind"), a.get("hazard_class")
        if cls == "site_hazard":
            category, color = ("hazard_warning", "red") if kind == "warning" else ("hazard_alert", "yellow")
        elif cls == "temperature":
            category, color = "temperature_alert", "yellow"
        else:
            category, color = ("other_warning", "blue") if kind == "warning" else ("other_notice", "blue")
        detail = _join(a.get("hazard") or a.get("headline"), _until(a.get("expires")))
        out.append(_item(category, a.get("event"), detail, a.get("onset"), "weather.alerts",
                         a.get("id") or f"nws-{a.get('event')}-{a.get('expires')}", color))
    return out


def _detected_lightning(lightning):
    if not lightning or lightning.get("status") != freshness.OK:
        return []
    closest = lightning.get("closest_mi")
    dist = f"{closest:g} mi away" if isinstance(closest, (int, float)) else "nearby"
    count = lightning.get("strikes_recent")
    window = lightning.get("window_min")
    strikes = f"{count} strikes in {window} min." if count is not None and window else ""
    if lightning.get("within_5mi"):
        until = freshness.parse_time(lightning.get("hold_until_5mi"))
        stay = f"Stay in until {clock_text(until)}." if until else ""
        since = lightning.get("hold_since_5mi")
        return [_item("lightning_5mi", f"Lightning {dist}", _join(strikes, stay), since,
                      "weather.lightning", f"lightning-5mi-{since}", "red")]
    if lightning.get("frequent"):
        return [_item("lightning_frequent", "Frequent lightning inside 10 mi",
                      _join(f"Closest {dist}." if closest is not None else "", strikes),
                      lightning.get("last_strike_at"), "weather.lightning", "lightning-frequent", "yellow")]
    if lightning.get("within_10mi"):
        until = freshness.parse_time(lightning.get("hold_until_10mi"))
        wait = f"Wait until {clock_text(until)}." if until else ""
        return [_item("lightning_10mi", f"Lightning {dist}", _join(strikes, wait),
                      lightning.get("last_strike_at"), "weather.lightning", "lightning-10mi", "yellow")]
    return []


def _forecast_lightning(forecast, now):
    outlook = (forecast or {}).get("lightning_outlook")
    if not outlook:
        return []
    start = freshness.parse_time(outlook.get("start"))
    basis = "NWS alert" if outlook.get("basis") == "nws_alert" else "NWS forecast"
    level = outlook.get("level")
    when = "now" if start is None or start <= now else f"after {clock_text(start)}"
    soon = level == "likely" and (start is None or start - now <= timedelta(hours=LIKELY_SOON_HOURS))
    category = "lightning_likely_soon" if soon else "lightning_outlook"
    return [_item(category, f"Lightning {level} {when}", f"{basis}, not detected.",
                  outlook.get("start"), "weather.forecast", f"lightning-outlook-{level}", "yellow")]


def _alerts_unavailable_storms(alerts, forecast, now, hours):
    if alerts.get("status") not in (freshness.UNAVAILABLE, freshness.STALE):
        return []
    outlook = (forecast or {}).get("lightning_outlook")
    start = freshness.parse_time((outlook or {}).get("start"))
    if not outlook or (start is not None and start - now > timedelta(hours=hours)):
        return []
    return [_item("alerts_unavailable_storms", "NWS alerts unavailable — storms forecast",
                  "This is not the same as no alerts. Tune NOAA Weather Radio.",
                  None, "weather.alerts", "alerts-unavailable-storms", "red")]


def _source_down(blocks):
    """One item per source that is stale, unavailable or in test mode."""
    by_source = {}
    for name, block in blocks:
        if not block or block.get("status") == freshness.OK:
            continue
        by_source.setdefault(block.get("source") or name, (name, block))
    out = []
    for source, (name, block) in by_source.items():
        if block["status"] == freshness.TEST:
            title = "Home weather station in test mode"
        elif block["status"] == freshness.STALE:
            title = f"{source} data is stale"
        else:
            title = f"{source} unavailable"
        out.append(_item("source_down", title, block.get("message"), block.get("fetched_at"),
                         name, f"source-{source}", "yellow"))
    return out


def _propagation(prop, t):
    if not prop or prop.get("status") != freshness.OK:
        return [], []
    good = [b["band"] for b in prop.get("bands") or [] if b.get("rating") == "good"]
    k, xray = prop.get("k_index"), prop.get("xray") or ""
    bad = []
    if isinstance(k, (int, float)) and k >= t["prop_bad_k_index"]:
        bad.append(f"K-index {k:g}: geomagnetic storm.")
    if xray[:1].upper() in t["prop_bad_xray_classes"]:
        bad.append(f"{xray} X-ray flare.")
    if bad:
        return [_item("propagation", "Propagation disturbed", " ".join(bad), prop.get("issued_at"),
                      "propagation", "propagation-bad", "yellow")], good
    if len(good) >= t["prop_good_min_bands"]:
        return [_item("propagation", f"{_band_list(good)} are open", prop.get("summary"),
                      prop.get("issued_at"), "propagation", "propagation-good", "green")], good
    return [], good


# ---------------------------------------------------------------- the list

def build(propagation, weather, settings, now, acks=None):
    """The ranked items for GET /api/alerts."""
    t = settings["thresholds"]
    weather = weather or {}
    alerts = weather.get("alerts") or {}
    forecast = weather.get("forecast")
    items = []
    items += _nws_items(alerts)
    items += _detected_lightning(weather.get("lightning"))
    items += _forecast_lightning(forecast, now)
    items += _alerts_unavailable_storms(alerts, forecast, now, t["storms_forecast_hours"])
    items += _source_down([("propagation", propagation), ("weather.alerts", alerts),
                           ("weather.conditions", weather.get("conditions")),
                           ("weather.lightning", weather.get("lightning")),
                           ("weather.forecast", forecast)])
    prop_items, good = _propagation(propagation, t)
    items += prop_items
    if not any(i["tier"] == "card" for i in items):
        detail = (f"{_band_list(good)} {'is' if len(good) == 1 else 'are'} open." if good else None)
        items.append(_item("all_quiet", "All quiet.", detail, None, None, "all-quiet", "green"))

    # Category order, then newest first within a category (no time sorts last).
    items.sort(key=lambda i: i["since"] or "", reverse=True)
    items.sort(key=lambda i: ORDER.index(i["category"]))
    acks = acks or {}
    for rank, item in enumerate(items, start=1):
        item["rank"] = rank
        item["acknowledged"] = item["ref"]["id"] in acks
    return items


class AckStore:
    """The console's own Acknowledge, kept in a small JSON file on the Pi so a
    restart doesn't make an acknowledged warning flash again. Entries older
    than ACK_KEEP_HOURS are dropped. A missing or broken file is treated as
    empty and never stops the service."""

    def __init__(self, path=None, clock=freshness.utcnow):
        default = Path.home() / ".local" / "state" / "hamops-console" / "acks.json"
        self.path = Path(path or os.environ.get("HAMOPS_STATE_ACKS") or default)
        self._clock = clock
        self._lock = threading.Lock()
        self._acks = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, AttributeError) as e:
            log.error("could not read %s (%s); starting with no acknowledgements", self.path, e)
            return {}

    def _prune(self, now):
        cutoff = now - timedelta(hours=ACK_KEEP_HOURS)
        self._acks = {k: v for k, v in self._acks.items()
                      if (freshness.parse_time(v) or now) >= cutoff}

    def current(self):
        with self._lock:
            self._prune(self._clock())
            return dict(self._acks)

    def add(self, alert_id):
        with self._lock:
            now = self._clock()
            self._prune(now)
            self._acks[alert_id] = freshness.iso(now)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._acks, f, indent=1)
                os.replace(tmp, self.path)
            except OSError as e:
                log.error("could not save acknowledgement to %s (%s); kept in memory", self.path, e)
