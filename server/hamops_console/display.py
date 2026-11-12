"""The screen: brightness by day and night, and Sleep (2026-11-11).

The Touch Display 2's backlight is a standard Linux backlight device under
/sys/class/backlight/ (`panel_backlight@1` on the Pi, max 31). Raspberry Pi OS
already lets the `video` group (hamops is in it) write `brightness`; `bl_power`
is root-only until the udev rule in pi/udev/ is installed, and isn't needed
(brightness 0 darkens the panel). If `brightness` can't be written, everything
here still works except that the screen itself doesn't change: the `display`
block in /api/system says so (`control: false`), and Settings shows it.

Brightness follows the Settings: day_brightness_pct, or night_brightness_pct
between night_start and night_end (local time; the night may cross midnight).
Sleep turns the backlight off; the dashboard puts a black cover over the page so
the touch that wakes the screen doesn't also press whatever was under the finger.
Night style (dim/red) is not applied here: "red" is a page colour, not a backlight
setting; the dashboard applies it from this block's `night` flag (common.js, 2026-11-12).
"""

import logging
import os
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

BACKLIGHT_ROOT = Path("/sys/class/backlight")
CHECK_SEC = 30              # how often the day/night schedule is checked


def find_backlight(root=BACKLIGHT_ROOT):
    """The first backlight device, or None (no display, or not a Pi)."""
    try:
        devices = sorted(p for p in root.iterdir() if (p / "brightness").exists())
    except OSError:
        return None
    return devices[0] if devices else None


def is_night(now, start, end):
    """now: a local datetime. start/end: "HH:MM". A night that crosses midnight
    (22:00 to 07:00) is handled; start == end means no night."""
    def minutes(text):
        h, m = text.split(":")
        return int(h) * 60 + int(m)
    try:
        s, e = minutes(start), minutes(end)
    except (AttributeError, ValueError):
        return False
    t = now.hour * 60 + now.minute
    if s == e:
        return False
    return s <= t < e if s < e else (t >= s or t < e)


def level(pct, max_brightness):
    """Percent -> the device's own scale, never 0 (0 is Sleep's job)."""
    pct = max(1, min(100, int(pct)))
    return max(1, round(max_brightness * pct / 100))


class Display:
    """Owns the backlight. Thread-safe; one per service."""

    def __init__(self, settings, device="auto", clock=None):
        self.settings = settings
        self.device = find_backlight() if device == "auto" else device
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.asleep = False
        self.applied_pct = None     # what was last written (None until written)
        self.error = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread = None

    # -------------------------------------------------- reading the device

    def _read(self, name):
        return (self.device / name).read_text().strip()

    def _write(self, name, value):
        (self.device / name).write_text(str(value))

    def max_brightness(self):
        try:
            return int(self._read("max_brightness"))
        except (OSError, ValueError, TypeError):
            return None

    def writable(self):
        if self.device is None:
            return False
        return os.access(self.device / "brightness", os.W_OK)

    # -------------------------------------------------- deciding

    def target_pct(self):
        d = self.settings.get("display", {})
        night = is_night(self.clock(), d.get("night_start"), d.get("night_end"))
        return int(d.get("night_brightness_pct" if night else "day_brightness_pct", 80)), night

    def apply(self):
        """Write what the screen should show now. Returns True if the device took it."""
        with self._lock:
            if self.device is None:
                self.error = "no backlight device found"
                return False
            try:
                if self.asleep:
                    self._set_power(False)
                    self._write("brightness", 0)
                    self.applied_pct = 0
                else:
                    pct, _ = self.target_pct()
                    top = self.max_brightness()
                    if top is None:
                        raise OSError("max_brightness unreadable")
                    self._set_power(True)
                    self._write("brightness", level(pct, top))
                    self.applied_pct = pct
                self.error = None
                return True
            except OSError as e:
                msg = "no permission (udev rule not installed)" if isinstance(e, PermissionError) else str(e)
                if msg != self.error:
                    log.warning("screen brightness not set: %s", msg)
                self.error = msg
                return False

    def _set_power(self, on):
        """bl_power: 0 on, 4 off. Not every driver has it, and on Raspberry Pi OS only
        `brightness` is group-writable out of the box (bl_power stays root-only until the
        udev rule is installed; seen on the Pi 2026-11-11). Brightness 0 turns the panel
        dark either way, so a bl_power that can't be written is skipped, not an error."""
        if (self.device / "bl_power").exists():
            try:
                self._write("bl_power", 0 if on else 4)
            except PermissionError:
                pass

    # -------------------------------------------------- actions

    def sleep(self):
        self.asleep = True
        log.info("screen asleep")
        return self.apply()

    def wake(self):
        self.asleep = False
        log.info("screen awake")
        return self.apply()

    def poke(self):
        """Settings changed: apply at once instead of waiting for the next check."""
        self._wake.set()

    def block(self):
        pct, night = self.target_pct()
        return {
            "control": self.writable(),        # false until the udev rule is installed
            "device": self.device.name if self.device else None,
            "asleep": self.asleep,
            "night": night,
            "brightness_pct": 0 if self.asleep else pct,
            "message": self.error,
        }

    def tick(self):
        """Write only when something changed (night began or ended, a new setting, or the
        last write failed), not every check: the panel is set over I2C."""
        want = 0 if self.asleep else self.target_pct()[0]
        if want != self.applied_pct or self.error:
            self.apply()

    def start(self):
        """Background check every CHECK_SEC, so night starts and ends on time."""
        if self._thread:
            return
        def run():
            while True:
                self.tick()
                self._wake.wait(CHECK_SEC)
                self._wake.clear()
        self._thread = threading.Thread(target=run, name="display", daemon=True)
        self._thread.start()
