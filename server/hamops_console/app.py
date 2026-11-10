"""The console service: serves the dashboard page and the JSON API.

Phase 1 (2026-11-02): GET /, /api/propagation, /api/weather, /api/alerts,
/api/system, POST /api/alerts/ack and /api/alerts/clear-hold, and
GET/PUT /api/settings with POST /api/settings/test. 2026-11-07: POST
/api/weather-radio/refresh. 2026-11-10: the Alerts page (/alerts), and
/api/alerts also lists NWS alerts ended in the last 24 hours. The System page
(/system).

The service listens on 127.0.0.1 by default, so only the Pi's own kiosk
browser can reach it. To look at it from another computer while developing,
set "server": {"host": "0.0.0.0"} in the settings file.
"""

from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from . import __version__, alerts as alerts_mod, freshness, settings as settings_mod, settings_api, system_info
from .propagation import PropagationSource
from .weather import WeatherSource

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_DIR = REPO_ROOT / "dashboard"


def create_app(settings=None, propagation=None, weather=None, acks=None, ended=None, dashboard_dir=DASHBOARD_DIR,
               settings_file=None, test_connections=settings_api.test_connections):
    settings = settings or settings_mod.load()
    store = settings_api.SettingsStore(settings, settings_file)
    propagation = propagation or PropagationSource(settings)
    weather = weather or WeatherSource(settings)
    acks = acks or alerts_mod.AckStore()
    ended = ended or alerts_mod.EndedStore()
    started_at = freshness.utcnow()
    app = Flask(__name__, static_folder=None)

    def envelope(**blocks):
        body = {"mode": settings["mode"], "generated_at": freshness.iso(freshness.utcnow())}
        body.update(blocks)
        response = jsonify(body)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return send_from_directory(dashboard_dir, "index.html")

    @app.get("/weather")
    def weather_page():
        """The Weather detail page (approved mockup "Weather page")."""
        return send_from_directory(dashboard_dir, "weather.html")

    @app.get("/propagation")
    def propagation_page():
        """The Propagation detail page (approved mockup "Propagation page")."""
        return send_from_directory(dashboard_dir, "propagation.html")

    @app.get("/alerts")
    def alerts_page():
        """The Alerts detail page (approved mockup "Alerts page")."""
        return send_from_directory(dashboard_dir, "alerts.html")

    @app.get("/system")
    def system_page():
        """The System detail page (approved mockup "System page")."""
        return send_from_directory(dashboard_dir, "system.html")

    @app.get("/check")
    def check_page():
        """The plain service check page (diagnostics), kept beside the dashboard."""
        return send_from_directory(dashboard_dir, "check.html")

    @app.get("/static/<path:name>")
    def static_file(name):
        return send_from_directory(dashboard_dir, name)

    @app.get("/api/propagation")
    def api_propagation():
        return envelope(propagation=propagation.block())

    @app.get("/api/weather")
    def api_weather():
        return envelope(**weather.blocks())

    def ranked(blocks=None):
        return alerts_mod.build(propagation.block(), blocks or weather.blocks(), settings,
                                freshness.utcnow(), acks.current())

    @app.get("/api/alerts")
    def api_alerts():
        blocks = weather.blocks()
        items = ranked(blocks)
        status = (blocks.get("alerts") or {}).get("status")
        return envelope(items=items, ended=ended.observe(items, status), nws_alerts_status=status)

    @app.post("/api/alerts/ack")
    def api_alerts_ack():
        """The console's own Acknowledge. Only an item currently in the list
        can be acknowledged; nothing is ever forwarded to Weather Sentinel."""
        body = request.get_json(silent=True) or {}
        alert_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(alert_id, str) or not alert_id or len(alert_id) > 300:
            return jsonify({"error": "send {\"id\": \"...\"}"}), 400
        if alert_id not in {i["ref"]["id"] for i in ranked()}:
            return jsonify({"error": "no current alert with that id"}), 404
        acks.add(alert_id)
        return jsonify({"acknowledged": alert_id})

    @app.post("/api/alerts/clear-hold")
    def api_alerts_clear_hold():
        """The operator's Clear for the lightning holds, until the next strike.
        Allowed only while a lightning item is in the list; the dashboard asks
        for confirmation first. Nothing is sent to Weather Sentinel."""
        lightning = {"lightning_5mi", "lightning_frequent", "lightning_10mi"}
        if not any(i["category"] in lightning for i in ranked()):
            return jsonify({"error": "No lightning hold to clear"}), 409
        return jsonify({"cleared_at": freshness.iso(weather.clear_hold())})

    @app.post("/api/weather-radio/refresh")
    def api_weather_radio_refresh():
        """Update the stored NOAA Weather Radio list now (before a field event),
        instead of waiting for the weekly download. Runs in the background;
        the weather_radio block shows `updating` until it finishes."""
        started = weather.nwr.refresh_now()
        return jsonify({"started": started}), (202 if started else 409)

    @app.get("/api/settings")
    def api_settings():
        response = jsonify(store.view())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.put("/api/settings")
    def api_settings_put():
        ok, body = store.update(request.get_json(silent=True))
        return jsonify(body), (200 if ok else 400)

    @app.post("/api/settings/test")
    def api_settings_test():
        """Test connections, optionally for addresses not yet saved:
        {"sources": {"propmon_url": "...", "sentinel_url": "..."}}"""
        body = request.get_json(silent=True)
        sources = body.get("sources") if isinstance(body, dict) else None
        if sources is not None and not (isinstance(sources, dict) and all(
                k in ("propmon_url", "sentinel_url") and isinstance(v, str) for k, v in sources.items())):
            return jsonify({"errors": ["sources: propmon_url and/or sentinel_url addresses"]}), 400
        return jsonify({"results": test_connections(settings, sources)})

    @app.get("/api/system")
    def api_system():
        return envelope(system=system_info.block(__version__, started_at))

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok", "version": __version__})

    return app
