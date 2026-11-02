# Dashboard

The touchscreen page (HTML, CSS and JavaScript) shown fullscreen by Chromium on the Pi. It reads JSON only from the console service on the same Pi.

**For now** `index.html` is a plain service check page: it polls `/api/propagation` and `/api/system` every 15 seconds and shows the raw values with their freshness, to prove the service works on the real screen. It is not the approved dashboard design. The real dashboard will be built against the JSON contracts in `docs/` once they and the mockups are approved.
