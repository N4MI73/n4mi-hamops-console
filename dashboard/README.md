# Dashboard

The touchscreen page shown fullscreen by Chromium on the Pi (1280 × 720, the Touch Display 2 in landscape). It reads JSON only from the console service on the same Pi; the service decides freshness and ranks the alerts (`docs/contracts.md`), and this page turns that into words and colours.

| File | What it is |
|---|---|
| `index.html`, `home.css`, `home.js` | The **Home screen**, built from the approved mockup: status bar (UTC and local time, mode, callsign · grid, Internet/NAS/GPS), module row with the Alerts badge, the Priority Card (‹ › paging, flashing border and Acknowledge for critical items, two-step Clear hold for lightning), and the Propagation, Weather and System tiles. Polls the service every 15 s |
| `fonts/` | Barlow and Barlow Condensed, bundled so the screen looks the same in the field with no internet. SIL Open Font License (`fonts/OFL.txt`) |
| `check.html` | The plain service check page, served at `/check`, for diagnostics |

**Not built yet:** the Propagation, Weather, Alerts and System detail pages, Settings, Sleep and Shut down. Their buttons are in place, dimmed and marked "Coming".

Display rules from the approved design: stale data loses its colour and shows "STALE · N MIN"; unavailable NWS alerts never read as "no alerts"; every colour comes with a word.
