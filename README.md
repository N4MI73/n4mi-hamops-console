# Ham Ops Console

A portable Raspberry Pi 5 touchscreen operations console for amateur radio. It puts propagation, weather, alerts and system status on one 7-inch screen, built around one question: **"What do I need to know right now?"**

- **Field Mode** (the main reason for the project): runs on its own at a portable site such as Field Day. It gets data over a phone hotspot and needs nothing from home.
- **Home Mode**: shows data from services already running in the shack, and acts mostly as a second display.

Both modes are the same dashboard reading the same JSON from a small service on the Pi; only that service's data sources change.

> **Status (November 2026): early development.** The hardware is built and the design is settled, but there is no dashboard yet. What's here so far: the kiosk display setup, a touch test page, the example settings file, and the first steps of the console service (propagation relay, home weather relay, NWS forecast and Pi status, with a plain check page).

## How it works

```
Home Mode:   home propagation monitor ─┐
             home weather server ──────┤
                                       ├─► console service (Python, systemd, on the Pi) ─► dashboard (Chromium kiosk)
Field Mode:  HamQSL, NOAA SWPC ────────┤        fetches and caches each source, decides fresh/stale,
             NWS api.weather.gov ──────┘        ranks alerts, reads the Pi's own status
```

- The **dashboard** is a local HTML/CSS/JavaScript page shown fullscreen by Chromium under the labwc Wayland compositor.
- It talks only to the **console service** on the Pi, never to the data sources directly. The service relays home services that send no CORS headers, and makes the NWS calls, which need a `User-Agent`.
- Switching Home ↔ Field is a setting, not a different build.
- With no internet, old data stays on screen greyed out and marked **STALE** with its age, and NWS alerts read **UNAVAILABLE**, never "no alerts".

## Repository layout

| Folder | Contents |
|---|---|
| `server/` | The console service (Python + Flask). So far: propagation relay from PropMon, home weather relay from Weather Sentinel, the NWS forecast, the Pi's own status, the shared freshness rule, alert classification, NWS alert text lines, and tests. See `server/README.md` |
| `dashboard/` | The touchscreen web page. For now a plain service check page; the real dashboard comes later |
| `pi/` | Files that set up the Raspberry Pi itself: the kiosk display (labwc + Chromium) and the service's systemd unit. See `pi/README.md` |
| `config/` | `settings.example.json`, the template for the console's settings |
| `diagnostics/` | Small standalone test tools, e.g. `kiosk-test.html` for checking screen rotation and touch |
| `docs/` | Reference documents (the JSON contracts will go here) |

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
- Console service: Python 3 + Flask (from Raspberry Pi OS packages), run as a systemd service

## Configuration

The console reads its settings from `~/.config/hamops-console/settings.json` on the Pi, **outside this repository**. Copy `config/settings.example.json` there and fill in your own callsign, grids and source addresses. Every data source address is a setting; nothing is hard-coded. Never commit real settings, addresses or contact details: `config/settings.json` is ignored by git for that reason.

## Running the tests

No network or Pi needed. `test_service.py`, `test_weather.py` and `test_forecast.py` need Flask (`sudo apt install python3-flask`, or `pip install flask` elsewhere):

```
python3 server/tests/test_nws_text.py
python3 server/tests/test_service.py
python3 server/tests/test_weather.py
python3 server/tests/test_forecast.py
```

## Credits

`server/hamops_console/nws_text.py` is ported from N4MI's [Weather Sentinel](https://github.com/N4MI73/n4mi-weather-sentinel), so Field Mode alerts read the same as the alerts Weather Sentinel provides at home.

Data sources: [NOAA/NWS](https://www.weather.gov) (alerts, forecasts, observations, NOAA Weather Radio listings), [NOAA SWPC](https://www.swpc.noaa.gov), [HamQSL](https://www.hamqsl.com), [KC2G](https://prop.kc2g.com).

## License

MIT. See [LICENSE](LICENSE).

— Dan Marshall, N4MI
