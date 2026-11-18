"""Where the console is: the station grid each data source should use.

Manual is the grid typed in Settings. GPS (Phase 4) is offered for the **Field
station only** (Dan, 2026-11-18): when the Field station's `grid_source` is
"gps" and the GPS has an accepted grid, that grid is used; until it has one
(no fix yet since the first install), the typed Field grid is used, and the
screens say so. The home grid is always typed.

Every place that needs the current grid asks here, so a GPS grid change
reaches the forecast, alerts, station, weather radio and propagation alike.
"""

_gps = None


def use_gps(source):
    """Called once at start-up with the GpsSource (tests may pass a stand-in)."""
    global _gps
    _gps = source


def gps_source():
    return _gps


def station_grid(settings, mode):
    """The grid to use for `mode` ("home" or "field")."""
    station = settings["station"].get(mode) or {}
    if mode == "field" and station.get("grid_source") == "gps" and _gps is not None:
        grid = _gps.grid()
        if grid:
            return grid
    return station.get("grid")


def grid_from(settings, mode):
    """Where the grid in use came from:
    'manual'; 'gps'; 'gps_unconfirmed' (the GPS grid saved before the last
    restart, no fix yet since, so it may be the previous site's); or
    'gps_waiting' (GPS chosen, no GPS grid at all: the typed grid is in use)."""
    station = settings["station"].get(mode) or {}
    if mode == "field" and station.get("grid_source") == "gps":
        if _gps is None or not _gps.grid():
            return "gps_waiting"
        return "gps" if _gps.confirmed() else "gps_unconfirmed"
    return "manual"
