"""Field Mode lightning from the GOES satellite (GLM), switched on by hand (step 2, 2026-11-20).

What it does
  A background loop lists NOAA's public GLM bucket for new 20-second files, downloads
  them (every file on Full, one a minute on Economy), keeps the flashes near the
  operating site, and builds the Field `lightning` block with the 20/10/5-mile tiers.
  glm.py does the reading and the geometry; this module does the fetching, the
  switch and the block.

On and off (Dan, 2026-11-12 and 2026-11-20)
  - Off by default. Turned on by hand: the Turn on button on the forecast card, the
    Weather page or Settings (POST /api/lightning).
  - Once an NWS lightning outlook has been seen while it is on, it stays on until the
    outlook clears, then turns itself off.
  - Turned on with no outlook: it stays on until turned off, for at most
    `glm_auto_off_hours` (6). After `glm_ask_first_min` (30) it asks "keep it on?";
    after Keep on it asks once more at `glm_ask_again_hours` (3). No answer means it
    stays on. An outlook arriving first switches to the outlook rule.
  - **An automatic turn-off waits while lightning is within the outer tier** (a flash
    inside 20 mi in the last 30 minutes), so the console never switches its own
    lightning warning off; it turns off once that hold clears.
  - Turned off by hand with flashes inside 20 mi in the last 30 minutes: the block
    carries `recent_at_off`, which the alert list turns into an amber card ("not an
    all-clear"), until it is dismissed, detection is turned back on, or 30 minutes
    pass after the last flash.
  - The switch, the question's answers, the recent flashes and today's data count are
    saved on the Pi (field_lightning.json), so a restart changes nothing.

Never an all-clear by accident
  While detection is off, or on with no current satellite data, a tier with no flash
  seen is null (unknown), never false. Flashes already seen keep their holds whatever
  the freshness (just turned on, after a restart, files late). Late data (no new file
  for `glm_stale_min`) makes the block stale, and the alert list adds a "lightning data
  late" card ("Not the same as no lightning"). A damaged file is skipped, not retried
  forever; a grid change re-measures the kept flashes rather than dropping them.

At-home test (Dan, 2026-11-20)
  `home_test` runs the same loop in Home Mode against the home grid, to compare with
  the home Tempest. Its results go only to the System page (`system_block`), never
  into the alert list. Switched on and off with POST /api/lightning (an SSH curl
  command for now).
"""

import collections
import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import freshness, glm, grid as grid_mod, location as location_mod

log = logging.getLogger(__name__)

SOURCE = "GOES satellite"
SATELLITES = {"east": "GOES-East", "west": "GOES-West"}
LEVELS = ("full", "economy")
POLL_SEC = {"full": 20, "economy": 60}
IDLE_SEC = 5                  # how often an idle loop looks again (a turn-on wakes it at once)
START_GRACE_SEC = 120         # no "data late" card for the first two minutes after turning on
RESUME_MAX_MIN = 5            # after a restart, catch up on missed files only this far back
MAX_FILES_PER_POLL = 15       # five minutes of Full files; anything older is skipped
FREQUENT_WINDOW_SEC = 60
RECENT_WINDOW_MIN = 10        # "6 flashes in the last 10 min"
SITE_MOVED_MI = 1.0           # flashes are about one site; a new site starts them afresh
LIST_TIMEOUT_SEC = 10
FILE_TIMEOUT_SEC = 20
MAX_BYTES = 20_000_000        # a GLM file is about 0.3 MB (a few MB in a big storm); a listing far less
ECONOMY_STALE_EXTRA_SEC = 60  # Economy reads a file a minute, so its newest file is up to a minute older
USER_AGENT = "HamOpsConsole"


class TooBig(ValueError):
    pass


