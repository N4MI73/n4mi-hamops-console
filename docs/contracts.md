# Ham Ops Console — JSON Contracts

**Version 0.4, 5 November 2026. Approved for building.** Draft 0.3 (1 November) went to reviewers; the feedback was positive, and further suggestions are expected once people can use a working console, so the project owner approved building on it. Version 0.4 records what the console service actually does (built 2 November, running on the Pi since 3 November) and the decisions made since. Later changes are made here and dated.

**All values in the examples are illustrative.** Grids, addresses, stations and IDs are placeholders.

---

## 1. How the pieces fit

```
Home Mode:   PropMon ──────────┐
             Weather Sentinel ─┤
                               ├─► console service (Python, systemd, on the Pi) ─► dashboard (kiosk Chromium)
Field Mode:  HamQSL, NOAA SWPC ┤        • fetches and caches each source
             NWS api.weather.gov┘       • translates to the contracts below
                                        • decides fresh / stale / unavailable / test
                                        • ranks the Priority Card
                                        • reads Pi status (temperature, disk, GPS, clock)
```

**These contracts are the console service's own API: what the dashboard reads.** They are the same in both modes. Home or Field changes only where the service gets its data. The dashboard never talks to PropMon, Weather Sentinel or NWS directly.

The source formats stay owned by their projects. The service translates them; when a source changes, only the translation changes, not the dashboard.

The service listens on `127.0.0.1:8090` only: nothing else on the network can reach it.

## 2. Rules that apply everywhere

- **Times:** ISO 8601 in UTC with `Z` (e.g. `2026-11-01T14:52:10Z`). The dashboard formats them. **Changed 11 November 2026 (Dan): the main clock.** The setting `display.main_clock` (`"utc"`, the default, or `"local"`) decides which time is shown large and first everywhere: the status bar ("00:09Z  7:09 PM EST · Wed 11 Nov", or "7:09 PM  EST · 00:09Z · Wed 11 Nov"), pairs of times ("15:36 UTC (10:36 AM)" or "10:36 AM (15:36 UTC)"), and a time standing alone ("Until 20:04Z" or "Until 3:04 PM"). Wording the service writes itself (alert `detail`) follows the same setting, so with the default it now reads "Until 15:36 UTC (10:36 AM)" (before 11 November it was always local first). It is changed by tapping the big time in the status bar (saved at once through `PUT /api/settings`, so it survives a restart) or on the Settings Display tab.
- **Units:** °F, mph, inches, miles for weather; °C for the Pi's CPU temperature. NWS's metric values are converted by the service.
- **Missing data:** a field the service can't fill is `null`, never left out and never a made-up zero. The dashboard shows `--` for `null`.
- **Unknown fields:** the dashboard ignores fields it doesn't know, so the service can add fields without breaking it.
- **Freshness is decided in one place: the service.** Every block carries the same freshness fields, computed from the thresholds in Settings. The dashboard has one shared function that turns `status` into colour and wording.
- **Every `GET` response** carries `mode` (`home` or `field`) and `generated_at` at the top level, and is sent with `Cache-Control: no-store`.

### The freshness fields (every data block has these)

| Field | Meaning |
|---|---|
| `status` | `ok`, `stale`, `unavailable`, or `test` (Weather Sentinel in simulation) |
| `source` | Short name shown on Help/System pages: `PropMon`, `Weather Sentinel`, `HamQSL + NOAA SWPC`, `NWS`, `Pi` |
| `fetched_at` | When the service last got a good response from the source |
| `issued_at` | The source's own time for the data: PropMon `updated`, NWS forecast `updateTime`, Tempest `observed_at`, Sentinel's `nws.last_polled_at` for alerts. `null` if the source gives none |
| `age_sec` | Seconds since `issued_at` (or since `fetched_at` if there's no `issued_at`) |
| `message` | Short plain-English reason when `status` isn't `ok`, e.g. "PropMon not answering (URLError)". `null` when ok |

The rules, in order:
1. Never had good data → `unavailable`.
2. Older than the block's *unavailable* limit, where it has one → `unavailable`. **The service then sends no data for that block** (for NWS alerts: an empty list, never an old one).
3. The source is in test mode → `test`.
4. Older than the stale threshold, or the source says its own data isn't current (Sentinel's `available: false`) → `stale`. The last good data is still sent; the dashboard greys it and shows its age ("STALE · 42 min").
5. Otherwise → `ok`.

**`unavailable`** for NWS alerts: the dashboard says "UNAVAILABLE — this is not the same as no alerts" and points to NOAA Weather Radio; it never says "no alerts".
**`test`** = Weather Sentinel is in simulation. The service passes **no alert items** through and the lightning flags are `null`, so a test can't reach the Priority Card even by mistake.

### Fetching and thresholds (all editable in Settings)

| Block | Fetched every | Stale after | Unavailable after | Notes |
|---|---|---|---|---|
| Propagation, Home | 5 min (`propagation_fetch_min`) | 15 min (30 until 7 November 2026) | — | Fetched only when asked, at most this often; PropMon's own `updated` is the age |
| Propagation, Field | 10 min (`field_propagation_fetch_min`), in the background | 15 min (`propagation_stale_min`) | — | Built 17 November 2026; aged from the last good HamQSL reply; retried 2 min after a failure; bands re-rated on every request |
| NWS alerts, Home | with the Sentinel fetch, 1 min (`sentinel_fetch_min`) | 10 min | 30 min (`nws_alerts_unavailable_min`) | Aged by when Sentinel last reached NWS (`last_polled_at`), not by whether Sentinel answers |
| NWS alerts, Field | 2 min (`nws_alerts_fetch_min`), in the background | 10 min | 30 min | Built 16 November 2026; aged by NWS's `updated`. Never slowed by Economy mode |
| Local conditions, Home (Tempest) | 1 min | 5 min, or at once if Sentinel says `available: false` | — | |
| Local conditions, Field (NWS station) | 15 min (`station_obs_fetch_min`), in the background | 90 min (`station_obs_stale_min`) | — | Built 16 November 2026; aged by the observation's own time |
| Lightning, Home (Tempest) | 1 min | 5 min (`lightning_stale_min`) | — | Home only |
| Lightning, Field (GOES satellite) | Only while switched on: every 20 s on Full (each new 20-second file), every 60 s on Economy (one file a minute), in the background | 3 min with no new file (`glm_stale_min`) | — | Built 20 November 2026. About 60 MB an hour on Full, 20 MB on Economy. Flashes already seen keep their holds while stale |
| Forecast (both modes) | 30 min (`forecast_fetch_min`), in the background | NWS `updateTime` older than 12 h | — | Retried 2 min after a failure |
| System | every request | never | — | It's the Pi itself |

The NWS forecast is fetched in the background: a request always gets the last good forecast at once, so the screen never waits for NWS over a weak hotspot. The first request after start-up starts the first fetch, so the forecast briefly reads "Fetching the NWS forecast".

**Economy mode (decided 5 November 2026; built in Phase 3):** one switch that saves both power and data. It dims the screen and stretches the slow fetches (chiefly the NWS forecast, the largest download). **NWS alerts are never slowed.** Stale thresholds stretch together with the fetch intervals, so data isn't marked stale just because it's checked less often. The service adjusts both in one place.

The dashboard polls the service every 15 seconds.

---

## 3. Endpoints

| Method + path | Returns | Built |
|---|---|---|
| `GET /` | The dashboard page | Yes (a plain check page until the dashboard is built) |
| `GET /healthz` | `{"status": "ok", "version": "..."}`; the kiosk waits for it at boot | Yes |
| `GET /api/propagation` | Propagation block (§4) | Yes, Home |
| `GET /api/weather` | Weather blocks (§5) | Home; forecast and weather radio both modes |
| `POST /api/weather-radio/refresh` | Update the stored NOAA Weather Radio list now, e.g. before a field event (§5) | Yes (7 Nov 2026) |
| `GET /api/wifi`, `POST /api/wifi/scan`, `/join`, `/order`, `/forget`, `/dismiss` | Wi-Fi networks from the touchscreen (§8, Wi-Fi) | Yes (17 Nov 2026) |
| `GET /api/alerts` | The ranked list behind the Priority Card and Alerts page (§6) | Yes |
| `POST /api/alerts/ack` | `{"id": "..."}`: the console's own Acknowledge (§6) | Yes |
| `POST /api/alerts/clear-hold` | Ends the lightning holds early, by the operator (§6) | **New in 0.4** |
| `POST /api/lightning` | Field Mode lightning detection: on, off, Keep on, dismiss, and the at-home test (§5) | Yes (20 Nov 2026) |
| `GET /api/system` | Pi and network status (§7) | Yes |
| `GET /api/settings` | Settings (§8) | Yes |
| `PUT /api/settings` | Change settings; returns the saved settings (§8) | Yes |
| `POST /api/settings/test` | Test connections (§8) | Yes |
| `POST /api/display` | `{"action": "sleep" \| "wake"}`: backlight off or back on (§7) | Yes (11 Nov 2026) |
| `POST /api/power` | `{"action": "shutdown" \| "restart"}`, from the Shut down dialog (§7) | Yes (11 Nov 2026) |

Errors from `POST`/`PUT` endpoints are JSON: `{"error": "..."}` or, for settings, `{"errors": [...]}`, with HTTP 400 (bad request), 404 (no such item) or 409 (nothing to act on).

---

## 4. Propagation — `GET /api/propagation`

