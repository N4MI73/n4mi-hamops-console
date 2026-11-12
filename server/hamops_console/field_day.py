"""The Field Day operating profile's timer (2026-11-12; mockups approved 2026-11-05).

The profile only adds a pill to the status bar; it never changes fetching, ranking or
ageing. Settings hold the operations start and end in UTC. The pill has four states:

  reminder    the profile is on, operations are on a later day ("Sat 30 Jan · ops 19:00Z")
  setup       from local midnight on the day operations begin, counting down to the start
  operations  counting down to the end
  complete    after the end, until the profile is switched off

Local midnight is the Pi's own time zone, so the service works it out (with the Pi's
daylight-saving rules) and the page only compares the clock with three times.
"""

from datetime import datetime, timezone

PROFILES = ("general_hf", "field_day")
FORMAT = "%Y-%m-%dT%H:%MZ"          # how the settings store the times: 2027-01-30T19:00Z


def parse(text):
    """"2027-01-30T19:00Z" -> an aware UTC datetime, or None if empty or malformed."""
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.strptime(text, FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def setup_start(ops_start):
    """Local midnight at the start of the (local) day operations begin, as UTC."""
    local = ops_start.astimezone()           # the Pi's zone, with its rules for that date
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc)


def phase(now, start, end, setup):
    if now < setup:
        return "reminder"
    if now < start:
        return "setup"
    if now < end:
        return "operations"
    return "complete"


def block(settings, now=None):
    """The `field_day` block in /api/system."""
    now = now or datetime.now(timezone.utc)
    enabled = settings.get("profile") == "field_day"
    fd = settings.get("field_day") or {}
    start, end = parse(fd.get("ops_start")), parse(fd.get("ops_end"))
    out = {"enabled": enabled, "ops_start": None, "ops_end": None, "setup_start": None,
           "phase": "off" if not enabled else "unset"}
    if start and end and end > start:
        setup = setup_start(start)
        out.update(ops_start=iso(start), ops_end=iso(end), setup_start=iso(setup))
        if enabled:
            out["phase"] = phase(now, start, end, setup)
    return out
