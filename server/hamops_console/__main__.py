"""Run the console service:  python3 -m hamops_console   (from the server/ folder)

On the Pi this is started by systemd (pi/systemd/hamops-console.service).
"""

import logging

from . import settings as settings_mod
from .app import create_app


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Flask's built-in server logs one line per request at INFO. The kiosk
    # polls every 15 s, which would be thousands of journal lines a day, so
    # only its warnings and errors are kept. Errors inside the app are still
    # logged in full by Flask itself.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    settings = settings_mod.load()
    app = create_app(settings)
    server = settings["server"]
    logging.getLogger("hamops_console").info(
        "listening on http://%s:%s/", server["host"], server["port"])
    # Flask's built-in server is enough here: one local client (the kiosk
    # browser) on the same Pi, listening on 127.0.0.1 only.
    app.run(host=server["host"], port=int(server["port"]), threaded=True)


if __name__ == "__main__":
    main()
