"""Is the field engine's copy of the rating model current? (2026-11-17)

Ham Shack Automation keeps tuning the model. Each change bumps `model_version` (the DX scale,
also reported live by PropMon v2) or `field_day.version` (the Field Day scale; PropMon doesn't
report it). The update terms (Dan, 2026-11-16): new models reach the console by handover --
committed to the console repository with their tests -- and **the console never downloads or
switches models itself**. It only compares and says so.

  - PropMon v2's `model_version`, whenever the home propagation block has one;
  - the published file (setting `sources.field_engine_url`), read about once a day in the
    background, for both versions.

The answer is `current`, `out_of_date` (with which version is newer) or `unknown` (nothing
to compare with yet, e.g. in the field with no internet) -- never a guess.
"""

import json
import threading
import urllib.request

from . import freshness

CHECK_EVERY_SEC = 24 * 3600
RETRY_AFTER_FAIL_SEC = 3600
TIMEOUT_SEC = 10
USER_AGENT = "HamOpsConsole (github.com/N4MI73/n4mi-hamops-console)"


def _get(url, timeout=TIMEOUT_SEC):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        return reply.read()


def _run_in_thread(job):
    threading.Thread(target=job, name="engine-check", daemon=True).start()


def published_versions(data):
    """The published rating_model.json -> (model_version, field_day_version)."""
    doc = json.loads(data)
    mv = doc.get("model_version") if isinstance(doc, dict) else None
    fd = doc.get("field_day") if isinstance(doc, dict) else None
    fv = fd.get("version") if isinstance(fd, dict) else None
    if not isinstance(mv, str) or not mv:
        raise ValueError("published model has no model_version")
    return mv, fv if isinstance(fv, str) and fv else None


class EngineCheck:
    def __init__(self, settings, model, fetch=_get, clock=freshness.utcnow, run=_run_in_thread):
        self.settings = settings
        self.mine = (model["model_version"], (model.get("field_day") or {}).get("version"))
        self._fetch = fetch
        self._clock = clock
        self._run = run
        self._lock = threading.Lock()
        self._propmon = None             # PropMon's model_version, last seen
        self._published = None           # (model_version, field_day_version)
        self._checked_at = None
        self._attempted_at = None
        self._failed = False
        self._busy = False

    def note_propmon(self, model_version):
        if isinstance(model_version, str) and model_version:
            with self._lock:
                self._propmon = model_version

    def _refresh(self, url):
        try:
            versions = published_versions(self._fetch(url))
        except Exception:  # noqa: BLE001 -- any failure must end the attempt
            versions = None
        with self._lock:
            if versions:
                self._published, self._checked_at = versions, self._clock()
            self._failed = versions is None
            self._busy = False

    def block(self):
        now = self._clock()
        url = self.settings["sources"].get("field_engine_url")
        start = False
        with self._lock:
            wait = RETRY_AFTER_FAIL_SEC if self._failed else CHECK_EVERY_SEC
            if url and not self._busy and (self._attempted_at is None or
                                           (now - self._attempted_at).total_seconds() >= wait):
                self._busy, self._attempted_at, start = True, now, True
        if start:
            self._run(lambda: self._refresh(url))
        with self._lock:
            mine_m, mine_f = self.mine
            # Versions start with their date (2026-11-17-v3), so a later one sorts after.
            # Only a *newer* version means out of date: a PropMon not yet redeployed, or a
            # published file behind this copy, is not a reason to warn.
            seen_m = [v for v in (self._propmon, self._published[0] if self._published else None) if v]
            latest_f = self._published[1] if self._published else None
            newer = sorted({f"model {v}" for v in seen_m if v > mine_m})
            if latest_f and (not mine_f or latest_f > mine_f):
                newer.append(f"Field Day scale {latest_f}")
            if newer:
                status = "out_of_date"
            elif seen_m or latest_f:
                status = "current"
            else:
                status = "unknown"
            return {
                "status": status,
                "model_version": mine_m,
                "field_day_version": mine_f,
                "propmon_model_version": self._propmon,
                "published_model_version": self._published[0] if self._published else None,
                "published_field_day_version": latest_f,
                "checked_at": freshness.iso(self._checked_at),
                "message": ("Field engine out of date: " + ", ".join(newer) + " is available") if newer else None,
            }
