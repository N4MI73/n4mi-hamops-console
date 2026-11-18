"""GPS as a location source (Phase 4): the console reads gpsd, never the receiver.

gpsd (Raspberry Pi OS package, started by its socket when something connects)
owns the USB receiver: baud rate, NMEA parsing, unplug and replug. This module
connects to gpsd on the Pi itself (127.0.0.1:2947), asks for its JSON reports
and keeps:

- whether a receiver is reporting, the fix (2D/3D) and satellites in use/seen;
- the GPS grid: the 6-character subsquare of the position, accepted only after
  the same new subsquare has held for `hold_sec` (2 minutes, Dan 2026-11-18), so
  a position near a square edge can't flip the grid back and forth. The accepted
  grid is saved on the Pi (`~/.local/state/hamops-console/gps.json`, grid and
  time only, no coordinates), so after a reboot at the site it is used until the
  next fix;
- a time check: GPS time against the Pi's clock, only with a fix. The console
  never sets the clock from GPS (Dan 2026-11-18): the RTC battery keeps time with
  no internet, and this receiver reports a made-up date (2031) before its first
  fix, so GPS time is only compared and reported.

The GPS grid is used for the Field station only (see location.py).

2026-11-18 (later): `details()` for the GPS details panel (GET /api/gps): the
satellites heard (direction, height, signal, used or not), position, altitude,
accuracy, how long the first fix took and the receiver as gpsd reports it.
Position is kept in memory only, never saved.
"""

import json
import logging
import os
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import freshness, grid as grid_mod

log = logging.getLogger(__name__)

STATE_FILE = Path(os.path.expanduser("~/.local/state/hamops-console/gps.json"))
GPSD = ("127.0.0.1", 2947)
WATCH = b'?WATCH={"enable":true,"json":true};\n'
REPORT_GONE_SEC = 10     # no report for this long: the receiver isn't reporting
FIX_GONE_SEC = 10        # no fix report for this long: no fix now
TIME_AGREES_SEC = 2      # GPS and Pi clocks within this many seconds agree
TIME_WRONG_SEC = 86400   # more than a day apart: the GPS date is wrong, not the clock


