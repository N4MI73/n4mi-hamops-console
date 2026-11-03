# Console service

The small Python service on the Pi that the dashboard talks to, run under systemd. It serves the dashboard page, gathers data from the configured sources (home services in Home Mode; NWS, HamQSL and NOAA SWPC in Field Mode), decides what is fresh or stale, ranks the alerts for the Priority Card, and reports the Pi's own status.

**So far (Phase 1):**

| Endpoint | What it returns |
|---|---|
| `GET /` | The dashboard page (`dashboard/index.html`; for now a plain service check page) |
| `GET /api/propagation` | Propagation block, relayed from PropMon in Home Mode |
| `GET /api/weather` | Weather blocks: NWS alerts, local conditions and lightning, relayed from Weather Sentinel in Home Mode. Plus the NWS forecast for the station grid (both modes). The NOAA Weather Radio block is `null` until a later step; Field Mode alerts and conditions aren't built yet |
| `GET /api/system` | The Pi's own status: temperature, power/throttling, memory, disk, network, clock, GPS present |
| `GET /healthz` | `{"status": "ok"}` and the service version |

Every data block carries the same freshness fields (`status`: ok / stale / unavailable / test, `source`, `fetched_at`, `issued_at`, `age_sec`, `message`), decided in one place: `hamops_console/freshness.py`.

| Module | Role |
|---|---|
| `settings.py` | Reads `~/.config/hamops-console/settings.json` (or the file named by `HAMOPS_SETTINGS`) over built-in defaults; a missing or broken file never stops the service |
| `freshness.py` | The single fresh/stale rule |
| `propagation.py` | PropMon relay: asks PropMon at most every `propagation_fetch_min` minutes, keeps the last good reply, orders bands good → fair → poor |
| `weather.py` | Weather Sentinel relay: one fetch a minute fills the alerts, conditions and lightning blocks. Follows the rules agreed with Weather Sentinel: in its simulation mode nothing passes through (status `test`); its acknowledgement fields are ignored and nothing is ever sent to it; missing fields are unknown, never "all clear". Alerts age by when Sentinel last reached NWS and are hidden after 30 min. Keeps the console's own 5-mile lightning hold (in memory, so a restart clears it) |
| `forecast.py` | NWS forecast for the centre of the current mode's grid: `/points` (re-checked daily or when the grid changes), then the 12-hour forecast, hourly forecast and gridpoint data every 30 min, **in the background**, so the page never waits for NWS over a slow hotspot. All three must succeed or the last good set is kept. Works out `thunder_next` and `lightning_outlook` (forecast, never detection); an active Severe Thunderstorm Warning/Watch or storm Special Weather Statement makes lightning "likely" (`weather.py`). Sends the `User-Agent` NWS requires, with `nws_user_agent_contact` |
| `grid.py` | Checks a Maidenhead grid and finds its centre. A 4-character grid works but is flagged as too coarse (`location.grid_warning`) |
| `alert_classes.py` | The one place that classifies an NWS product: warning/watch/advisory/statement, and hazard to the site (site hazard, temperature, other) |
| `system_info.py` | Readings from the Pi; each is `null` on a machine that isn't a Pi |
| `nws_text.py` | Short "what" and "hazard" lines from NWS alert text (ported from Weather Sentinel) |
| `app.py`, `__main__.py` | The Flask app and its start-up |

## Running it

Needs Python 3 and Flask. On the Pi, Flask comes from Raspberry Pi OS: `sudo apt install python3-flask`.

```
cd server
python3 -m hamops_console
```

It listens on `127.0.0.1:8090`, so only the Pi's own browser can reach it. To look at it from another computer while developing, set `"server": {"host": "0.0.0.0", "port": 8090}` in the settings file. On the Pi it runs as a systemd service; see `pi/README.md`.

## Tests

No network or Pi needed (the service, weather and forecast tests need Flask):

```
python3 server/tests/test_nws_text.py
python3 server/tests/test_service.py
python3 server/tests/test_weather.py
python3 server/tests/test_forecast.py
```