def fetch_bytes(url, timeout):
    """GET a URL -> bytes, at most MAX_BYTES. Raises urllib/OS errors, or TooBig."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        data = reply.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise TooBig(f"reply over {MAX_BYTES} bytes")
    return data


def reader_available():
    try:
        import netCDF4  # noqa: F401
        return True
    except ImportError:
        return False


def _describe(e):
    if isinstance(e, urllib.error.HTTPError):
        return f"{SOURCE} answered with an error ({e.code})"
    if isinstance(e, (urllib.error.URLError, OSError)):
        return f"{SOURCE} not answering ({e.__class__.__name__})"
    return f"{SOURCE} sent something unreadable ({e.__class__.__name__})"


class FieldLightning:
    """The switch, the fetching loop and the blocks. Thread-safe."""

    def __init__(self, settings, fetch=fetch_bytes, read=glm.read_flashes, clock=freshness.utcnow,
                 state_path=None, reader_ok=None):
        self.settings = settings
        self._fetch = fetch
        self._read = read
        self._clock = clock
        self._reader_ok = reader_available if reader_ok is None else reader_ok
        default = Path.home() / ".local" / "state" / "hamops-console" / "field_lightning.json"
        self.path = Path(state_path or os.environ.get("HAMOPS_STATE_FIELD_LIGHTNING") or default)
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread = None
        # saved
        self.on = False
        self.turned_on_at = None
        self.outlook_seen = False
        self.answered = 0             # 0 none, 1 the 30-minute question, 2 the 3-hour question
        self.turned_off_at = None
        self.off_reason = None        # "operator", "outlook_cleared", "time_limit"
        self.recent_dismissed_at = None
        self.cleared_at = None        # the operator's Clear hold
        self.home_test = False
        self.home_test_since = None
        self.site = None              # (lat, lon) the flashes are about
        self.flashes = []             # [{"time", "distance_mi", "bearing"}] within the watch radius
        self.last_key = None
        self.last_key_satellite = None        # last_key is only meaningful for its own satellite
        self.count_day = None
        self.bytes_today = 0
        self.files_today = 0
        # not saved
        self._hour = collections.deque()     # (time, bytes) over the last hour
        self._newest_file_at = None          # start time of the newest file read
        self._last_ok_at = None              # last poll that worked end to end
        self._error = None
        self._started_at = None              # when this run of the loop became active
        self._deferred = None                # an automatic turn-off waiting for a hold to clear
        self._saved_at = None
        self._load()
        if self.on or self.home_test:
            self._started_at = clock()        # after a restart: the same short grace as a turn-on

    # ---------------------------------------------------------------- settings
    def _t(self, key):
        return self.settings["thresholds"][key]

    def _cfg(self, key):
        return (self.settings.get("field_lightning") or {}).get(key)

    def level(self):
        level = self._cfg("level")
        return level if level in LEVELS else "full"

    def satellite(self):
        sat = self._cfg("satellite")
        return sat if sat in SATELLITES else "east"

    def _bucket_url(self):
        return (self.settings["sources"].get(f"glm_{self.satellite()}_url") or "").rstrip("/")

    def _tiers(self):
        return (self._t("lightning_high_mi"), self._t("lightning_notify_mi"), self._t("lightning_field_outer_mi"))

    def _hold(self):
        return timedelta(minutes=self._t("lightning_hold_min"))

    # ---------------------------------------------------------------- saving
    _TIMES = ("turned_on_at", "turned_off_at", "recent_dismissed_at", "cleared_at", "home_test_since")

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            self._apply(data)
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as e:
            log.error("could not read %s (%s); lightning detection starts off", self.path, e)
            self._reset_state()

    def _reset_state(self):
        self.on = self.outlook_seen = self.home_test = False
        self.answered = 0
        for name in self._TIMES:
            setattr(self, name, None)
        self.off_reason = self.site = self.last_key = self.last_key_satellite = self.count_day = None
        self.flashes = []
        self.bytes_today = self.files_today = 0

    @staticmethod
    def _num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    def _apply(self, data):
        self.on = data.get("on") is True
        self.outlook_seen = data.get("outlook_seen") is True
        self.answered = data.get("answered") if data.get("answered") in (0, 1, 2) else 0
        self.off_reason = data.get("off_reason") if isinstance(data.get("off_reason"), str) else None
        self.home_test = data.get("home_test") is True
        for name in self._TIMES:
            setattr(self, name, freshness.parse_time(data.get(name)))
        if self.on and self.turned_on_at is None:
            self.on = False             # a damaged file: no turn-on time, so no limits to apply
        site = data.get("site")
        if (isinstance(site, list) and len(site) == 2
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in site)):
            self.site = (float(site[0]), float(site[1]))
        flashes = data.get("flashes")
        for f in flashes if isinstance(flashes, list) else []:
            if not isinstance(f, dict):
                continue
            t, seen = freshness.parse_time(f.get("time")), freshness.parse_time(f.get("seen"))
            d, la, lo = f.get("distance_mi"), f.get("lat"), f.get("lon")
            if t and self._num(d) and isinstance(f.get("bearing"), str):
                item = {"time": t, "seen": seen or t, "distance_mi": float(d), "bearing": f["bearing"]}
                if self._num(la) and self._num(lo):
                    item.update(lat=float(la), lon=float(lo))
                self.flashes.append(item)
        self.last_key = data.get("last_key") if isinstance(data.get("last_key"), str) else None
        sat = data.get("last_key_satellite")
        self.last_key_satellite = sat if sat in SATELLITES else None
        self.count_day = data.get("count_day") if isinstance(data.get("count_day"), str) else None
        for name in ("bytes_today", "files_today"):
            v = data.get(name)
            setattr(self, name, v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0)

    def _save(self, now=None):
        data = {name: freshness.iso(getattr(self, name)) for name in self._TIMES}
        data.update({
            "on": self.on, "outlook_seen": self.outlook_seen, "answered": self.answered,
            "off_reason": self.off_reason, "home_test": self.home_test,
            "site": list(self.site) if self.site else None,
            "flashes": [dict({"time": freshness.iso(f["time"]), "seen": freshness.iso(f.get("seen") or f["time"]),
                              "distance_mi": round(f["distance_mi"], 2), "bearing": f["bearing"]},
                             **({"lat": round(f["lat"], 4), "lon": round(f["lon"], 4)} if "lat" in f else {}))
                        for f in self.flashes],
            "last_key": self.last_key, "last_key_satellite": self.last_key_satellite,
            "count_day": self.count_day,
            "bytes_today": self.bytes_today, "files_today": self.files_today,
        })
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, self.path)
            self._saved_at = now or self._clock()
        except OSError as e:
            log.error("could not save lightning detection state to %s (%s); kept in memory", self.path, e)

    # ---------------------------------------------------------------- where, and what is active
    def _site_for(self, mode):
        grid = location_mod.station_grid(self.settings, mode)
        if not grid_mod.is_valid(grid):
            return None
        return grid_mod.centre(grid)

    def _active_mode(self):
        """'field' when Field Mode detection is on, 'home_test' for the at-home test, else None."""
        mode = self.settings["mode"]
        if mode == "field" and self.on:
            return "field"
        if mode == "home" and self.home_test:
            return "home_test"
        return None

    def _within(self, now, miles, since=None):
        """Flashes inside `miles` in the last hold period (and after the operator's Clear)."""
        hold = self._hold()
        return [f for f in self.flashes
                if f["distance_mi"] <= miles and now - f["time"] <= hold
                and (self.cleared_at is None or f.get("seen", f["time"]) > self.cleared_at)
                and (since is None or f["time"] > since)]

    def _prune(self, now):
        keep = max(self._hold(), timedelta(minutes=RECENT_WINDOW_MIN))
        self.flashes = [f for f in self.flashes if now - f["time"] <= keep]

    def _move_to(self, site):
        """A new site: measure the kept flashes from it, so a hold survives a grid change
        (a GPS grid moving to the next subsquare, say). Flashes saved without a position
        (none should be) can't be re-measured and are dropped."""
        watch = self._t("lightning_field_outer_mi") + 5
        kept = []
        for f in self.flashes:
            if "lat" not in f:
                continue
            d = glm.distance_mi(site[0], site[1], f["lat"], f["lon"])
            if d <= watch:
                kept.append(dict(f, distance_mi=d, bearing=glm.compass(glm.bearing_deg(site[0], site[1], f["lat"], f["lon"]))))
        self.site, self.flashes = site, kept

    def _hold_active(self, now):
        return bool(self._within(now, self._t("lightning_field_outer_mi")))

    # ---------------------------------------------------------------- the switch
    def turn_on(self):
        with self._lock:
            now = self._clock()
            if not self.on:
                self.on = True
                self.turned_on_at = now
                self.outlook_seen = False
                self.answered = 0
                self._begin_run(now)
            self.turned_off_at = self.off_reason = None   # also ends the "turned off" card
            self._deferred = None
            self._save(now)
            log.info("lightning detection turned on (%s, %s)", self.level(), SATELLITES[self.satellite()])
        self._wake.set()
        return self.detection()

    def turn_off(self, reason="operator"):
        with self._lock:
            now = self._clock()
            if self.on:
                self.on = False
                self.turned_off_at = now
                self.off_reason = reason
                self.answered = 0
                self.recent_dismissed_at = None
                self._deferred = None
                self._save(now)
                log.info("lightning detection turned off (%s)", reason)
            return self.detection()

    def keep_on(self):
        """The operator's Keep on. False when no question is waiting."""
        with self._lock:
            q = self._question(self._clock())
            if q is None:
                return False
            self.answered = 2 if q["stage"] == "3h" else 1
            self._save()
            return True

    def dismiss_recent(self):
        """Dismiss the "turned off with lightning recent" card. False when there is none."""
        with self._lock:
            now = self._clock()
            if self._recent_at_off(now) is None:
                return False
            self.recent_dismissed_at = now
            self._save(now)
            return True

    def clear_hold(self):
        """The operator's Clear (as at home): ends the holds until the next flash."""
        with self._lock:
            now = self._clock()
            self.cleared_at = now
            self._save(now)
            log.info("field lightning hold cleared by the operator")
            return now

    def set_home_test(self, on):
        with self._lock:
            now = self._clock()
            if on and not self.home_test:
                self.home_test, self.home_test_since = True, now
                self._begin_run(now)
            elif not on and self.home_test:
                self.home_test, self.home_test_since = False, None
            self._save(now)
            log.info("GOES lightning home test %s", "on" if on else "off")
        self._wake.set()
        return self.system_block()

    def _begin_run(self, now):
        self._newest_file_at = self._last_ok_at = None
        self._error = None
        self._started_at = now

    # ---------------------------------------------------------------- the rules
    def _question(self, now):
        if not self.on or self.outlook_seen or self.turned_on_at is None:
            return None
        elapsed = now - self.turned_on_at
        first = timedelta(minutes=self._t("glm_ask_first_min"))
        again = timedelta(hours=self._t("glm_ask_again_hours"))
        if elapsed >= again and self.answered < 2:
            return {"stage": "3h", "asked_at": freshness.iso(self.turned_on_at + again)}
        if elapsed >= first and self.answered < 1:
            return {"stage": "30min", "asked_at": freshness.iso(self.turned_on_at + first)}
        return None

    def _auto_off_at(self):
        if not self.on or self.outlook_seen or self.turned_on_at is None:
            return None
        return self.turned_on_at + timedelta(hours=self._t("glm_auto_off_hours"))

    def _automatic_off(self, now, reason):
        """Turn off on a rule, unless lightning is within the outer tier: then wait."""
        if self._hold_active(now):
            if self._deferred != reason:
                log.info("lightning detection would turn off (%s) but lightning is near; waiting", reason)
            self._deferred = reason
            return
        self.on = False
        self.turned_off_at = now
        self.off_reason = reason
        self.answered = 0
        self._deferred = None
        self._save(now)
        log.info("lightning detection turned itself off (%s)", reason)

    def _check_time_limit(self, now):
        until = self._auto_off_at()
        if until is not None and now >= until:
            self._automatic_off(now, "time_limit")
        elif self._deferred == "time_limit":
            self._deferred = None

    def observe(self, forecast, alerts_status=None):
        """Apply the outlook rule; called with the forecast block (after storm alerts are
        applied) and the NWS alerts block's status each time the weather blocks are built.
        The outlook counts as cleared only when the forecast is current (ok) and the NWS
        alerts are ok or stale: a storm outlook from a Severe Thunderstorm Watch vanishes
        from the forecast when the alerts can't be read, and that is not a clearing.
        Anything less says nothing, and never turns detection off."""
        with self._lock:
            now = self._clock()
            if not self.on or self.settings["mode"] != "field":
                return
            forecast = forecast or {}
            status = forecast.get("status")
            outlook = forecast.get("lightning_outlook")
            if outlook and status in (freshness.OK, freshness.STALE):
                if not self.outlook_seen:
                    self.outlook_seen = True
                    self._save(now)
                if self._deferred == "outlook_cleared":
                    self._deferred = None
            elif (self.outlook_seen and status == freshness.OK and not outlook
                  and alerts_status in (freshness.OK, freshness.STALE)):
                self._automatic_off(now, "outlook_cleared")
            self._check_time_limit(now)

    # ---------------------------------------------------------------- fetching
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="glm", daemon=True)
            self._thread.start()

    def _run(self):
        while True:
            try:
                wait = self.poll_once()
            except Exception:                         # never let the loop die
                log.exception("lightning detection loop error")
                wait = POLL_SEC["full"]
            self._wake.wait(wait)
            self._wake.clear()

    def poll_once(self):
        """One pass: check the limits, then fetch any new files. Returns seconds to wait."""
        with self._lock:
            now = self._clock()
            if self.settings["mode"] == "field" and self.on:
                self._check_time_limit(now)
            active = self._active_mode()
            if active is None:
                return IDLE_SEC
            site = self._site_for("field" if active == "field" else "home")
            if site is None:
                self._error = "Station grid not set or not valid in Settings"
                return POLL_SEC[self.level()]
            if self.site is None or glm.distance_mi(*self.site, *site) > SITE_MOVED_MI:
                self._move_to(site)
            if not self._reader_ok():
                self._error = "python3-netcdf4 isn't installed on this Pi, so satellite files can't be read"
                return POLL_SEC[self.level()]
            base = self._bucket_url()
            if not base:
                self._error = f"{SATELLITES[self.satellite()]} bucket address not set in Settings"
                return POLL_SEC[self.level()]
            economy = self.level() == "economy"
            if self.last_key_satellite != self.satellite():
                self.last_key = None              # another satellite's file names don't compare
            after = self.last_key
            started = glm.file_start(after) if after else None
            if started is None or now - started > timedelta(minutes=RESUME_MAX_MIN):
                after = None
        # Network work happens outside the lock, so the screens never wait on it.
        try:
            keys = self._list(base, now, after)
            todo = glm.choose(keys, after, economy) if after else keys[-1:]
            todo = todo[-MAX_FILES_PER_POLL:]
            bad = None
            for key in todo:
                damaged = False
                data = self._fetch(f"{base}/{urllib.parse.quote(key)}", FILE_TIMEOUT_SEC)
                self._count(len(data))
                try:
                    flashes = self._read(data)
                except glm.ReaderMissing:
                    raise
                except Exception as e:                # a damaged file: skip it, don't stall on it
                    flashes, damaged = [], True
                    bad = f"{SOURCE} sent a file that couldn't be read ({e.__class__.__name__})"
                    log.warning("lightning detection: skipped unreadable file %s", key.rsplit("/", 1)[-1])
                near = glm.nearby(flashes, site[0], site[1], radius_mi=self._t("lightning_field_outer_mi") + 5)
                with self._lock:
                    if self._active_mode() != active:
                        break                         # turned off (or switched) while fetching
                    seen = self._clock()
                    self.flashes.extend(dict(f, seen=seen) for f in near)
                    self.last_key, self.last_key_satellite = key, self.satellite()
                    if damaged:
                        continue                      # a skipped file is not fresh data
                    self._newest_file_at = glm.file_start(key)
                    self.files_today += 1
                    if near:
                        self._save()
            with self._lock:
                self._last_ok_at = self._clock()
                self._error = bad
        except glm.ReaderMissing:
            with self._lock:
                self._error = "python3-netcdf4 isn't installed on this Pi, so satellite files can't be read"
        except Exception as e:                        # network, S3 reply, a damaged file
            with self._lock:
                self._error = _describe(e)
            log.warning("lightning detection: %s", self._error)
        with self._lock:
            now = self._clock()
            self._prune(now)
            if self._saved_at is None or now - self._saved_at >= timedelta(minutes=1):
                self._save(now)
            return POLL_SEC[self.level()]

    def _list(self, base, now, after):
        """New keys from this hour's folder (and the last hour's, near the top of the hour
        or when catching up). S3's start-after keeps each reply to the new files only."""
        hours = [now.replace(minute=0, second=0, microsecond=0)]
        started = glm.file_start(after) if after else None
        if (started is not None and started < hours[0]) or (after is None and now.minute < 2):
            hours.insert(0, hours[0] - timedelta(hours=1))
        keys = []
        for hour in hours:
            query = {"list-type": "2", "prefix": glm.hour_prefix(hour)}
            if after:
                query["start-after"] = after
            body = self._fetch(f"{base}/?{urllib.parse.urlencode(query)}", LIST_TIMEOUT_SEC)
            self._count(len(body), file=False)
            found, _ = glm.listing_keys(body)
            keys.extend(found)
        return sorted(set(keys))

    def _count(self, n, file=True):
        with self._lock:
            now = self._clock()
            day = now.astimezone().strftime("%Y-%m-%d")      # the Pi's local day
            if day != self.count_day:
                self.count_day, self.bytes_today, self.files_today = day, 0, 0
            self.bytes_today += n
            self._hour.append((now, n))
            while self._hour and now - self._hour[0][0] > timedelta(hours=1):
                self._hour.popleft()

    # ---------------------------------------------------------------- blocks
    def _recent_at_off(self, now):
        if self.on or self.turned_off_at is None:
            return None
        if self.recent_dismissed_at is not None and self.recent_dismissed_at >= self.turned_off_at:
            return None
        seen = [f for f in self._within(now, self._t("lightning_field_outer_mi")) if f["time"] <= self.turned_off_at]
        if not seen:
            return None
        last = max(f["time"] for f in seen)
        closest = min(seen, key=lambda f: f["distance_mi"])
        return {"last_strike_at": freshness.iso(last), "closest_mi": round(closest["distance_mi"], 1),
                "closest_bearing": closest["bearing"], "until": freshness.iso(last + self._hold())}

    def detection(self):
        with self._lock:
            now = self._clock()
            auto_off = self._auto_off_at()
            return {
                "on": self.on, "level": self.level(), "satellite": self.satellite(),
                "satellite_name": SATELLITES[self.satellite()],
                "turned_on_at": freshness.iso(self.turned_on_at) if self.on else None,
                "outlook_seen": self.outlook_seen if self.on else None,
                "auto_off_at": freshness.iso(auto_off),
                "off_waiting_for_hold": self._deferred is not None and self.on,
                "question": self._question(now),
                "starting": self._starting(now),
                "turned_off_at": freshness.iso(self.turned_off_at),
                "off_reason": self.off_reason,
                "recent_at_off": self._recent_at_off(now),
                "reader": bool(self._reader_ok()),
            }

    def _starting(self, now):
        return (self._active_mode() is not None and self._newest_file_at is None
                and self._started_at is not None and (now - self._started_at).total_seconds() < START_GRACE_SEC)

    def _freshness(self, now):
        if self._newest_file_at is None:
            error = self._error or ("Starting: the first satellite file is due within a minute"
                                    if self._starting(now) else "No satellite data yet")
            return freshness.block(SOURCE, None, None, 0, error=error, now=now)
        stale = self._t("glm_stale_min") * 60 + (ECONOMY_STALE_EXTRA_SEC if self.level() == "economy" else 0)
        return freshness.block(SOURCE, self._last_ok_at or self._clock(), self._newest_file_at,
                               stale, error=self._error, now=now)

    def _tier_fields(self, now):
        """The tier flags and the details for the Weather page, from the kept flashes."""
        high, notify, outer = self._tiers()
        within = self._within(now, outer)
        out = {"within_20mi": bool(within), "within_10mi": False, "within_5mi": False, "frequent": False,
               "closest_mi": None, "closest_bearing": None, "last_strike_at": None,
               "strikes_recent": sum(1 for f in within if now - f["time"] <= timedelta(minutes=RECENT_WINDOW_MIN)),
               "window_min": RECENT_WINDOW_MIN,
               "hold_until_20mi": None, "hold_until_10mi": None, "hold_until_5mi": None, "hold_since_5mi": None}
        if not within:
            return out
        closest = min(within, key=lambda f: f["distance_mi"])
        out.update({"closest_mi": round(closest["distance_mi"], 1), "closest_bearing": closest["bearing"],
                    "last_strike_at": freshness.iso(max(f["time"] for f in within)),
                    "hold_until_20mi": freshness.iso(max(f["time"] for f in within) + self._hold())})
        ten = [f for f in within if f["distance_mi"] <= notify]
        if ten:
            out["within_10mi"] = True
            out["hold_until_10mi"] = freshness.iso(max(f["time"] for f in ten) + self._hold())
            last_minute = sum(1 for f in ten if (now - f["time"]).total_seconds() <= FREQUENT_WINDOW_SEC)
            out["frequent"] = last_minute > self._t("lightning_frequent_per_min")
        five = sorted(f["time"] for f in within if f["distance_mi"] <= high)
        if five:
            # The hold began with the first flash of the run that reaches now: a gap
            # longer than the hold would have ended the hold before.
            since = five[-1]
            for earlier in reversed(five[:-1]):
                if since - earlier > self._hold():
                    break
                since = earlier
            out.update({"within_5mi": True, "hold_until_5mi": freshness.iso(five[-1] + self._hold()),
                        "hold_since_5mi": freshness.iso(since)})
        return out

    def block(self):
        """The Field Mode `lightning` block (contract section 5)."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            detection = self.detection()
            unknown = {k: None for k in ("within_20mi", "within_10mi", "within_5mi", "frequent",
                                         "closest_mi", "closest_bearing", "last_strike_at",
                                         "strikes_recent", "window_min", "hold_until_20mi",
                                         "hold_until_10mi", "hold_until_5mi", "hold_since_5mi")}
            if not self.on:
                out = {"status": "off", "source": SOURCE, "fetched_at": None, "issued_at": None,
                       "age_sec": None, "message": "Lightning detection off"}
                out.update(unknown)
            else:
                out = self._freshness(now)
                out.update(unknown)
                if out["status"] != freshness.UNAVAILABLE:
                    out.update(self._tier_fields(now))
                elif self._within(now, self._t("lightning_field_outer_mi")):
                    # No current satellite data (just turned on, just restarted, or the
                    # bucket can't be reached) but flashes already seen: their holds still
                    # stand. A tier with no flash stays unknown (null), never false.
                    fields = self._tier_fields(now)
                    for k in ("within_20mi", "within_10mi", "within_5mi", "frequent"):
                        if fields[k] is False:
                            fields[k] = None
                    out.update(fields)
            out["cleared_at"] = (freshness.iso(self.cleared_at)
                                 if self.cleared_at and now - self.cleared_at < timedelta(hours=24) else None)
            out["detection"] = detection
            return out

    def system_block(self):
        """For the System page: what is running, the data it has used, and (at home)
        the test's own results to compare with the Tempest."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            active = self._active_mode()
            fresh = self._freshness(now) if active else None
            hour_bytes = sum(n for t, n in self._hour if now - t <= timedelta(hours=1))
            today = self.bytes_today if self.count_day == now.astimezone().strftime("%Y-%m-%d") else 0
            out = {
                "running": active, "home_test": self.home_test,
                "home_test_since": freshness.iso(self.home_test_since),
                "level": self.level(), "satellite": self.satellite(),
                "satellite_name": SATELLITES[self.satellite()],
                "reader": bool(self._reader_ok()),
                "status": fresh["status"] if fresh else None,
                "message": fresh["message"] if fresh else None,
                "newest_file_at": freshness.iso(self._newest_file_at),
                "mb_today": round(today / 1e6, 1),
                "mb_last_hour": round(hour_bytes / 1e6, 1),
                "files_today": self.files_today if today else 0,
                "test": None,
            }
            if active == "home_test":
                fields = self._tier_fields(now)
                out["test"] = {k: fields[k] for k in ("within_20mi", "within_10mi", "within_5mi", "closest_mi",
                                                       "closest_bearing", "last_strike_at", "strikes_recent",
                                                       "window_min")}
            return out
