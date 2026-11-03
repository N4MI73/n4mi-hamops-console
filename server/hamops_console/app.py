"""The console service: serves the dashboard page and the JSON API.

Phase 1 (2026-11-02): GET /, /api/propagation, /api/weather, /api/alerts,
/api/system and POST /api/alerts/ack. The settings endpoints follow.

The service listens on 127.0.0.1 by default, so only the Pi's own kiosk
browser can reach it. To look at it from another computer while developing,
set "server": {"host": "0.0.0.0"} in the settings file.
"""

from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from . import __version__, alerts as alerts_mod, freshness, settings as settings_mod, system_info
from .propagation import PropagationSource
from .weather import WeatherSource

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_DIR = REPO_ROOT / "dashboard"


def create_app(settings=None, propagation=None, weather=None, acks=None, dashboard_dir=DASHBOARD_DIR):
    settings = settings or settings_mod.load()
    propagation = propagation or PropagationSource(settings)
    weather = weather or WeatherSource(settings)
    acks = acks or alerts_mod.AckStore()
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

    @app.get("/static/<path:name>")
    def static_file(name):
        return send_from_directory(dashboard_dir, name)

    @app.get("/api/propagation")
    def api_propagation():
        return envelope(propagation=propagation.block())

    @app.get("/api/weather")
    def api_weather():
        return envelope(**weather.blocks())

    def ranked():
        return alerts_mod.build(propagation.block(), weather.blocks(), settings,
                                freshness.utcnow(), acks.current())

    @app.get("/api/alerts")
    def api_alerts():
        return envelope(items=ranked())

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

    @app.get("/api/system")
    def api_system():
        return envelope(system=system_info.block(__version__, started_at))

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok", "version": __version__})

    return app