```json
{
  "mode": "home",
  "generated_at": "2026-11-01T14:52:10Z",
  "propagation": {
    "status": "ok", "source": "PropMon",
    "fetched_at": "2026-11-01T14:50:02Z", "issued_at": "2026-11-01T14:49:44Z", "age_sec": 146, "message": null,
    "sfi": 122, "sunspots": 92, "a_index": 13, "k_index": 1,
    "xray": "B5.6", "solar_wind_kms": 470.1,
    "bands": [
      {"band": "20m", "rating": "good"},  {"band": "17m", "rating": "good"},
      {"band": "30m", "rating": "fair"},  {"band": "15m", "rating": "fair"},
      {"band": "12m", "rating": "fair"},
      {"band": "160m", "rating": "poor"}, {"band": "80m", "rating": "poor"},
      {"band": "40m", "rating": "poor"},  {"band": "10m", "rating": "poor"},
      {"band": "6m",  "rating": "poor"}
    ],
    "summary": "GOOD: 20m 17m | FAIR: 30m 15m 12m"
  }
}
```

- **Home:** from PropMon (`solar_wind` renamed to show its unit; `status` → `rating`, since `status` means freshness here).
- **Field (built 17 November 2026):** the service rates the bands itself with **Ham Shack Automation's field engine** (`server/hamops_console/fieldengine/`, a copy from `n4mi-shack-server` `field-engine/` at commit `4872101`: model `2026-11-17-v3`, Field Day scale `2026-11-17-fd1`; needs `python3-astral`), the same model as PropMon, for the centre of the Field grid and the site's time zone (from NWS `/points`, else the Pi's). Inputs: HamQSL `solarxml.php` (`solarflux`, `kindex`, `aindex`, `sunspots`, `xray`, `solarwind`) and NOAA's planetary K JSON for the 12-hour storm-recovery hold (if NOAA can't be reached no hold is applied and `note` says so). Missing SFI or K: `ratings_available: false` and no bands, never ten POORs. Extra fields in Field Mode: `scale` (`"dx"`, or `"field_day"` while the Field Day profile is on), `scale_version`, `model_version`, `rated_for` (the 4-character Field square), `meanings` (`{good, fair, poor}` one-liners for the scale, read by the Help panel; Field Day's come from the model file), `note`, and per band an optional `note` (6m on the Field Day scale: always `poor`, "Sporadic E openings can't be forecast from SFI/K - check the band"). `period` is 20m's time of day (the 40m–6m bucket, as PropMon's). `source` is `"HamQSL + NOAA SWPC"`. **The Field Day scale** rates North America reach (within 4,000 km) on 160, 80, 40, 20, 15, 10 and 6m; tuned on November data at EM83, suited to Winter Field Day, not yet validated for ARRL Field Day (June). In Home Mode the Field Day profile doesn't change PropMon's ratings.
- **`engine`, both modes (17 November 2026):** whether the field engine's copy of the model is current — `{"status": "current" | "out_of_date" | "unknown", "model_version", "field_day_version", "propmon_model_version", "published_model_version", "published_field_day_version", "checked_at", "message"}`. Compared with PropMon v2's `model_version` (read at home) and the published model file (setting `sources.field_engine_url`, read about daily, retried hourly). It only reports: **the console never downloads or switches models** (Dan, 16 November 2026); new models arrive by handover, committed with their tests. The Propagation page shows `message` in amber when out of date. Home Mode also reads PropMon v2's `model_version`.
- `rating` is `good`, `fair`, `poor`, or `null`. Poor shows grey, not red.
- **Band order:** good, then fair, then poor, then unknown; within each, lowest frequency first. The service sends `bands` already in this order.
- PropMon's `alerts[]` and `tower_status` are not used.

**Changed 7 November 2026: PropMon v2.** PropMon's server is owned by Ham Shack Automation; at its owner's request and Dan's decision the console now reads **`GET <propmon_url>/api/v2/conditions`** (v1, `/api/instrument/propagation`, stays frozen for the ESP32 instrument and DXMon). v2 only gains fields; renames and removals are checked with this project first. Designed from the live reply of 7 November 2026 (a G1 storm). New fields in the block, all present in every reply:

```json
    "ratings_available": true,
    "period": "dusk",
    "recovery_active": true, "recovery_ends": "2026-11-08T06:00:00Z"
```

- **`ratings_available`:** `false` when PropMon is on its NOAA fallback (HamQSL unreachable: its `source` is `"noaa"` or its `summary` is `"Data unavailable"`). PropMon then rates every band poor whatever the real conditions, so the service sends every band's `rating` as `null` and `summary` as `null`, and the screens say "Band ratings unavailable"; the solar figures (from NOAA) are still shown. `null` when there's no data at all. A real all-poor state from HamQSL (e.g. a storm) keeps `true`.
- **`period`:** `dawn`, `day`, `dusk`, `night` or `null` — the 40m–6m time of day where PropMon rates (sunrise/sunset ±1 hour for dawn/dusk). 160m and 80m switch only day/night, so they can differ. The Propagation page heading reads "BAND CONDITIONS · NOW · DAYTIME" (DAWN, DUSK, NIGHT).
- **`recovery_active` / `recovery_ends`:** PropMon caps ratings for 12 hours after any 3-hour Kp of 5 or more. While active, the Propagation page says "Storm recovery: PropMon holds ratings down until 1:00 AM (06:00 UTC)." and the Home tile "Storm recovery until …", so poor bands after a storm don't look like a fault. `recovery_ends` is `null` unless `recovery_active` is `true`.
- **Where the ratings apply:** PropMon rates with sun times for its own location (the home station). The Propagation page footnote reads "Ratings for FM18 from solar flux, K index and time of day", taking the 4-character square from the **home** station grid in Settings.
- **Just after a PropMon restart** v2 answers HTTP 503 `{"error": "no_data_yet"}`: the block says "PropMon has just restarted and has no data yet" (unavailable until the first good reply, or the last good data keeps ageing). Test connections reports a 503 as answering.
- **Stale after 15 minutes** (three missed PropMon fetches; PropMon's own limit is 30), from PropMon's `updated`.
- Read from v2: `bands[].band`, `bands[].status`, `period`, `source`, `summary`, `updated`, `solar.sfi`, `solar.sunspots`, `solar.a_index`, `solar.k_index`, `solar.xray`, `solar.solar_wind`, `geomagnetic.recovery_active`, `geomagnetic.recovery_ends`. Not used: `bands[].tod`, `tower`, `noaa_scales`, `sun`, `geomagnetic.kp_recent` / `penalty`, other `solar` fields. (Told to Ham Shack Automation, so PropMon changes can be checked against them.)

---

## 5. Weather — `GET /api/weather`

Weather is made of blocks that age separately, each with its own freshness fields. A block that doesn't exist in the current mode or isn't built yet is `null`.

```json
{
  "mode": "home",
  "generated_at": "2026-11-01T14:52:10Z",
  "location": {"label": "Home", "grid": "FM18lv", "grid_source": "manual",
               "nws_office": "LWX", "nws_zone": "VAZ054", "nws_county": "VAC013",
               "grid_warning": null},
  "alerts":     {"status": "ok", "source": "Weather Sentinel", "...": "...", "items": []},
  "conditions": {"status": "ok", "source": "Weather Sentinel", "...": "..."},
  "lightning":  {"status": "ok", "source": "Weather Sentinel", "...": "..."},
  "weather_radio": {"status": "ok", "source": "NWS NWR listing (stored on the Pi)", "...": "..."},
  "forecast":   {"status": "ok", "source": "NWS", "...": "..."}
}
```

### Location

| Field | Notes |
|---|---|
| `label`, `grid`, `grid_source` | The current mode's station settings; `grid` is the grid in use (from GPS when the Field station's grid comes from GPS) |
| `grid_from` | `manual`; `gps`; `gps_unconfirmed` (the GPS grid saved before the last restart, with no fix since, so it may be the previous site's; shown amber, "not yet confirmed"); or `gps_waiting` (GPS chosen but no GPS grid yet, so the typed grid is in use). Added 18 November 2026 |
| `nws_office`, `nws_zone`, `nws_county` | From NWS `/points` for the grid's centre; `null` until known |
| `grid_warning` | Text when the grid has only 4 characters ("about 70 x 115 miles, too coarse for local forecasts and alerts"), else `null` |

The **centre of the current mode's grid** locates the console for NWS. A 4-character grid works but is flagged; a 6-character subsquare is about 3 × 5 miles. `/points` is looked up again when the grid changes and once a day.

**GPS as the grid source (built 18 November 2026, Phase 4).** Offered for the **Field station only**; the home grid is always typed (Dan). The service reads the receiver through gpsd (Raspberry Pi OS package) on the Pi itself. A new GPS grid (6 characters) is accepted only after the same subsquare has held for **2 minutes** with a 2D or 3D fix, so a position at a square edge can't flip the grid back and forth (the console is used parked, never mobile: Dan). The accepted grid is kept through a lost fix and saved on the Pi (`~/.local/state/hamops-console/gps.json`, grid and time only), so after a restart at the site it is used until the next fix, marked `gps_unconfirmed` until a fix has been seen since the restart. The 2 minutes must be continuous: a report without a fix, or a gap of more than 10 s between fixes, starts the hold again. Until there is a GPS grid at all, the typed Field grid is used and `grid_from` is `gps_waiting`. Forecast, alerts, station, weather radio and Field propagation all follow the grid in use, so a GPS change reaches them like a typed change.

### Alerts (`alerts`)

```json
"alerts": {
  "status": "ok", "source": "Weather Sentinel",
  "fetched_at": "2026-11-01T14:51:40Z", "issued_at": "2026-11-01T14:51:32Z", "age_sec": 38, "message": null,
  "items": [
    {
      "id": "urn:oid:2.49.0.1.840.0.example",
      "event": "Severe Thunderstorm Warning",
      "kind": "warning",
      "hazard_class": "site_hazard",
      "severity": "Severe",
      "headline": "A severe thunderstorm was located over the area, moving east at 25 mph.",
      "hazard": "60 mph wind gusts and quarter size hail.",
      "instruction": "For your protection move to an interior room on the lowest floor of a building.",
      "onset": null,
      "expires": "2026-11-01T15:36:32Z",
      "ends": "2026-11-01T15:36:32Z"
    }
  ]
}
```

| Field | Notes |
|---|---|
| `id` | NWS alert id. Acknowledge is keyed on it |
| `event` | NWS event name |
| `kind` | `warning`, `watch`, `advisory` or `statement`, from the event name |
| `hazard_class` | `site_hazard`, `temperature` or `other`, below |
| `severity` | NWS CAP severity |
| `headline` | One line of what's happening. Home: Sentinel's `what`, else its `headline`. Field: NWS `parameters.NWSheadline`, else `headline` |
| `hazard` | The short HAZARD line. Home: Sentinel's `hazard`. Field: the same extraction, ported from Sentinel |
| `instruction` | NWS instruction text (can be long) |
| `onset`, `expires` | NWS times; `onset` may be `null`. **`expires` is when NWS will replace the message, not when the hazard ends** (live sample 16 November 2026: 189 of 588 active alerts were past it) |
| `ends` | When the hazard ends: NWS `ends`, else `parameters.eventEndingTime`; may be `null`. "Until" on the card and the Alerts page uses this, never `expires`; no `ends`, no "until". Field: from NWS. Home: Weather Sentinel's own `ends` (added by Sentinel 16 November 2026, `57813a4`, worked out the same way; always present, may be `null`) |

**Not passed through from Sentinel:** `level`, `acknowledged`, `needs_alert`, `last_transition`. Those belong to Sentinel's bedside unit. The console classifies and sorts alerts itself, and never sends anything to Sentinel.

**`hazard_class`: the hazard to the operating location decides priority, not the product type.** Matched on whole words in the event name, in this order:
1. `temperature`: heat, cold or wind chill. Checked first, so "Wind Chill" isn't taken for wind.
2. `site_hazard`: tornado, thunderstorm, wind, flood, hurricane, tropical storm, storm surge, or a Special Weather Statement.
3. `other`: everything else (Freeze Warning, Winter Storm Warning, Red Flag Warning, Dense Fog Advisory, ...).

The word lists live in one file in the service.

**Sorting:** warning → watch → advisory → statement, then soonest `ends` first, then soonest `expires` (neither: last).

**Which alerts are kept (changed 16 November 2026):** every alert the source lists as active. Never dropped because `expires` has passed (NWS keeps alerts active past it between reissues). Dropped by time only once `ends` has passed. Field Mode uses only `status: Actual` alerts and ignores `messageType: Cancel`.

### Conditions (`conditions`)

```json
"conditions": {
  "status": "ok", "source": "Weather Sentinel",
  "fetched_at": "2026-11-01T14:51:40Z", "issued_at": "2026-11-01T14:51:20Z", "age_sec": 50, "message": null,
  "station": null,
  "temperature_f": 71.2, "feels_like_f": 71.2, "humidity_pct": 64,
  "wind_mph": 9.1, "wind_dir": "WSW", "gust_mph": 18.0,
  "rain_rate_in_hr": 0.0, "pressure_inhg": 29.62
}
```

- **Home:** from Sentinel's `tempest` block; `station` is `null` (it's your own Tempest); `pressure_inhg` is station pressure.
- **Field (built 16 November 2026):** the nearest NWS observation station; `station` is `{"id", "name", "distance_mi"}` (NWS's own distance from the location, else worked out from the station's position); `rain_rate_in_hr` is `null`. The station list comes from the forecast's `/points` lookup (`observationStations`), checked once a day. If the nearest station's latest report is over 2 hours old or has no temperature, the next is tried (up to three); if none is good, the freshest found is shown and ages to stale. NWS units are metric and converted; a value that failed NWS quality control (`"X"`) is missing. `feels_like_f` is the wind chill when NWS gives one, else the heat index, else the temperature. `pressure_inhg` is NWS `barometricPressure`. `wind_dir` is `null` when calm.

