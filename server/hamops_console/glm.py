"""Field Mode lightning from NOAA's GOES Geostationary Lightning Mapper (GLM).

Step 1 (2026-11-13): reading GLM files and working out what is near the operating site.
Not yet wired into the service; the fetching loop, the on/off switch and the lightning
block come next. Design agreed with Dan 2026-11-12 (see docs/contracts.md and the brief):

- Source: NOAA's public GLM-L2-LCFA files on AWS (``noaa-goes19`` = GOES-East), one file
  per 20 seconds covering the satellite's whole view. Measured on the Pi: about 60 MB an
  hour in November; files appear a median 9 s after they end.
- Tiers: an amber notification for any flash within 20 mi, then the same 10-mile notice
  and 5-mile alert as at home; one flash is enough; field holds clear 30 minutes after
  the last flash.
- Full reads every file; Economy reads one file a minute.

GLM reports *flashes* (a flash is all the light from one lightning discharge, in cloud or
to ground), each with an energy-weighted centre about 8 km across in resolution. All
quality flags are kept: a "degraded" flash (for example one with very many events) is
still lightning.

Files are NetCDF-4. Reading them needs the ``python3-netcdf4`` package (apt, no pip);
the import is deferred so the service runs without it until Field Mode lightning is used.
"""

import math
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

EARTH_RADIUS_MI = 3958.8
WATCH_RADIUS_MI = 25          # flashes kept around the site (the outer tier is 20 mi)
TIERS_MI = (5, 10, 20)        # alert, notice, notification (Dan, 2026-11-12)
HOLD_MIN = 30                 # field all-clear: 30 minutes after the last flash
S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


class ReaderMissing(RuntimeError):
    """python3-netcdf4 isn't installed on this Pi."""


# ------------------------------------------------------------------ reading a file

def read_flashes(data):
    """GLM-L2-LCFA file contents (bytes) -> [(time_utc, lat, lon)], one per flash.

    The time is the flash's first event: the file's start time plus its offset (which
    can be a little negative when a flash began just before the file's window)."""
    try:
        import netCDF4
    except ImportError as e:
        raise ReaderMissing("python3-netcdf4 isn't installed") from e
    with netCDF4.Dataset("glm", memory=data) as d:
        if "flash_lat" not in d.variables:
            return []
        start = _nc_time(d.getncattr("time_coverage_start"))
        lat = d.variables["flash_lat"][:]
        lon = d.variables["flash_lon"][:]
        off = d.variables["flash_time_offset_of_first_event"][:]
        out = []
        for la, lo, of in zip(lat, lon, off):
            if _masked(la) or _masked(lo):
                continue
            t = start + timedelta(seconds=0.0 if _masked(of) else float(of))
            out.append((t, float(la), float(lo)))
        return out


def _nc_time(text):
    """'2026-11-13T14:59:00.0Z' -> aware UTC datetime."""
    return datetime.strptime(text.rstrip("Z").split(".")[0], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def _masked(v):
    return getattr(v, "mask", False) is True


# ------------------------------------------------------------------ geometry

def distance_mi(lat1, lon1, lat2, lon2):
    """Great-circle distance in statute miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2):
    """Initial bearing from the site (1) to the flash (2), degrees from true north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def compass(deg):
    return COMPASS[int((deg + 11.25) // 22.5) % 16]


def nearby(flashes, lat, lon, radius_mi=WATCH_RADIUS_MI):
    """Flashes within radius_mi of the site -> [{time, distance_mi, bearing}], nearest first.

    A cheap latitude/longitude box first, so a whole-hemisphere file costs little."""
    dlat = radius_mi / 69.0 + 0.1
    dlon = radius_mi / (69.0 * max(0.1, math.cos(math.radians(lat)))) + 0.1
    out = []
    for t, la, lo in flashes:
        if abs(la - lat) > dlat or abs(lo - lon) > dlon:
            continue
        d = distance_mi(lat, lon, la, lo)
        if d <= radius_mi:
            out.append({"time": t, "distance_mi": d, "bearing": compass(bearing_deg(lat, lon, la, lo))})
    out.sort(key=lambda s: s["distance_mi"])
    return out


# ------------------------------------------------------------------ what it adds up to

class Recent:
    """The flashes near the site over the last HOLD_MIN minutes, and the tier they make.

    add() the nearby flashes from each file read; summary(now) says what to show."""

    def __init__(self, hold_min=HOLD_MIN):
        self.hold = timedelta(minutes=hold_min)
        self.flashes = []

    def add(self, flashes):
        self.flashes.extend(flashes)

    def summary(self, now):
        self.flashes = [f for f in self.flashes if now - f["time"] <= self.hold]
        within = [f for f in self.flashes if f["distance_mi"] <= TIERS_MI[-1]]
        if not within:
            return {"tier_mi": None, "closest_mi": None, "closest_bearing": None,
                    "last_time": None, "hold_until": None, "flashes_10_min": 0}
        closest = min(within, key=lambda f: f["distance_mi"])
        tier = next(t for t in TIERS_MI if any(f["distance_mi"] <= t for f in within))
        # Each tier holds until 30 minutes after the last flash inside it.
        last_in_tier = max(f["time"] for f in within if f["distance_mi"] <= tier)
        return {
            "tier_mi": tier,
            "closest_mi": round(closest["distance_mi"], 1),
            "closest_bearing": closest["bearing"],
            "last_time": max(f["time"] for f in within),
            "hold_until": last_in_tier + self.hold,
            "flashes_10_min": sum(1 for f in within if now - f["time"] <= timedelta(minutes=10)),
        }


# ------------------------------------------------------------------ finding new files

def hour_prefix(hour):
    """The bucket folder for one UTC hour: GLM-L2-LCFA/2026/317/14/."""
    return f"GLM-L2-LCFA/{hour:%Y}/{hour.timetuple().tm_yday:03d}/{hour:%H}/"


def listing_keys(xml_bytes):
    """An S3 list-objects reply -> (keys in order, truncated?)."""
    root = ET.fromstring(xml_bytes)
    keys = [c.findtext("s3:Key", namespaces=S3_NS) for c in root.findall("s3:Contents", S3_NS)]
    truncated = root.findtext("s3:IsTruncated", default="false", namespaces=S3_NS) == "true"
    return sorted(k for k in keys if k), truncated


def file_start(key):
    """OR_GLM-L2-LCFA_G19_s20263171459000_e..._c....nc -> 2026-11-13 14:59:00 UTC."""
    name = key.rsplit("/", 1)[-1]
    part = next((p for p in name.split("_") if p.startswith("s") and p[1:].isdigit()), None)
    if not part:
        return None
    return datetime.strptime(part[1:14], "%Y%j%H%M%S").replace(tzinfo=timezone.utc)


def choose(keys, after_key, economy=False):
    """The keys to read next: those after after_key; Economy keeps one a minute (the
    first file starting in each minute)."""
    new = [k for k in keys if after_key is None or k > after_key]
    if not economy:
        return new
    last = file_start(after_key) if after_key else None
    seen, out = ({last.replace(second=0)} if last else set()), []
    for k in new:
        s = file_start(k)
        minute = s.replace(second=0) if s else k
        if minute not in seen:
            seen.add(minute)
            out.append(k)
    return out
