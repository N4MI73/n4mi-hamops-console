# Dashboard

The touchscreen page shown fullscreen by Chromium on the Pi (1280 × 720, the Touch Display 2 in landscape). It reads JSON only from the console service on the same Pi; the service decides freshness and ranks the alerts (`docs/contracts.md`), and this page turns that into words and colours.

| File | What it is |
|---|---|
| `common.js` | Shared by every screen: the status bar (UTC and local time, mode, callsign · grid, Internet/NAS/GPS), the module row with the Alerts badge, the clock, the one freshness function, and polling the service every 15 s. On a detail page it goes back to Home after 3 minutes untouched, or at once for a new unacknowledged critical item |
| `home.css` | Fonts, colours and the shared layout (status bar, module row, tiles, toolbar), plus the Home screen's own styles. Every screen loads it |
| `index.html`, `home.js` | The **Home screen**, built from the approved mockup: the Priority Card (‹ › paging, 60-second rotation unless a critical item pins it, flashing border and Acknowledge for critical items, two-step Clear hold for lightning), and the Propagation, Weather and System tiles |
| `weather.html`, `weather.css`, `weather.js` | The **Weather page** (`/weather`), from the approved mockup: this location's lightning and NWS alerts, current conditions (the home Tempest in Home Mode), the NOAA Weather Radio panel, four forecast periods and the next 12 hours |
| `fonts/` | Barlow and Barlow Condensed, bundled so the screen looks the same in the field with no internet. SIL Open Font License (`fonts/OFL.txt`) |
| `check.html` | The plain service check page, served at `/check`, for diagnostics |

**Not built yet:** the Propagation, Alerts and System detail pages, Settings, Sleep, Help and Shut down. Their buttons are in place, dimmed and marked "Coming". The Weather page's NOAA Weather Radio panel says "Station list not loaded yet" until the service has downloaded the NWS transmitter list once (it then works offline), and shows in amber when the transmitter isn't confirmed for the county.

Display rules from the approved design: stale data loses its colour and shows "STALE · N MIN"; unavailable NWS alerts never read as "no alerts"; every colour comes with a word.