### Lightning, Home Mode (`lightning`, from Weather Sentinel)

```json
"lightning": {
  "status": "ok", "source": "Weather Sentinel",
  "fetched_at": "2026-11-01T14:51:40Z", "issued_at": null, "age_sec": 20, "message": null,
  "within_10mi": true,
  "within_5mi": true,
  "frequent": false,
  "closest_mi": 3.5,
  "last_strike_at": "2026-11-01T14:51:32Z",
  "strikes_recent": 4, "window_min": 10,
  "hold_until_10mi": "2026-11-01T15:21:32Z",
  "hold_until_5mi": "2026-11-01T15:21:32Z",
  "hold_since_5mi": "2026-11-01T14:51:32Z",
  "cleared_at": null
}
```

| Field | Meaning | From |
|---|---|---|
| `within_10mi` | A strike inside 10 miles in the last 30 minutes | Sentinel's `close.active` (its 10-mile hold) |
| `within_5mi` | A strike inside 5 miles in the last 30 minutes | **The console's own hold**, below |
| `frequent` | More than 5 strikes a minute inside 10 miles | Sentinel's `close.strikes_last_minute` > `lightning_frequent_per_min`. `null` if Sentinel doesn't send the field (unknown, never `false`) |
| `closest_mi`, `last_strike_at`, `strikes_recent`, `window_min` | For the Weather page | Sentinel's `closest_distance_mi`, `most_recent_strike_at`, `strike_count_recent`, `window_minutes`. Sentinel counts within its own filter radius, not just 10 mi |
| `hold_until_10mi`, `hold_until_5mi` | When each hold ends ("Stay in until 10:21 AM") | 30 minutes after the last strike inside that distance |
| `hold_since_5mi` | When the current 5-mile hold began (new in 0.4) | The first strike of the hold; later strikes extend `hold_until_5mi` but don't change it. The alert list keys Acknowledge on it, so a hold flashes once |
| `cleared_at` | When the operator last cleared the holds, if within the last 24 hours (new in 0.4) | `POST /api/alerts/clear-hold` |

**The console's 5-mile hold.** A strike counts when Sentinel's most recent close strike (`close.last_distance_mi`, `close.last_strike_epoch`) is within `lightning_high_mi` (5). If only the window's closest strike (`closest_distance_mi`) is within 5 miles, its time is taken as Sentinel's most recent strike time (`most_recent_strike_epoch`, or the fetch time if Sentinel gives none). That is never earlier than the real strike, so the hold never ends too soon. The hold lasts `lightning_hold_min` (30) from the latest qualifying strike.

**The hold survives a service restart (new in 0.4).** The hold and any operator clear are saved on the Pi in `~/.local/state/hamops-console/lightning.json`, next to the Acknowledge file. A missing or damaged file is treated as empty and never stops the service.

**Operator clear (new in 0.4).** The operator may know from radar, the weather radio or someone on site that the storm has passed. **Clear hold** ends both holds early. Rules:
- **Separate from Acknowledge.** Acknowledge stops the flashing; the hold and its "stay in until" time remain. Clear ends the hold.
- **Two steps on screen:** Clear hold, then Confirm.
- **Both distances:** after a clear, `within_5mi`, `within_10mi` and `frequent` read `false`, and the hold times `null`, **until a new strike**. Sentinel's own 10-mile hold is not changed (the console never writes to Sentinel); the console only stops showing it.
- **Until the next strike only.** Any strike after `cleared_at` counts as new and starts a fresh hold that flashes again.
- **Recorded:** `cleared_at` is kept for 24 hours, and the Alerts page shows "Lightning hold cleared by operator at 3:20 PM (20:20 UTC)".

**In Sentinel's simulation:** `status` is `test`, every flag is `null`, and simulated strikes never start a hold.

### Lightning, Field Mode (`lightning`, GOES satellite; built 20 November 2026)

From NOAA's GOES Geostationary Lightning Mapper (GLM), read on the Pi from NOAA's public files (`sources.glm_east_url` / `glm_west_url`, chosen by `field_lightning.satellite`). **Detection is off until someone turns it on**, because it downloads about 60 MB an hour (Full) or 20 MB (Economy). Design agreed with Dan 12 and 20 November 2026.

```json
"lightning": {
  "status": "ok", "source": "GOES satellite",
  "fetched_at": "2026-07-04T22:41:20Z", "issued_at": "2026-07-04T22:40:40Z", "age_sec": 40, "message": null,
  "within_20mi": true, "within_10mi": false, "within_5mi": false, "frequent": false,
  "closest_mi": 14.2, "closest_bearing": "WSW",
  "last_strike_at": "2026-07-04T22:40:52Z",
  "strikes_recent": 6, "window_min": 10,
  "hold_until_20mi": "2026-07-04T23:10:52Z", "hold_until_10mi": null, "hold_until_5mi": null, "hold_since_5mi": null,
  "cleared_at": null,
  "detection": {
    "on": true, "level": "full", "satellite": "east", "satellite_name": "GOES-East",
    "turned_on_at": "2026-07-04T22:10:00Z", "outlook_seen": true, "auto_off_at": null,
    "off_waiting_for_hold": false, "question": null, "starting": false,
    "turned_off_at": null, "off_reason": null, "recent_at_off": null, "reader": true
  }
}
```

