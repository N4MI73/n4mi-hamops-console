# Console service

The small Python service on the Pi that the dashboard talks to, run under systemd. It serves the dashboard page, gathers data from the configured sources (home services in Home Mode; NWS, HamQSL and NOAA SWPC in Field Mode), decides what is fresh or stale, ranks the alerts for the Priority Card, and reports the Pi's own status.

**So far (Phase 1, first step):**

| Endpoint | What it returns |
|---|---|
| `GET /` | The dashboard page (`dashboard/index.html`; for now a plain service check page) |
| `GET /api/propagation` | Propagation block, relayed from PropMon in Home Mode |
| `GET /api/system` | The Pi's own status: temperature, power/throttling, memory, disk, network, clock, GPS present |
| `GET /healthz` | `{"status": "ok"}` and the service version |

Every data block carries the same freshness fields (`status`: ok / stale / unavailable / test, `source`, `fetched_at`, `issued_at`, `age_sec`, `message`), decided in one place: `hamops_console/freshness.py`.

| Module | Role |
|---|---|
| `settings.py` | Reads `~/.config/hamops-console/settings.json` (or the file named by `HAMOPS_SETTINGS`) over built-in defaults; a missing or broken file never stops the service |
| `freshness.py` | The single fresh/stale rule |
| `propagation.py` | PropMon relay: asks PropMon at most every `propagation_fetch_min` minutes, keeps the last good reply, orders bands good → fair → poor |
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

No network or Pi needed (the service tests need Flask):

```
python3 server/tests/test_nws_text.py
python3 server/tests/test_service.py
```
