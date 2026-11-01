# Console service

The small Python service on the Pi that the dashboard talks to. It will run under systemd. It serves the dashboard page, gathers data from the configured sources (home services in Home Mode; NWS, HamQSL and NOAA SWPC in Field Mode), decides what is fresh or stale, ranks the alerts for the Priority Card, and reports the Pi's own status.

**So far:** `hamops_console/nws_text.py`, which pulls the short "what" and "hazard" lines out of NWS alert text (ported from Weather Sentinel), with tests in `tests/`.

```
python server/tests/test_nws_text.py
```