def to_grid(lat, lon):
    """(lat, lon) in degrees -> 6-character Maidenhead subsquare, e.g. 'FM18lv'."""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("position out of range")
    x = min(lon + 180, 359.999999)
    y = min(lat + 90, 179.999999)
    return (chr(ord("A") + int(x // 20)) + chr(ord("A") + int(y // 10))
            + str(int(x % 20 // 2)) + str(int(y % 10))
            + chr(ord("a") + int(x % 2 * 12)) + chr(ord("a") + int(y % 1 * 24)))


def _parse_time(text):
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (AttributeError, ValueError):
        return None


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class GpsSource:
    def __init__(self, hold_sec=120, state_file=STATE_FILE, address=GPSD,
                 connect=socket.create_connection, device_path="/dev/ttyUSB0"):
        self.hold_sec = hold_sec
        self.state_file = Path(state_file)
        self.address = address
        self._connect = connect
        self._device_path = Path(device_path)
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._connected = False         # gpsd answering
        self._error = None
        self._last_report = None        # any TPV/SKY from a receiver
        self._mode = 0
        self._last_fix = None
        self._position_grid = None      # subsquare of the latest fix
        self._sat_used = None
        self._sat_seen = None
        self._candidate = None          # (grid, first seen) waiting out the hold
        self._time_offset = None        # GPS minus Pi clock, seconds, with a fix
        self._gps_time = None
        self._fix_since_start = False   # a saved grid isn't confirmed until a fix this run
        self._sats = []                 # [{prn, az, el, ss, used}] from the last SKY with a list
        self._hdop = None
        self._lat = self._lon = self._alt = self._acc = None
        self._release = None            # gpsd version
        self._device = {}               # path, driver, bps
        self._search_since = None       # when the receiver started reporting (this time)
        self._first_fix_sec = None
        self._grid, self._grid_at = self._load()

    # -- saved grid
    def _load(self):
        try:
            doc = json.loads(self.state_file.read_text())
            grid = doc.get("grid")
            at = _parse_time(doc.get("accepted_at") or "")
            if isinstance(grid, str) and len(grid) == 6 and grid_mod.is_valid(grid):
                return grid[:4].upper() + grid[4:].lower(), at
        except (OSError, ValueError, AttributeError):
            pass
        return None, None

    def _save(self):
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"grid": self._grid, "accepted_at": freshness.iso(self._grid_at)}))
            tmp.replace(self.state_file)
        except OSError as e:
            log.warning("couldn't save the GPS grid: %s", e)

    # -- reports from gpsd (pure apart from the clock; tests call it directly)
    def handle(self, msg, now=None):
        now = now or freshness.utcnow()
        if not isinstance(msg, dict):
            return
        cls = msg.get("class")
        with self._lock:
            if cls in ("TPV", "SKY") and (self._last_report is None
                                          or (now - self._last_report).total_seconds() > REPORT_GONE_SEC):
                self._search_since, self._first_fix_sec = now, None     # (re)started reporting
            if cls == "VERSION":
                self._release = msg.get("release") if isinstance(msg.get("release"), str) else None
            elif cls == "DEVICE" and isinstance(msg.get("path"), str):
                if msg.get("activated"):
                    self._device = {k: msg.get(k) for k in ("path", "driver", "bps")
                                    if isinstance(msg.get(k), (str, int)) and not isinstance(msg.get(k), bool)}
            elif cls == "TPV":
                self._last_report = now
                mode = msg.get("mode") if isinstance(msg.get("mode"), int) else 0
                self._mode = mode
                lat, lon = _num(msg.get("lat")), _num(msg.get("lon"))
                if mode >= 2 and lat is not None and lon is not None:
                    try:
                        grid = to_grid(lat, lon)
                    except ValueError:
                        return
                    # A gap in fixes restarts the hold: the 2 minutes must be continuous.
                    if self._last_fix is None or (now - self._last_fix).total_seconds() > FIX_GONE_SEC:
                        self._candidate = None
                    self._last_fix = now
                    self._fix_since_start = True
                    if self._first_fix_sec is None and self._search_since is not None:
                        self._first_fix_sec = int((now - self._search_since).total_seconds())
                    self._lat, self._lon = lat, lon
                    alt = _num(msg.get("altMSL"))
                    if alt is None:
                        alt = _num(msg.get("alt"))
                    self._alt = alt if mode >= 3 else None      # a 2D fix has no altitude
                    eph = _num(msg.get("eph"))
                    if eph is None:
                        ex, ey = _num(msg.get("epx")), _num(msg.get("epy"))
                        eph = max(ex, ey) if ex is not None and ey is not None else None
                    self._acc = eph
                    self._position_grid = grid
                    self._consider(grid, now)
                    t = _parse_time(msg.get("time") or "")
                    self._gps_time = t
                    self._time_offset = None if t is None else (t - now).total_seconds()
                else:
                    self._candidate = None      # no fix: the hold starts again at the next fix
            elif cls == "SKY":
                self._last_report = now
                seen, used = _num(msg.get("nSat")), _num(msg.get("uSat"))
                self._hdop = _num(msg.get("hdop"))
                sats = msg.get("satellites")
                if isinstance(sats, list):
                    self._sats = [
                        {"prn": s.get("PRN"), "az": _num(s.get("az")), "el": _num(s.get("el")),
                         "ss": _num(s.get("ss")), "used": s.get("used") is True}
                        for s in sats if isinstance(s, dict) and _num(s.get("PRN")) is not None][:64]
                elif seen == 0:
                    self._sats = []
                if isinstance(sats, list) and (seen is None or used is None):
                    seen = len(sats)
                    used = sum(1 for s in sats if isinstance(s, dict) and s.get("used"))
                if seen is not None:
                    self._sat_seen, self._sat_used = int(seen), int(used or 0)

    def _consider(self, grid, now):
        if grid == self._grid:
            self._candidate = None
            return
        if self._candidate is None or self._candidate[0] != grid:
            self._candidate = (grid, now)
            return
        if (now - self._candidate[1]).total_seconds() >= self.hold_sec:
            old = self._grid
            self._grid, self._grid_at, self._candidate = grid, now, None
            log.info("GPS grid %s (was %s)", grid, old or "none")
            self._save()

    # -- what others read
    def confirmed(self):
        """True once a fix has been seen since the service started. A grid only
        loaded from the saved file may be from the last site."""
        with self._lock:
            return self._fix_since_start

    def grid(self):
        """The accepted GPS grid (kept through a lost fix and a restart), or None."""
        with self._lock:
            return self._grid

    def block(self, now=None):
        now = now or freshness.utcnow()
        with self._lock:
            reporting = (self._last_report is not None
                         and (now - self._last_report).total_seconds() <= REPORT_GONE_SEC)
            fixed = (reporting and self._mode >= 2 and self._last_fix is not None
                     and (now - self._last_fix).total_seconds() <= FIX_GONE_SEC)
            offset = self._time_offset if fixed else None
            if offset is None:
                time_check = None
            elif abs(offset) <= TIME_AGREES_SEC:
                time_check = "agrees"
            elif abs(offset) >= TIME_WRONG_SEC:
                time_check = "gps_wrong"     # e.g. a receiver's made-up date; ignored
            else:
                time_check = "differs"
            if not self._connected:
                message = self._error or "gpsd isn't answering"
            elif not reporting:
                message = "No GPS receiver reporting"
            elif not fixed:
                message = "No fix yet"
            else:
                message = None
            return {
                "present": reporting or self._device_path.exists(),
                "reporting": reporting,
                "fix": ("3d" if self._mode >= 3 else "2d") if fixed else None,
                "satellites_used": self._sat_used if reporting else None,
                "satellites_seen": self._sat_seen if reporting else None,
                "grid": self._grid,
                "grid_accepted_at": freshness.iso(self._grid_at),
                "grid_confirmed": self._fix_since_start,
                "position_grid": self._position_grid if fixed else None,
                "grid_pending": self._candidate[0] if self._candidate else None,
                "last_fix_at": freshness.iso(self._last_fix),
                "time_offset_sec": None if offset is None else round(offset, 1),
                "time_check": time_check,
                "message": message,
            }

    def details(self, now=None):
        """The block plus what the GPS details panel shows (GET /api/gps)."""
        now = now or freshness.utcnow()
        out = self.block(now)
        with self._lock:
            fixed = out["fix"] is not None
            reporting = out["reporting"]
            out.update({
                "satellites": [dict(s) for s in self._sats] if reporting else [],
                "hdop": self._hdop if fixed else None,
                "lat": round(self._lat, 5) if fixed and self._lat is not None else None,
                "lon": round(self._lon, 5) if fixed and self._lon is not None else None,
                "alt_m": round(self._alt, 1) if fixed and self._alt is not None else None,
                "accuracy_m": round(self._acc, 1) if fixed and self._acc is not None else None,
                "gps_time": freshness.iso(self._gps_time) if fixed and self._gps_time else None,
                "searching_since": freshness.iso(self._search_since) if reporting else None,
                "first_fix_sec": self._first_fix_sec if reporting else None,
                "gpsd_release": self._release,
                "device": dict(self._device) if self._device else None,
            })
        return out

    # -- background reader
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="gps", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self._read_once()
            except Exception as e:      # never let the reader die
                with self._lock:
                    self._connected = False
                    self._error = f"gpsd: {e}"
            self._stop.wait(10)

    def _read_once(self):
        sock = self._connect(self.address, timeout=5)
        try:
            sock.settimeout(30)     # gpsd may be quiet with no receiver; that's fine
            sock.sendall(WATCH)
            with self._lock:
                self._connected, self._error = True, None
            buf = b""
            while not self._stop.is_set():
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    raise ConnectionError("gpsd closed the connection")
                buf += chunk
                if len(buf) > 1_000_000:        # a line that never ends
                    buf = b""
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        self.handle(json.loads(line))
                    except ValueError:
                        pass
        finally:
            with self._lock:
                self._connected = False
            try:
                sock.close()
            except OSError:
                pass