| Field | Meaning |
|---|---|
| `status` | The usual freshness, plus **`off`** while detection is off. Aged by the newest file's start time (`issued_at`); stale after `glm_stale_min` (3) with no new file (a minute more on Economy). `unavailable` until the first file arrives. A file that can't be read is skipped (and named in `message`), never retried endlessly |
| `within_20mi`, `within_10mi`, `within_5mi` | A flash inside `lightning_field_outer_mi` (20), `lightning_notify_mi` (10) or `lightning_high_mi` (5) in the last `lightning_hold_min` (30) minutes. **One flash is enough.** `null` (unknown, never `false`) while detection is off, or with no current satellite data. **A hold from a flash already seen stands whatever the freshness** (just turned on, just restarted, files late): with no current data the tiers it reaches are `true` and the others `null` |
| `frequent` | More than `lightning_frequent_per_min` (5) flashes inside 10 mi in the last minute |
| `closest_mi`, `closest_bearing` | The closest flash inside 20 mi in the last 30 minutes, with a 16-point compass direction from the site |
| `last_strike_at`, `strikes_recent`, `window_min` | The latest flash inside 20 mi, and how many in the last 10 minutes ("6 flashes in the last 10 min"). GLM counts **flashes** (all the light from one discharge, in cloud or to ground) |
| `hold_until_20mi`, `_10mi`, `_5mi`, `hold_since_5mi` | As at home: 30 minutes after the last flash inside each distance; `hold_since_5mi` is the first flash of the current 5-mile hold, so it flashes once per hold |
| `cleared_at` | The operator's Clear hold (as at home; `POST /api/alerts/clear-hold` works in both modes). A flash counts as new when it **arrives** after the Clear: files come 30–60 s after the flash, so one the operator couldn't yet have known about is never cleared away |
| `detection` | The switch: below |

**Where:** the centre of the Field station's grid (the GPS grid when the grid source is GPS). When the grid changes, the kept flashes are measured again from the new centre (each is kept with its position), so a hold survives a GPS grid moving to the next subsquare. Switching satellite starts from the newest file.

**Reading the files** needs `python3-netcdf4` on the Pi (apt). Without it nothing is downloaded and `detection.reader` is `false`; the block says why.

