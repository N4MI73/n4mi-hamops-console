"""System block (contract section 7): readings from the Pi itself.

Everything here reads local files or runs a local command. On a machine that
isn't a Pi (e.g. a development PC), a reading that can't be taken is None,
never an exception, so the service runs anywhere.

GPS details come from gpsd in a later step; for now `gps` reports whether the
receiver is plugged in.

2026-11-10, for the System page: memory in GB, the Wi-Fi signal (from
NetworkManager's last scan, never starting a new one) and the real-time
clock's backup battery.
"""

import glob
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


def memory(text=None):
    """(used %, used GB, total GB) from /proc/meminfo; Nones if unreadable."""
    text = text if text is not None else _read("/proc/meminfo")
    if not text:
        return None, None, None
    info = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            info[key] = int(parts[0])
    total, available = info.get("MemTotal"), info.get("MemAvailable")
    if not total or available is None:
        return None, None, None
    kib_per_gb = 1024 ** 2
    return (round(100 * (total - available) / total), round((total - available) / kib_per_gb, 1),
            round(total / kib_per_gb, 1))


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
    return {"interface": device, "connection": name, "ip": ip,
            "wifi_signal_pct": wifi_signal() if device and device.startswith("wl") else None}


def wifi_signal(text=None):
    """Signal (0-100) of the Wi-Fi network in use, from NetworkManager's last
    scan list. `--rescan no` so reading it never starts a scan (scanning needs
    a permission the service doesn't have, and costs radio time)."""
    text = text if text is not None else _run(
        ["nmcli", "-t", "-f", "IN-USE,SIGNAL", "device", "wifi", "list", "--rescan", "no"])
    for line in (text or "").splitlines():
        in_use, _, signal = line.partition(":")
        if in_use.strip() == "*" and signal.strip().isdigit():
            return int(signal)
    return None


RTC_GLOB = "/sys/devices/platform/soc*/*rpi_rtc/rtc/rtc0"


def rtc(base=None):
    """The real-time clock's backup battery: voltage and whether charging is
    on. Pi 5 only (on Trixie the folder is under soc@107c000000, so it's found
    with a pattern rather than a fixed path). Values are in microvolts."""
    folders = [base] if base else glob.glob(RTC_GLOB)
    for folder in folders:
        def volts(name):
            raw = _read(os.path.join(folder, name))
            try:
                return round(int(raw) / 1_000_000, 2)
            except (TypeError, ValueError):
                return None
        battery, charging = volts("battery_voltage"), volts("charging_voltage")
        if battery is not None or charging is not None:
            return {"battery_v": battery, "charging": None if charging is None else charging > 0}
    return {"battery_v": None, "charging": None}


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
    mem_pct, mem_used, mem_total = memory()
    out = freshness.block(SOURCE, now, None, 60, now=now)
    out.update({
        "hostname": socket.gethostname(),
        "uptime_sec": uptime_sec(),
        "cpu_temp_c": cpu_temp_c(),
        "cpu_load_pct": cpu_load_pct(),
        "memory_used_pct": mem_pct,
        "memory_used_gb": mem_used,
        "memory_total_gb": mem_total,
        "disk_used_gb": used,
        "disk_total_gb": total,
        "power": throttle_flags(),
        "network": network(),
        "clock": clock(),
        "rtc": rtc(),
        "gps": gps(),
        "service": {"version": version, "started_at": freshness.iso(started_at)},
    })
    return out
