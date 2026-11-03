"""The one place that decides whether a block of data is fresh, stale or unavailable.

Every data block the dashboard receives carries the same freshness fields
(contract section 2): status, source, fetched_at, issued_at, age_sec, message.
The dashboard turns `status` into colour and wording with one shared function;
it never works out staleness itself. This mirrors the ESP32 series'
ui_staleness_color() lesson: one rule, used everywhere.
"""

from datetime import datetime, timezone

OK = "ok"
STALE = "stale"
UNAVAILABLE = "unavailable"
TEST = "test"


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    """ISO 8601 in UTC with a Z, to the second, or None."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value):
    """Parse an ISO 8601 time from a source ("2026-11-02T20:11:06Z",
    "...+00:00", with or without fractions). Returns an aware datetime or
    None; never raises on bad input."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def block(source, fetched_at, issued_at, stale_after_sec, error=None, now=None, test=False,
          force_stale=False, unavailable_after_sec=None):
    """Build the freshness fields for one data block.

    source           short name shown to the user, e.g. "PropMon"
    fetched_at       when the service last got a GOOD response (datetime or None)
    issued_at        the source's own time for the data (datetime or None)
    stale_after_sec  age beyond which the data counts as stale
    error            short reason the last attempt failed, or None
    test             True while the source is in a test/simulation mode
    force_stale      True when the source itself says its data isn't current
                     (e.g. Weather Sentinel's `available: false`)
    unavailable_after_sec
                     age beyond which old data is no longer shown at all
                     (NWS alerts: 30 min), or None for never

    Rules, in order:
      - never had good data                     -> unavailable
      - older than unavailable_after_sec        -> unavailable
      - source in test mode                     -> test
      - older than the threshold, or forced     -> stale (last good data still shown)
      - otherwise                               -> ok
    Age is measured from issued_at when the source gives one, else fetched_at.
    The caller must not show a block's data when its status is unavailable.
    """
    now = now or utcnow()
    if fetched_at is None:
        return {
            "status": UNAVAILABLE, "source": source,
            "fetched_at": None, "issued_at": iso(issued_at), "age_sec": None,
            "message": error or f"No data from {source} yet",
        }
    reference = issued_at or fetched_at
    age = max(0, int((now - reference).total_seconds()))
    if unavailable_after_sec is not None and age > unavailable_after_sec:
        status = UNAVAILABLE
        message = error or f"No current data from {source} for {age // 60} min"
    elif test:
        status, message = TEST, f"{source} is in test mode"
    elif force_stale:
        status, message = STALE, error or f"{source} says its data isn't current"
    elif age > stale_after_sec:
        status = STALE
        minutes = age // 60
        message = error or f"No fresh data from {source} for {minutes} min"
    else:
        status, message = OK, None
    return {
        "status": status, "source": source,
        "fetched_at": iso(fetched_at), "issued_at": iso(issued_at), "age_sec": age,
        "message": message,
    }
