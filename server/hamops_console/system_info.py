"""System block (contract section 7): readings from the Pi itself.

Everything here reads local files or runs a local command. On a machine that
isn't a Pi (e.g. a development PC), a reading that can't be taken is None,
never an exception, so the service runs anywhere.

GPS details come from gpsd in a later step; for now `gps` reports whether the
receiver is plugged in.
"""

import os
import shutil
import socket
import subprocess
from pathlib import Path

from . import freshness

SOURCE = "Pi"


def _read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _run(args, timeout=3):
    """Run a command and return its stdout, or None if it fails or is missing."""
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def cpu_temp_c():
    raw = _read("/sys/class/thermal/thermal_zone0/temp")
    try:
        return round(int(raw) / 1000, 1)
    except (TypeError, ValueError):
        return None


def throttle_flags(text=None):
    """Decode `vcgencmd get_throttled` ("throttled=0x50000"). Bit meanings are
    Raspberry Pi's: 0 under-voltage now, 2 throttled now, 16 under-voltage has
    occurred since boot, 18 throttling has occurred since boot."""
    text = text if text is not None else _run(["vcgencmd", "get_throttled"])
    if not text or "=" not in text:
        return {"undervoltage_now": None, "throttled_now": None,
                "undervoltage_since_boot": None, "throttled_since_boot": None}
    try:
        value = int(text.split("=", 1)[1], 16)
    except ValueError:
        return throttle_flags("")
    return {
        "undervoltage_now": bool(value & (1 << 0)),
        "throttled_now": bool(value & (1 << 2)),
        "undervoltage_since_boot": bool(value & (1 << 16)),
        "throttled_since_boot": bool(value & (1 << 18)),
    }


def memory_used_pct():
    text = _read("/proc/meminfo")
    if not text:
        return None
    info = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            info[key] = int(parts[0])
    total, available = info.get("MemTotal"), info.get("MemAvailable")
    if not total or available is None:
        return None
    return round(100 * (total - available) / total)


def uptime_sec():
    text = _read("/proc/uptime")
    try:
        return int(float(text.split()[0]))
    except (AttributeError, IndexError, ValueError):
        return None


def cpu_load_pct():
    try:
        load1 = os.getloadavg()[0]
    except (AttributeError, OSError):
        return None
    cpus = os.cpu_count() or 1
    return min(100, round(100 * load1 / cpus))


def disk():
    try:
        usage = shutil.disk_usage("/")
    except OSError:
        return None, None
    gb = 1024 ** 3
    return round(usage.used / gb, 1), round(usage.total / gb)


def network():
    """Active NetworkManager connection (its profile name, never a password),
    the device and the Pi's address."""
    name = device = None
    text = _run(["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"])
    if text:
        for line in text.splitlines():
            conn, _, dev = line.rpartition(":")
            if dev and dev != "lo":
                name, device = conn, dev
                break
    ip = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET address: no packet is sent
            ip = s.getsockname()[0]
    except OSError:
        pass
    return {"interface": device, "connection": name, "ip": ip}


def clock():
    """Whether the system clock is synchronised, from timedatectl."""
    text = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    synced = None if text is None else (text == "yes")
    return {"source": "ntp" if synced else None, "synced": synced}


def gps():
    present = Path("/dev/ttyUSB0").exists()
    return {"present": present, "fix": None, "satellites_used": None,
            "satellites_seen": None, "grid": None, "last_fix_at": None}


def block(version, started_at, now=None):
    now = now or freshness.utcnow()
    used, total = disk()
    out = freshness.block(SOURCE, now, None, 60, now=now)
    out.update({
        "hostname": socket.gethostname(),
        "uptime_sec": uptime_sec(),
        "cpu_temp_c": cpu_temp_c(),
        "cpu_load_pct": cpu_load_pct(),
        "memory_used_pct": memory_used_pct(),
        "disk_used_gb": used,
        "disk_total_gb": total,
        "power": throttle_flags(),
        "network": network(),
        "clock": clock(),
        "gps": gps(),
        "service": {"version": version, "started_at": freshness.iso(started_at)},
    })
    return out
