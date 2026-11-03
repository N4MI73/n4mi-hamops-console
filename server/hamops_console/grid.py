"""Maidenhead grid locators: check one and find its centre point.

The console locates itself for NWS by the centre of the station grid
(contract section 9). A 6-character subsquare is about 3 x 5 miles; a
4-character square is about 70 x 115 miles, too coarse for local forecasts
and alerts, so it is accepted but flagged.
"""

import re

_GRID = re.compile(r"^[A-R]{2}[0-9]{2}([A-X]{2})?$", re.IGNORECASE)

COARSE_WARNING = ("4-character grid: about 70 x 115 miles, too coarse for local "
                  "forecasts and alerts. Use 6 characters.")


def is_valid(grid):
    return isinstance(grid, str) and bool(_GRID.match(grid.strip()))


def centre(grid):
    """'FM18lv' -> (lat, lon) of the square's centre, rounded to 4 places
    (NWS redirects requests with more). Raises ValueError for a bad grid."""
    if not is_valid(grid):
        raise ValueError(f"not a 4- or 6-character Maidenhead grid: {grid!r}")
    g = grid.strip().upper()
    lon = (ord(g[0]) - ord("A")) * 20 - 180 + int(g[2]) * 2
    lat = (ord(g[1]) - ord("A")) * 10 - 90 + int(g[3])
    if len(g) == 6:
        lon += (ord(g[4]) - ord("A")) * 5 / 60 + 2.5 / 60
        lat += (ord(g[5]) - ord("A")) * 2.5 / 60 + 1.25 / 60
    else:
        lon += 1
        lat += 0.5
    return round(lat, 4), round(lon, 4)
