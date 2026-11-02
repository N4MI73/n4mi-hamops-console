"""The console service: serves the dashboard page and the JSON API.

Phase 1, first step (2026-11-02): GET /, /api/propagation and /api/system.
Weather, alerts and settings endpoints follow in later steps.

The service listens on 127.0.0.1 by default, so only the Pi's own kiosk
browser can reach it. To look at it from another computer while developing,
set "server": {"host": "0.0.0.0"} in the settings file.
"""

from pathlib import Path

from flask import Flask, jsonify, send_from_directory

from . import __version__, freshness, settings as settings_mod, system_info
from .propagation import PropagationSource

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_DIR = REPO_ROOT / "dashboard"


def create_app(settings=None, propagation=None, dashboard_dir=DASHBOARD_DIR):
    settings = settings or settings_mod.load()
    propagation = propagation or PropagationSource(settings)
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

    @app.get("/api/system")
    def api_system():
        return envelope(system=system_info.block(__version__, started_at))

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok", "version": __version__})

    return app
