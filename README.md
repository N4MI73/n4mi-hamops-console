# Ham Ops Console

A portable Raspberry Pi 5 touchscreen operations console for amateur radio. It puts propagation, weather, alerts and system status on one 7-inch screen, built around one question: **"What do I need to know right now?"**

- **Field Mode** (the main reason for the project): runs on its own at a portable site such as Field Day. It gets data over a phone hotspot and needs nothing from home.
- **Home Mode**: shows data from services already running in the shack, and acts mostly as a second display.

Both modes are the same dashboard reading the same JSON from a small service on the Pi; only that service's data sources change.

> **Status (November 2026): release candidate in preparation.** Every screen is built and running on the real console: Home, Propagation, Weather (with the NOAA Weather Radio panel), Alerts, System, Settings (including Wi-Fi from the touchscreen and a Field Day timer) and Help. Field Mode is complete and self-contained: NWS alerts, forecast and nearest station; bands rated on the Pi; GOES satellite lightning; the grid from a USB GPS. Also: Sleep, day/night brightness and a red night style, panel cycling for a tent display, and an automatic mirror to an HDMI monitor. Version 0.9.0 follows a dry run away from home in January 2027; 1.0 is a successful Winter Field Day. The MUF map screen is deferred.

## How it works

```
Home Mode:   home propagation monitor ─┐
             home weather server ──────┤
                                       ├─► console service (Python, systemd, on the Pi) ─► dashboard (Chromium kiosk)
Field Mode:  HamQSL, NOAA SWPC ────────┤        fetches and caches each source, decides fresh/stale,
             NWS api.weather.gov ──────┤        ranks alerts, reads the Pi's own status
             NOAA GOES lightning ──────┘
```

- The **dashboard** is a local HTML/CSS/JavaScript page shown fullscreen by Chromium under the labwc Wayland compositor.
- It talks only to the **console service** on the Pi, never to the data sources directly. The service relays home services that send no CORS headers, and makes the NWS calls, which need a `User-Agent`.
- Switching Home ↔ Field is a setting, not a different build.
- With no internet, old data stays on screen greyed out and marked **STALE** with its age, and NWS alerts read **UNAVAILABLE**, never "no alerts".

## Repository layout

| Folder | Contents |
|---|---|
| `server/` | The console service (Python + Flask): the home relays (PropMon, Weather Sentinel), the Field Mode sources (NWS, the field propagation engine, GOES satellite lightning), GPS, Wi-Fi, the ranked alert list, the settings API, the Pi's own status and display control, the shared freshness rule, and tests. See `server/README.md` |
| `dashboard/` | The touchscreen web pages (Home, Propagation, Weather, Alerts, System, Settings) with their bundled fonts, and a service check page at `/check`. See `dashboard/README.md` |
| `pi/` | Files that set up the Raspberry Pi itself: the kiosk display (labwc + Chromium) and the service's systemd unit. See `pi/README.md` |
| `config/` | `settings.example.json`, the template for the console's settings |
| `diagnostics/` | Small standalone test tools: `kiosk-test.html` (screen rotation and touch), `glm_size_check.py` (GOES lightning data cost, listings only), `glm_replay.py` (replays a past storm's archived GOES lightning files through the console's own lightning code and prints what the Priority Card would have said) |
| `docs/` | Reference documents, including the JSON contracts (`docs/contracts.md`) |

## Hardware

| Part | Choice |
|---|---|
| Computer | Raspberry Pi 5 (8 GB), official Active Cooler |
| Storage | 500 GB NVMe on a Pimoroni NVMe Base, booting from NVMe |
| Display | Official Raspberry Pi Touch Display 2 (7"), landscape 1280 × 720 |
| Enclosure | Pi and NVMe Base mounted on the back of the display under a 3D-printed open-grid cage |
| Location and time | USB GPS (GlobalSat BU-353S4); Pi 5 RTC backup battery |
| Power | Official 27 W USB-C supply, from mains or a portable power station's AC outlet |

## Software

- Raspberry Pi OS Lite (64-bit, Debian 13 "trixie")
- labwc (Wayland compositor) + Chromium in kiosk mode
- Console service: Python 3 + Flask and astral (from Raspberry Pi OS packages), run as a systemd service

## Configuration

The console reads its settings from `~/.config/hamops-console/settings.json` on the Pi, **outside this repository**. Copy `config/settings.example.json` there and fill in your own callsign, grids and source addresses. Every data source address is a setting; nothing is hard-coded. Never commit real settings, addresses or contact details: `config/settings.json` is ignored by git for that reason.

## Running the tests

No network or Pi needed. The service, weather, forecast and alert tests need Flask (`sudo apt install python3-flask`, or `pip install flask` elsewhere); the Field Mode propagation tests need astral (`sudo apt install python3-astral`, or `pip install astral==3.2`). Run them all with `for t in server/tests/test_*.py; do python3 $t; done`, or `python3 -m unittest discover -s server/tests`. The GOES lightning file tests also need netCDF4 (`sudo apt install python3-netcdf4`) and are skipped without it.


## Credits

`server/hamops_console/nws_text.py` is ported from N4MI's [Weather Sentinel](https://github.com/N4MI73/n4mi-weather-sentinel), so Field Mode alerts read the same as the alerts Weather Sentinel provides at home.

`server/hamops_console/fieldengine/` is Ham Shack Automation's PropMon field engine from [n4mi-shack-server](https://github.com/N4MI73/n4mi-shack-server) (`field-engine/`, commit `4872101`), so Field Mode rates bands with the same model as PropMon at home; its README lists what was changed in this copy.

Data sources: [NOAA/NWS](https://www.weather.gov) (alerts, forecasts, observations, NOAA Weather Radio listings), [NOAA SWPC](https://www.swpc.noaa.gov), [NOAA GOES Geostationary Lightning Mapper](https://registry.opendata.aws/noaa-goes/) (Field Mode lightning), [HamQSL](https://www.hamqsl.com).

## License

MIT. See [LICENSE](LICENSE).

— Dan Marshall, N4MI