**On and off** (`detection`):
- **Turned on by hand** (`POST /api/lightning {"action": "on"}`, Field Mode only): the forecast card's Turn on lightning detection button, the Weather page or Settings. It starts with the newest file only; nothing older is fetched. For the first 2 minutes (`starting: true`) no "data late" card is raised.
- **With an NWS lightning outlook** (the forecast's `lightning_outlook`, which a Severe Thunderstorm Watch or Warning also sets): once one has been seen while on (`outlook_seen`), detection stays on until the outlook clears, then turns itself off (`off_reason` `outlook_cleared`). **Only a current forecast (status `ok`) with no outlook, while the NWS alerts are `ok` or `stale`, counts as clearing**: an outlook from a Watch disappears from the forecast when the alerts can't be read, and that isn't a clearing. An unavailable or stale forecast never turns detection off.
- **With no outlook** (Dan, 20 November 2026): stays on until turned off, at most `glm_auto_off_hours` (6, `auto_off_at`; `off_reason` `time_limit`). After `glm_ask_first_min` (30) `question` is `{"stage": "30min", "asked_at"}`: the screen asks "Lightning detection has been on for 30 min with no storms forecast. Keep it on?" with **Keep on** and **Turn off**. No answer: it stays on. After Keep on it asks once more at `glm_ask_again_hours` (3, `stage` `"3h"`), then turns itself off at 6 hours. An outlook arriving first ends the questions and the time limit. **The screen never puts the question over a flashing critical alert**: it waits until that alert is acknowledged or ends.
- **An automatic turn-off waits while lightning is near**: with a flash inside 20 mi in the last 30 minutes, `off_waiting_for_hold` is `true` and detection stays on until that hold clears. The console never switches its own lightning warning off.
- **Turned off by hand with lightning recent:** if flashes were inside 20 mi in the last 30 minutes, `recent_at_off` is `{"last_strike_at", "closest_mi", "closest_bearing", "until"}` and the alert list carries an amber `lightning_off_recent` card (§6). It goes when tapped (turn back on), dismissed (`dismiss_recent`), or at `until` (30 minutes after the last flash). It never reads as an all-clear.
- **Saved on the Pi** in `~/.local/state/hamops-console/field_lightning.json`: the switch, when it was turned on, the answers, the recent nearby flashes, the last file read and today's data count. A restart changes nothing; after one it catches up on files missed in the last 5 minutes. A missing or damaged file starts with detection off.

**`POST /api/lightning`** `{"action": ...}`, replies `no-store`:

| `action` | Does | Reply |
|---|---|---|
| `on` | Turns detection on (also ends the `recent_at_off` card) | 200 `{"detection": {...}}`; 409 in Home Mode |
| `off` | Turns detection off (the operator) | 200 `{"detection": {...}}` |
| `keep_on` | Answers the question | 200; 409 "No question waiting" |
| `dismiss_recent` | Dismisses the `recent_at_off` card | 200; 409 "Nothing to dismiss" |
| `home_test_on`, `home_test_off` | The at-home test, below | 200 `{"satellite_lightning": {...}}` (§7) |

Anything else: 400.

**At-home test** (Dan, 20 November 2026): `home_test_on` runs the same loop in **Home Mode**, at the centre of the home grid, so the satellite can be compared with the home Tempest. Its results appear only in `/api/system`'s `satellite_lightning.test` (§7), never in the weather blocks or the alert list. Switched with an SSH command for now:

```
curl -s -X POST -H 'Content-Type: application/json' -d '{"action": "home_test_on"}' http://127.0.0.1:8090/api/lightning
```

### NOAA Weather Radio (`weather_radio`, both modes; built 7 November 2026)

```json
"weather_radio": {
  "status": "ok", "source": "NWS NWR listing (stored on the Pi)", "fetched_at": "2026-11-07T15:00:00Z", "issued_at": null, "age_sec": 3600, "message": null, "updating": false,
  "county": "Example County, VA", "same_code": "051000",
  "nearest": [
    {"callsign": "WXX00", "frequency_mhz": 162.550, "site": "Example City", "distance_mi": 18.2,
     "covers_county": true, "county_part": null, "status": "NORMAL"}
  ]
}
```

- Which NOAA Weather Radio transmitter to tune for the console's location, and the county's SAME code for programming a weather radio.
- **Stored on the Pi** (`~/.local/state/hamops-console/nwr_stations.json`, a compact copy), so it works in the field with no internet. Downloaded from NWS (`https://www.weather.gov/source/nwr/JS/CCL.js`, about 2.5 MB) in the background when there is no copy yet, then **once a week** (Dan, 7 November 2026: sites and frequencies rarely change); a failed download keeps the stored copy and is retried after 6 hours. **On demand:** `POST /api/weather-radio/refresh` starts a download at once, e.g. before a field event (202 `{"started": true}`; 409 while one is already running); `updating` is `true` while a download runs. `fetched_at` is when the stored copy was downloaded; the Weather page shows its age ("List · 3 days"). The block is `stale` when the copy is over 30 days old (still shown) and `unavailable` only when there is no copy yet or the station grid isn't valid.
- **County:** from the NWS points lookup the forecast already makes (`location.nws_county`, e.g. `VAC059` → SAME `051059`). `same_code` is the whole-county code. Until NWS has answered at this location, `county` and `same_code` are `null`.
- **`nearest`** (the first entry is the one to tune; distances from the centre of the station grid):
  1. Transmitters NWS lists for the county, working before degraded, nearest first (`covers_county: true`). A transmitter not listed for the county doesn't carry its SAME-coded warnings, so a listed one comes first even when another is nearer.
  2. Then the nearest other working transmitters (`covers_county: false`), so there are always three working ones to try: alternates if the first can't be heard, or first in the list when the county has none working (Dan, 7 November 2026). Then up to two of the county's out-of-service ones, shown as such.
  3. With no county known yet, the three nearest working transmitters (`covers_county: null`). This works offline: the stored list has each transmitter's position.
  - The Weather page shows the first entry large and the next two working ones on one line: "Or 162.550 WXX01 · 162.400 WXX02".
  - `county_part` is NWS's note on which part of the county a transmitter covers (e.g. "NW 1/2"), or `null`. `status` is NWS's NORMAL, DEGRADED or OUT OF SERVICE.
  - **Out-of-service transmitters are shown as such and never come first.**
  - `message` explains cases 2 and 3 ("The county's own transmitter is out of service", "NWS lists no weather radio coverage for this county", "County not known yet: nearest transmitters, county coverage not confirmed").
- The NWS-alerts outage cards (§6) name the station: "… Not the same as no alerts. Tune NOAA Weather Radio WXX00, 162.550 MHz." They name it only when the first entry is working and isn't marked `covers_county: false`; otherwise they say "Tune NOAA Weather Radio."

### Forecast (`forecast`, both modes)

```json
"forecast": {
  "status": "ok", "source": "NWS",
  "fetched_at": "2026-11-01T14:30:00Z", "issued_at": "2026-11-01T11:21:00Z", "age_sec": 12310, "message": null,
  "thunder_next": {"start": "2026-11-01T19:00:00Z", "coverage": "chance"},
  "lightning_outlook": {"level": "possible", "start": "2026-11-01T19:00:00Z", "basis": "forecast"},
  "hourly": [
    {"time": "2026-11-01T15:00:00Z", "temperature_f": 72, "rain_chance_pct": 20,
     "wind_mph": 10, "gust_mph": 20, "wind_dir": "WSW", "thunder": null, "lightning": null, "short": "Partly Sunny"}
  ],
  "periods": [
    {"name": "This Afternoon", "start": "2026-11-01T15:00:00Z", "end": "2026-11-01T22:00:00Z",
     "temperature_f": 74, "rain_chance_pct": 40, "wind": "10 to 15 mph WSW",
     "short": "Chance Showers And Thunderstorms", "detail": "A chance of showers and thunderstorms after 3pm..."}
  ]
}
```

- **From NWS** for the grid's centre: the 12-hour forecast, the hourly forecast and the gridpoint data. **All three must arrive** or the last good set is kept. That way a missing gridpoint can never read as "no thunderstorms".
- `issued_at` is NWS `updateTime`, not `generatedAt`. NWS revises a few times a day, so an `updateTime` several hours old is normal.
- `hourly`: the next 12 hours. `gust_mph` comes from the gridpoint `windGust` series. `thunder` is the NWS coverage word for that hour, or `null`. **`lightning` (new 7 November 2026)** is that hour's plain level, `"likely"`, `"possible"` or `null`, by the same rule as `lightning_outlook`, so screens never interpret coverage words themselves.
- `periods`: the next 4 twelve-hour periods. `wind` is NWS's own text (it can be a range) with the direction.
- Hours and periods already over are dropped.
- **Thunderstorms come from the gridpoint `weather` series only.** `probabilityOfThunder` was empty even with thunderstorms forecast, so it isn't used. The series' times are intervals (`start/PT12H`), and one interval can hold several weather items.
- `thunder_next`: the first hour in the next 12 with thunderstorms, and **NWS's own coverage word** (`slight_chance`, `chance`, `isolated`, `scattered`, `likely`, `numerous`, `widespread`, `definite`; note the underscore). `null` if none.
- **`lightning_outlook`** turns that into plain wording, always shown as a forecast, never as detection ("Lightning likely after 3 PM — NWS forecast, not detected"):
  - `level` is `likely` if the coverage anywhere in the next 12 hours is likely, numerous, widespread or definite, otherwise `possible`. It is `null` when no thunderstorms are forecast.
  - `start` is **the first hour at that level**. An earlier slight chance doesn't make "likely" start sooner, so it can't trigger "lightning likely within 2 hours" early. `thunder_next` still gives the first thunder hour of any kind.
  - `basis` is `forecast`, or `nws_alert` when an active **Severe Thunderstorm Warning or Watch**, or a **Special Weather Statement mentioning thunderstorms or lightning**, makes it `likely` from now. This applies even when the forecast shows nothing, but not while Sentinel is in test mode or while the alerts are unavailable.

---

## 6. Alerts — `GET /api/alerts`

The ranked list behind the Priority Card and the Alerts page, built by the service from the other blocks. The ranking lives in one tested place, not in screen code.

```json
{
  "mode": "home",
  "generated_at": "2026-11-01T14:52:10Z",
  "items": [
    {
      "rank": 1,
      "category": "hazard_warning",
      "tier": "card",
      "color": "red",
      "critical": true,
      "acknowledged": false,
      "title": "Severe Thunderstorm Warning",
      "detail": "60 mph wind gusts and quarter size hail. Until 15:36 UTC (10:36 AM).",
      "since": null,
      "until": "2026-11-01T15:36:32Z",
      "kind": "warning",
      "instruction": "For your protection move to an interior room on the lowest floor of a building.",
      "ref": {"block": "weather.alerts", "id": "urn:oid:2.49.0.1.840.0.example"}
    },
    {
      "rank": 2,
      "category": "lightning_5mi",
      "tier": "card",
      "color": "red",
      "critical": true,
      "acknowledged": false,
      "title": "Lightning 3.5 mi away",
      "detail": "4 strikes in 10 min. Stay in until 10:21 AM (15:21 UTC).",
      "since": "2026-11-01T14:51:32Z",
      "ref": {"block": "weather.lightning", "id": "lightning-5mi-2026-11-01T14:51:32Z"}
    }
  ],
  "ended": [
    {"id": "urn:oid:2.49.0.1.840.0.example2", "title": "Special Weather Statement", "category": "hazard_alert",
     "kind": "statement", "color": "yellow", "until": "2026-11-01T14:30:00Z", "ended_at": "2026-11-01T14:10:00Z"}
  ],
  "nws_alerts_status": "ok"
}
```

- The Priority Card shows the first `card` item, with ‹ › and "1 of N" across the other card items. The Alerts page lists everything, card and badge items alike.
- **Alerts page and the `ended` list (new 10 November 2026).** The reply also carries `"ended": [...]` and `"nws_alerts_status"` (the NWS alerts block's status: `ok`, `stale`, `unavailable` or `test`). `ended` holds NWS alerts that ended in the last 24 hours, newest first: `{"id", "title", "category", "kind", "color", "until", "ended_at"}`. An NWS alert counts as ended when it leaves the list **while the NWS alerts block is `ok`**; while it is stale, unavailable or in test mode nothing is concluded, because a missing alert there may simply not have been fetched, and the page says "Can't tell while NWS alerts are out of date." instead of "None". `ended_at` is the alert's own expiry if that has passed, otherwise when it was seen gone (NWS ended it early); an alert that comes back (extended) leaves `ended`. Kept on the Pi in `~/.local/state/hamops-console/ended.json`, so a restart doesn't forget it. NWS items (`ref.block` `weather.alerts`) also carry `kind` (`warning`, `watch`, `advisory`, `statement`), for the page's label, and `instruction` (NWS's advice, or `null`), for its Details panel.
- **The Alerts page** (approved mockup "Alerts page") draws three lists: the `card` items worst first (label, title with the NWS "until" time, a line naming the source, Acknowledge on unacknowledged critical items, Details on each); the `badge` items, marked "new" until the page has shown them once; and `ended`. Details opens a panel with the full detail text, NWS's instruction, the from/until times in local and UTC, Acknowledge, and the two-tap Clear hold for lightning items. Every label is a word as well as a colour (CRITICAL, WATCH, ADVISORY, STATEMENT, NWS DOWN, LOCAL, FORECAST, SOURCE, TEST, BANDS, QUIET, HEAT, COLD, WARNING). When the NWS list is out of date, its items say "NWS list out of date". The list scrolls with a finger when it doesn't fit. The Alerts button on every screen opens it.
- **`until` (new 7 November 2026):** NWS alert items only (`ref.block` `weather.alerts`) carry the alert's NWS expiry, or `null`, so the Weather page can show a short "Until 3:04 PM" line instead of the full detail. Other items have no `until` key.
- **Detail pages go back to Home (proposed 7 November 2026, with the Weather page; Dan to confirm on the Pi):** after 3 minutes untouched, and at once when a new unacknowledged critical item arrives, so a warning is never hidden behind a detail page. Each critical item does this once: one already shown on the Home screen, or already sent Home for, doesn't bounce the operator back again. Screen behaviour only.
- **Rotation (changed 5 November 2026; replaces "no auto-scroll"):** with no critical item in the list, the card moves to the next card item every 60 seconds and wraps round. A critical item (always ranked first) pins the card on it until it ends, even after Acknowledge; with two, it stays on the worse one. ‹ › still page; touching them pauses rotation, and 30 seconds after the last touch the card returns to the first item and rotation carries on. A single item, or "All quiet.", never rotates. A new first item jumps to the front. This is screen behaviour; the service's ranking is unchanged by it.
- **`tier`**: `card` items go on the Priority Card. `badge` items are counted on a small badge on the Alerts button, coloured by the worst one; the badge blinks for about 10 seconds when a new item arrives, then stays steady until the Alerts page is opened.
- **`critical`**: flash the border (about once a second) and show Acknowledge. Flashing stops when acknowledged or after 2 minutes; the item stays on top until it ends.
- **The list is never empty.** When nothing is wrong it holds one `all_quiet` card ("All quiet.", with "20m is open." or similar when bands are good). `all_quiet` can appear alongside badge items.
- Card items come first in the order below, then badge items. Within a category, the newest `since` comes first.

### Ranking

| Order | `category` | When | Colour | Tier | Critical |
|---|---|---|---|---|---|
| 1 | `hazard_warning` | NWS warning with `hazard_class` = `site_hazard` | red | card | yes |
| 2 | `lightning_5mi` | `within_5mi` (Home) | red | card | yes, once per hold (`ref.id` carries `hold_since_5mi`) |
| 3 | `alerts_unavailable_storms` | NWS alerts `unavailable` or `stale`, **and** a `lightning_outlook` starting within 6 h (`storms_forecast_hours`) | red | card | no |
| 4 | `hazard_alert` | NWS watch, advisory or statement with `hazard_class` = `site_hazard` | yellow | card | no |
| 5 | `lightning_likely_soon` | `lightning_outlook.level` = `likely` starting within 2 h | yellow | card | no |
| 6 | `lightning_frequent` | `frequent` (both modes) | yellow | card | no |
| 7 | `lightning_10mi` | `within_10mi` (both modes) | yellow | card | no |
| 8 | `lightning_20mi` | `within_20mi` (Field, GOES satellite; 20 November 2026) | yellow (amber) | card | no |
| 9 | `lightning_off_recent` | Field detection turned off with `recent_at_off` (§5); carries `"action": "lightning_on"` | yellow | card | no |
| 10 | `lightning_outlook` | `lightning_outlook` possible, or likely more than 2 h out | yellow | card | no |
| 11 | `source_down` | A source stale, unavailable, **or in test mode** (one item per source). **The NWS alerts card comes first among these** (5 November 2026) | yellow | card | no |
| 12 | `propagation` | Notable propagation (below) | green (good) / yellow (bad) | card | no |
| 13 | `all_quiet` | No other card item | green | card | no |
| — | `temperature_alert` | Any NWS product with `hazard_class` = `temperature` | yellow | badge | no |
| — | `other_warning` | NWS warning with `hazard_class` = `other` | blue | badge | no |
| — | `other_notice` | NWS advisory or statement with `hazard_class` = `other` | blue | badge | no |

- **One detected-lightning item at a time:** the highest that applies (5 mi beats frequent beats 10 mi beats 20 mi). Home lightning data is used only when its block is `ok`; **Field (GOES) lightning whenever detection is on**, because the block sets a tier only from flashes actually seen and a flash already seen keeps its hold when newer files are late. One forecast-lightning item can appear as well.
- **Field lightning wording (20 November 2026):** `lightning_20mi` is "Lightning within 20 mi" with "14 mi WSW at 6:41 PM (22:41 UTC). 6 flashes in the last 10 min. GOES satellite · check your lightning app."; the 10- and 5-mile items read "Lightning 8.2 mi WSW" with the same flash count, the Wait / Stay in time and the same last line. The 5-mile item is critical and flashes once per hold, as at home.
- **Field lightning switched off:** while detection is off, the forecast-lightning item carries `"action": "lightning_on"` (the Turn on lightning detection button on the card). A switched-off block (`status` `off`) never makes a `source_down` card. When detection is on and its files are late, the `source_down` card is "Lightning data late" (stale) or "Lightning data unavailable", with "Not the same as no lightning."; none while `detection.starting`.
- **The keep-on question is not an alert item.** It is `lightning.detection.question` (§5); the screen shows it as a popup, never over a flashing critical alert.
- **Stale NWS alerts are still ranked**, beside a `source_down` card saying the alerts are stale. After 30 minutes they're unavailable and drop out.
- **NWS alerts outage (5 November 2026).** When the `alerts` block is stale or unavailable, its `source_down` card is titled "NWS alerts out of date" or "NWS alerts unavailable" (not the source's name, since in Home Mode Sentinel may be answering while it can't reach NWS), its detail is the block's `message` followed by "Not the same as no alerts.", its `ref.id` is `source-nws-alerts`, and it comes before the other `source_down` cards whatever their `since`. Other blocks from the same source fold into that one card, as before.
- **Sentinel test mode** shows as a `source_down` card titled "Home weather station in test mode". Otherwise the card could read "All quiet." while real alerts can't pass through.
- **Notable propagation** is judged only when the propagation block is `ok`. Bad (yellow): K-index of `prop_bad_k_index` (5) or higher, or an X-ray flare of a class in `prop_bad_xray_classes` (M, X). Good (green): `prop_good_min_bands` (2) or more bands rated good. All tunable in Settings.

### Acknowledge — `POST /api/alerts/ack`

`{"id": "<ref.id>"}`. The console's own Acknowledge, saved on the Pi in `~/.local/state/hamops-console/acks.json` and kept 48 hours, so a restart doesn't make an acknowledged item flash again. Only an item currently in the list can be acknowledged (otherwise 404); a missing or bad `id` gives 400. Never forwarded to Weather Sentinel. Returns `{"acknowledged": "<id>"}`.

### Clear lightning hold — `POST /api/alerts/clear-hold` (new in 0.4)

No body. Ends both lightning holds early, on the operator's word, until the next strike (see §5 Lightning). In Field Mode it clears the GOES satellite holds. It's allowed only while a lightning item (`lightning_5mi`, `lightning_frequent`, `lightning_10mi` or, in Field Mode, `lightning_20mi`) is in the list; otherwise it returns 409, "No lightning hold to clear". Returns `{"cleared_at": "<time>"}`. The dashboard asks for confirmation first. Never forwarded to Weather Sentinel.

---

## 7. System — `GET /api/system`

```json
{
  "mode": "home",
  "generated_at": "2026-11-01T14:52:10Z",
  "system": {
    "status": "ok", "source": "Pi", "fetched_at": "2026-11-01T14:52:10Z", "issued_at": null, "age_sec": 0, "message": null,
    "hostname": "hamops",
    "uptime_sec": 86400,
    "cpu_temp_c": 48.3,
    "cpu_load_pct": 6,
    "memory_used_pct": 14, "memory_used_gb": 1.1, "memory_total_gb": 7.9,
    "disk_used_gb": 4.5, "disk_total_gb": 457,
    "power": {"undervoltage_now": false, "throttled_now": false, "undervoltage_since_boot": false},
    "network": {"interface": "wlan0", "connection": "home-wifi", "ip": "192.0.2.10", "wifi_signal_pct": 68},
    "clock": {"source": "ntp", "synced": true},
    "rtc": {"battery_v": 2.98, "charging": true},
    "gps": {"present": true, "fix": "3d", "satellites_used": 4, "satellites_seen": 12,
            "grid": "FM18lv", "last_fix_at": "2026-11-01T14:52:05Z"},
    "service": {"version": "0.1.0", "started_at": "2026-10-31T14:52:10Z"}
  }
}
```

- `power` comes from `vcgencmd get_throttled`.
- `network.connection` is the NetworkManager profile name (e.g. `hotspot` in the field), never a password.
- `clock.source` is `ntp`, `gps` or `rtc`. GPS time is used only when it's plausible against the RTC.
- **`GET /api/gps` (built 18 November 2026): the GPS details panel** (approved mockup "GPS details", Dan 18 November 2026), opened by tapping the GPS light in the status bar on any screen, or "Details ›" on the System page's GPS row; it reads this endpoint every 2 s while open. Reply: `{"generated_at", "gps": {...}}` with everything in the `gps` block below plus `satellites` (`[{"prn", "az", "el", "ss", "used"}]`: direction and height in degrees, signal in dB-Hz, whether used in the fix; empty when the receiver isn't reporting), `hdop`, `lat` / `lon` (5 places, only with a fix; held in memory only, never saved), `alt_m` (only with a 3D fix), `accuracy_m` (gpsd's horizontal error estimate), `gps_time`, `searching_since` (when the receiver started reporting this time), `first_fix_sec` (how long the first fix took), `gpsd_release` and `device` (`{"path", "driver", "bps"}`). The panel shows a sky plot (centre overhead, outer ring the horizon; green = used, grey ring = heard), satellites used/heard, the grid in use and where it came from, position (Dan: keep latitude and longitude), altitude in metres with feet, accuracy and HDOP, GPS time against the Pi clock, last and first fix, the receiver, and one signal bar per satellite (30 dB-Hz and up is good); with no fix, an amber border and what to try.
- **GPS (built 18 November 2026):** from gpsd. `present` (reports in the last 10 s, or the receiver's device exists), `fix` (`"2d"`, `"3d"` or `null`; a fix older than 10 s is no fix), `satellites_used` / `satellites_seen`, `grid` (the accepted GPS grid, kept through a lost fix), `last_fix_at`, plus `reporting`, `grid_accepted_at`, `grid_confirmed` (a fix seen since the service started), `position_grid` (the subsquare of the current fix), `grid_pending` (a new subsquare waiting out its 2 minutes), `time_offset_sec` and `time_check` (`"agrees"` within 2 s, `"differs"`, `"gps_wrong"` when over a day apart, or `null` without a fix), and `message` ("gpsd isn't answering", "No GPS receiver reporting", "No fix yet", or `null`). **The console never sets the clock from GPS** (Dan, 18 November 2026): the RTC keeps time with no internet, and this receiver (BU-353S4, SiRF IV) reports a made-up date (2031) before its first fix, so GPS time is only compared, and only with a fix. The System page adds "GPS agrees" / "GPS differs by N s" / "GPS date wrong, ignored" to the Clock row; `clock.source` stays `ntp` or `rtc`.
- Readings the service can't take (e.g. off the Pi) are `null`.
- **Added 20 November 2026: the `satellite_lightning` block**, beside `system`: what the GOES satellite loop is doing and the data it has used (Dan asked for a data counter).
  `"satellite_lightning": {"running": "field" | "home_test" | null, "home_test": false, "home_test_since": null, "level": "full", "satellite": "east", "satellite_name": "GOES-East", "reader": true, "status": "ok", "message": null, "newest_file_at": "...", "mb_today": 412.6, "mb_last_hour": 58.9, "files_today": 1250, "test": null}`.
  `mb_today` is since local midnight (saved, so a restart doesn't zero it); `mb_last_hour` is the last 60 minutes since the service started. During the at-home test, `test` holds that test's own `within_20mi`, `within_10mi`, `within_5mi`, `closest_mi`, `closest_bearing`, `last_strike_at`, `strikes_recent` and `window_min`, for the System page.
- **Changed 10 November 2026 (System page):** added `memory_used_gb` / `memory_total_gb`, `network.wifi_signal_pct` (0–100, from NetworkManager's last scan with `--rescan no`, so reading it never starts a scan; `null` off Wi-Fi) and `rtc` (the Pi 5's real-time-clock backup battery: `battery_v` in volts and whether `charging` is on, read from the RTC's sysfs folder). `network.internet` and `network.home_sources_reachable`, drawn in earlier drafts, were never built: the screens judge the internet by whether NWS answers (the forecast block) and the home sources by their own blocks.
- **Added 11 November 2026: the `display` block**, beside `system` in the same reply:
  `"display": {"control": true, "device": "<backlight name>", "asleep": false, "night": false, "brightness_pct": 80, "message": null}`.
  `control` is false when the backlight's `brightness` file can't be written; `message` then gives the reason. (On the Pi, 11 November 2026: `panel_backlight@1`, max 31; Raspberry Pi OS already lets the `video` group write `brightness`, and the udev rule in `pi/udev/` adds `bl_power`, which Sleep uses when it can.) `night` is whether the night times in Settings apply now; `brightness_pct` is what the screen should be at (0 asleep). The service sets the backlight itself: day or night brightness from Settings, checked every 30 seconds and at once after a Settings change. Night style: **Dim** changes only the backlight; **Red** (built 12 November 2026) also shows the whole page in red only during the night times — a colour filter on the page turns every colour into its brightness in red, so alerts rely on their words and the flashing border (as approved 2026-10-12). Settings' Display tab has a 10-second Preview Red button.
- **`POST /api/display`** `{"action": "sleep" | "wake"}` → 200 `{"display": {...}}`. Sleep turns the backlight off; the dashboard covers the page in black so the touch that wakes it doesn't also press a button. Sleep is remembered by the service (a page loaded while asleep covers itself) and a new unacknowledged critical item wakes the screen. 400 for any other action.
- **`POST /api/power`** `{"action": "shutdown" | "restart"}` runs `systemctl poweroff` or `systemctl reboot` → 200 `{"started": "shutdown"}`; 503 `{"error": "..."}` when the Pi refuses (until the polkit rule in `pi/polkit/` is installed: "This Pi doesn't allow it yet (the polkit rule isn't installed)."). The dashboard asks first, with the approved Shut down dialog: large Cancel on the left, Restart in the middle, red Shut down on the right, and "Wait for the screen to go dark before unplugging power."
- **Caching (11 November 2026):** pages, scripts and styles are sent with `Cache-Control: no-cache`, so Chromium checks with the service each time and a `git pull` plus service restart shows on the screen without a reboot. JSON replies keep `no-store`.
- **The System page** (approved mockup "System page") shows three tiles: THIS PI (CPU temperature, amber "warm" from 75 °C and red "hot" from 80 °C, where the Pi 5 starts slowing itself down; load; memory; disk; power from `vcgencmd`, red for under-voltage now, amber for throttling or under-voltage since boot; uptime; version and when the service started), NETWORK · LOCATION · TIME (Wi-Fi profile and signal; internet, judged by whether NWS answers; location source and grid; GPS; clock; backup-clock battery) and DATA SOURCES (PropMon, Weather Sentinel — the worst of its three blocks — the console service, the NWS forecast and the stored weather radio list, each with its age or its state and reason). Its System button is live on every screen.

---

## 8. Settings — `GET` / `PUT /api/settings`, `POST /api/settings/test`

### `GET /api/settings`

Everything the Settings screen may show and change. The service's listening address (`server`) is not included and can't be changed from the screen.

```json
{
  "mode": "home",
  "station": {
    "home":  {"callsign": "N0CALL", "grid_source": "manual", "grid": "FM18lv"},
    "field": {"callsign": "N0CALL", "grid_source": "manual", "grid": "FM18lw"}
  },
  "display": {"main_clock": "utc", "night_style": "dim", "day_brightness_pct": 80, "night_brightness_pct": 20,
              "night_start": "22:00", "night_end": "07:00"},
  "sources": {"propmon_url": "http://propmon.example:8076", "sentinel_url": "http://sentinel.example:8085"},
  "thresholds": {
    "propagation_stale_min": 15, "propagation_fetch_min": 5,
    "sentinel_fetch_min": 1, "nws_alerts_stale_min": 10, "nws_alerts_unavailable_min": 30,
    "conditions_stale_min": 5, "lightning_stale_min": 5, "station_obs_stale_min": 90,
    "nws_alerts_fetch_min": 2, "station_obs_fetch_min": 15,
    "forecast_fetch_min": 30, "forecast_stale_hours": 12,
    "lightning_notify_mi": 10, "lightning_high_mi": 5, "lightning_hold_min": 30,
    "lightning_frequent_per_min": 5, "storms_forecast_hours": 6,
    "prop_bad_k_index": 5, "prop_bad_xray_classes": ["M", "X"], "prop_good_min_bands": 2
  },
  "nws_user_agent_contact": "contact@example.com"
}
```

- Stored on the Pi in `~/.config/hamops-console/settings.json`, outside the repository, so no real addresses or contact details are ever committed. The repository carries `config/settings.example.json` with placeholders.
- `nws_user_agent_contact`: NWS asks every application to identify itself with a contact. It's sent in the `User-Agent` header: `HamOpsConsole/<version> (<contact>)`.
- `grid_source`: `manual`, or `gps` for the **Field** station only (18 November 2026; `gps` for home is refused: "GPS is for the Field station only"). With `gps` the station's `grid` is kept as the fallback until the GPS has a position.

### `PUT /api/settings`

Send **only what changes**, e.g. `{"station": {"home": {"grid": "FM18lv"}}}`.

- **Every value is checked first. If anything is wrong, nothing is saved.** The reply is 400 with every problem listed: `{"errors": ["mode must be 'home' or 'field'", ...]}`.
- On success: 200 with the saved settings (as `GET`) plus `"warnings": [...]`, e.g. for a 4-character grid.
- Callsigns are upper-cased; grids are written `FM18lv` style.
- Ranges are checked for every threshold; `lightning_high_mi` can't exceed `lightning_notify_mi`, which can't exceed `lightning_field_outer_mi`; and `glm_ask_first_min` < `glm_ask_again_hours` < `glm_auto_off_hours`.
- **Field lightning (20 November 2026):** `field_lightning {"level": "full" | "economy", "satellite": "east" | "west"}` (defaults Full, East). Turning detection on and off is **not** a setting: it is `POST /api/lightning` (§5), shown on the Settings tile. `sources` gains `glm_east_url` (`https://noaa-goes19.s3.amazonaws.com`) and `glm_west_url` (`https://noaa-goes18.s3.amazonaws.com`): when NOAA moves a satellite, change the address. `thresholds` gain `lightning_field_outer_mi` (20, 5–30), `glm_stale_min` (3, 1–30), `glm_auto_off_hours` (6, 1–24), `glm_ask_first_min` (30, 5–240) and `glm_ask_again_hours` (3, 1–12).
- The file is written to a new copy and swapped in, so a power cut can't leave it half-written. Comments (`_comment` keys), other entries such as `server`, and the file's permissions are kept. A damaged file is set aside as `settings.json.broken` rather than overwritten.
- **Changes take effect at once, with no restart.** A new grid triggers a fresh NWS lookup.

### `POST /api/settings/test` — Test connections

Optional body `{"sources": {"propmon_url": "...", "sentinel_url": "..."}}` tests addresses before saving them.

```json
{"results": [
  {"source": "PropMon", "url": "http://propmon.example:8076/api/v2/conditions", "ok": true, "ms": 25, "message": null},
  {"source": "Weather Sentinel", "url": "http://sentinel.example:8085/api/conditions", "ok": true, "ms": 7, "message": null},
  {"source": "NWS", "url": "https://api.weather.gov/alerts/active/count", "ok": true, "ms": 450, "message": null}
]}
```

NWS is tested against a small JSON endpoint (its home page is HTML). `message` explains a failure: "address not set", "not answering (URLError)", "answered with an error (503)", "answered, but not with JSON".

### The Settings screen (`/settings`, built 11 November 2026)

The screen in the approved tabs uses only the three calls above plus `POST /api/weather-radio/refresh`. Every change is sent at once as its own `PUT`; there is no separate Save step. The service's reasons for refusing a change are shown where it was made (in the keyboard, or on the footer line), and nothing is saved.

- **Station & profile:** callsign and grid for the current mode, typed on the on-screen keyboard. Data source Field is live since 17 November 2026 (it was disabled, "Phase 3", until Field propagation existed), GPS as the grid source live for the Field station since 18 November 2026 (a line under it shows the GPS grid, fix and satellites, or that it is waiting for a position and using the typed grid, labelled "Typed grid"; for Home the GPS button reads "Field only"), and the Field Day profile and economy mode "Coming"/"Phase 3". A 4-character grid shows the coarse-grid warning.
- **Display:** the main clock (working), day and night brightness, night style and night times. Brightness and night style are saved now; the screen follows them once brightness control is built (with Sleep and Shut down).
- **Data sources:** the PropMon and Weather Sentinel addresses, Test connections, and the weather radio list's age with Update now (202 starts it; 409 means one is already running).
- **Wi-Fi:** saved networks in preference order (up/down, Forget with two taps), nearby networks from the last scan with Scan again, and Join with the password typed on the on-screen keyboard (built 17 November 2026; see §10). Needs `pi/polkit/50-hamops-wifi.rules`.
- **Addresses** must be `http(s)://host[:port][/path]` with no spaces and a numeric port (tightened 11 November 2026, since addresses are now typed on the touchscreen).

---

## 9. Mode-by-mode source map

| Contract block | Home Mode | Field Mode |
|---|---|---|
| propagation | PropMon | HamQSL + NOAA SWPC, rated on the Pi by Ham Shack Automation's field engine (17 Nov 2026) |
| weather.alerts | Weather Sentinel `nws` | NWS `/alerts/active?point=` for the Field grid's centre |
| weather.conditions | Weather Sentinel `tempest` | Nearest NWS observation station |
| weather.lightning | Weather Sentinel `lightning` | GOES satellite (GLM), switched on by hand (20 Nov 2026); `status` `off` until then |
| weather.forecast | NWS forecast for the home grid | NWS forecast for the field grid |
| weather.weather_radio | Stored NWR list, home location | Stored NWR list, field location |
| alerts | Built by the service | Built by the service |
| system | The Pi | The Pi |

In Field Mode the home station is never asked. Field Mode alerts and conditions were built 16 November 2026; before then they were `unavailable` ("Field Mode weather is not built yet").

---

## 10. Decisions and future work

**Decided (1 November 2026):** priority by hazard to the operating location; lightning notice at 10 miles, high priority and flashing at 5 miles, frequent-strike flag; heat/cold and other NWS products on the badge; forecast lightning wording ("possible"/"likely", never detection); NOAA Weather Radio nearest transmitter stored on the Pi; notable propagation thresholds; bands good → fair → poor; PropMon's tower fields not used; NWS forecast in Home Mode; Sentinel's hazard-line routine ported for Field Mode.

**Decided (5 November 2026):**
- Contracts approved for building (this version).
- The lightning holds survive a restart, and the operator can clear them until the next strike (§5, §6).
- Sentinel test mode stays a yellow Priority Card item.
- Priority Card rotation every 60 s unless a critical item pins it, and an NWS-alerts outage first among the data-source cards, worded as such (§6; built the same evening).
- Economy mode in Phase 3 (§2).
- **Settings names:** "Data source: Home / Field" (the mode in these contracts) and **"Operating profile: General HF / Field Day"**, kept separate so Field Mode isn't confused with Field Day.

**Decided (16 November 2026):** alert times follow NWS CAP meaning. `ends` added to alert items; "until" and the Ended list's end time use `ends`; alerts are no longer dropped when `expires` passes, in either mode (§5). Field Mode NWS alerts (exact point) and nearest-station conditions built; settings gain `thresholds.nws_alerts_fetch_min` (2) and `thresholds.station_obs_fetch_min` (15).

**Decided (17 November 2026):** Field Mode propagation built on Ham Shack Automation's field engine (§4), with its DX scale and, under the Field Day profile, its Field Day scale (North America reach; Dan accepted Ham Shack Automation's offer the same day, replacing the "FAIR or better = open for North America" stopgap of 16 November). The engine copy carries no home coordinates: its EM83 example and test cases use the centre of EM83 (Dan). Version check `engine` in the propagation block (§4). Settings gain `thresholds.field_propagation_fetch_min` (10) and `sources.field_engine_url`; Data source Field is enabled. Help gains Ham Shack Automation's FAIR line and the Field Day meanings.

**Decided (20 November 2026): Field lightning from the GOES satellite** (§5, §6, §7, §8), as agreed 12 November with Dan's additions: a hand turn-on with no NWS outlook stays on until turned off, at most 6 hours, asking "keep it on?" at 30 minutes and once more at 3 hours after Keep on (no answer: stays on; never over a flashing critical alert; an outlook arriving first switches to the outlook rule); the switch survives a restart; an at-home test switched by an SSH command, results on the System page only. Added in building, for Dan to confirm: an automatic turn-off waits while a flash is inside 20 mi in the last 30 minutes; only a current forecast with no outlook (and NWS alerts readable) counts as the outlook clearing; Field lightning holds stay shown while the satellite data is late or missing; a flash arriving after the operator's Clear counts as new.

**Wi-Fi from the touchscreen (built 17 November 2026; mockups approved 5 November).** The Settings Wi-Fi tab. The service runs `nmcli` as user `hamops`; saving, joining and scanning need `pi/polkit/50-hamops-wifi.rules` (NetworkManager's `settings.modify.system`, `network-control` and `wifi.scan` for `hamops` only; the radio on/off stays blocked). Without the rule the tab still lists networks and says why it can't change them.
- `GET /api/wifi` → `{"wifi": {"available", "can_change", "message", "permissions": {"scan", "modify", "control"}, "saved": [{"id", "ssid", "rank", "active", "in_range", "signal", "bars"}], "nearby": [{"ssid", "signal", "bars", "security", "open", "enterprise", "saved_id"}], "scanning", "scanned_at", "scan_error", "job": {"ssid", "state": "joining" | "joined" | "failed", "message"} | null}}`. Saved networks are Wi-Fi connections only, first choice first (NetworkManager's `autoconnect-priority`, higher first); `id` is the connection's UUID. Nearby is NetworkManager's last scan (reading it never starts one), one row per name, strongest first; networks with no name (hidden) are left out.
- `POST /api/wifi/scan` (202; 409 while scanning). `POST /api/wifi/join` `{"ssid", "password"?}` (202, runs in the background; the result appears in `job`): a saved network is simply brought up; a new one is saved and joined, then placed **last** in the order (Dan, 5 November); a failed first join removes the half-made profile. Password: 8–63 printable ASCII characters, or none for an open network. `POST /api/wifi/order` `{"ids": [every saved id, first choice first]}` sets the priorities N…1. `POST /api/wifi/forget` `{"id"}` (two taps on the screen). `POST /api/wifi/dismiss` clears a finished `job`.
- **Passwords go only to NetworkManager:** never logged (the log says only "Wi-Fi join succeeded/failed", without the name), never returned, never written to the console's settings; the keyboard shows dots unless Show is pressed and forgets the text when it closes. Accepted limit: `nmcli` takes the password on its command line, so it is visible to `ps` on the Pi for the second the command runs (a single-user console).
- Not supported (Dan, 2 November): hidden networks, WPA-Enterprise (username logins), captive portals.

**Planned, not yet specified here:**
- **Field Day operating profile.** Independent of data source and economy mode. It only adds display elements; it never changes how data is fetched, ranked or aged, so weather-alert priority and freshness are identical in both profiles. Event information is hidden in General HF. First version: a timer in a status-bar pill, never on the Priority Card, with its settings (operations start and end, entered in UTC). **Changed 5 November 2026 (evening):** the profile can be switched on days ahead; until the operations day the pill is a quiet reminder ("Field Day · Sat 30 Jan · ops 19:00Z"). Setup then begins on its own at local midnight on the operations day and counts down to the start of operations (time left to finish setup); there is no Start setup button. Then Operations (time remaining), then "Operations complete" until the profile is switched off. Times are hours:minutes. Mockups approved 5 November (four pill states, Settings in tabs). Later (Phase 2): setup readiness with manually updated categories and states Not started / In progress / Ready / Needs attention, where Ready means tested and working. A compact "X of Y ready" opens a detail panel; during operations a small indicator shows only when something isn't Ready. Candidates: UTC/local on the timer, one next-milestone reminder. Needs a mockup before building.
- **Wi-Fi networks from the touchscreen** (built 17 November 2026, see above), with Field Mode (Phase 3). Mockups approved 5 November (Wi-Fi tab with saved networks in preference order and nearby networks; join screen with on-screen keyboard). Needs a narrow polkit rule letting user `hamops` save, join and scan Wi-Fi (checked on the Pi 5 November).
- **Field Day timer built (12 November 2026).** Settings gain `profile` (`"general_hf"` default, or `"field_day"`) and `field_day {ops_start, ops_end}` (UTC, `"2027-01-30T19:00Z"`, or null), checked by `PUT /api/settings` (end after start; a warning past 72 hours). `/api/system` gains a `field_day` block: `{"enabled": true, "ops_start": "2027-01-30T19:00:00Z", "ops_end": "2027-01-31T19:00:00Z", "setup_start": "2027-01-30T05:00:00Z", "phase": "reminder" | "setup" | "operations" | "complete" | "unset" | "off"}`. `setup_start` is local midnight (the Pi's zone and daylight-saving rules) on the local day operations begin; the page compares the clock with the three times each second, so the pill changes on time between polls. The pill (changed 16 November 2026, Dan): a day or more before operations "FIELD DAY · Sat 30 Jan · 14 days away"; under a day "FIELD DAY · 20:00 until operations" (from local midnight on the day labelled "Setup"); in the last hour before operations the pill fills in solid blue (still the info colour, never an alert colour). As first approved: "FIELD DAY · Sat 30 Jan · ops 19:00Z" → "FIELD DAY · Setup 4:48 until operations" → "FIELD DAY · Operations 20:30 left" → "FIELD DAY ✓ Operations complete 19:00Z" (grey), hours:minutes rounded up; "Set ops times in Settings" when the times are missing. While it shows, the status bar drops the date beside the clock to make room. Times are set with a touch date and time picker (built 16 November 2026, mockup approved 13 November): a Monday-first month calendar and hour/minute steppers in UTC (minutes in 5s), quick 18:00Z/19:00Z/20:00Z buttons, and a summary with the local time and how long operations last; Set is disabled when the end would come before the start.
- **Settings in tabs** (approved 5 November): Station & profile (Data source, station, Operating profile, Field Day times, economy mode) · Display · Data sources · Wi-Fi.
- **Main clock, UTC or local first** (opened 5 November; **settled 11 November, Dan: both ways**): one saved setting, `display.main_clock`, changed by tapping the large time or on the Display tab; default UTC first. Built 11 November (see Times, at the top).
- **`POST /api/power`** with the Shut down dialog. (The **NOAA Weather Radio** block was built 7 November 2026, §5.)
