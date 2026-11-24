"""The Field Day operating profile's timer (2026-11-12; mockups approved 2026-11-05).

The profile only adds a pill to the status bar; it never changes fetching, ranking or
ageing. Settings hold the operations start and end in UTC. The pill has four states:

  reminder    the profile is on, setup hasn't started yet ("Sat 30 Jan · ops 19:00Z")
  setup       from Setup start, counting down to the start of operations
  operations  counting down to the end
  complete    after the end, until the profile is switched off

Setup start (Dan, 2026-11-22) is a third setting, because the Field Day director decides
when setup begins. Left blank it is local midnight on the day operations begin (the rule
before it existed). Local midnight is the Pi's own time zone, so the service works it out
(with the Pi's daylight-saving rules) and the page only compares the clock with three
times. A Setup start that isn't before Ops start (the settings API refuses one, but a
hand-edited file could hold it) is ignored in favour of local midnight.
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


def local_midnight(ops_start):
    """Local midnight at the start of the (local) day operations begin, as UTC."""
    local = ops_start.astimezone()           # the Pi's zone, with its rules for that date
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc)


def setup_start(ops_start, configured=None):
    """When setup begins: the configured Setup start if it is before Ops start, else
    local midnight on the operations day."""
    if configured is not None and configured < ops_start:
        return configured
    return local_midnight(ops_start)


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
    configured = parse(fd.get("setup_start"))
    out = {"enabled": enabled, "ops_start": None, "ops_end": None, "setup_start": None,
           "setup_start_set": False, "phase": "off" if not enabled else "unset"}
    if start and end and end > start:
        setup = setup_start(start, configured)
        out.update(ops_start=iso(start), ops_end=iso(end), setup_start=iso(setup),
                   setup_start_set=configured is not None and configured < start)
        if enabled:
            out["phase"] = phase(now, start, end, setup)
    return out
