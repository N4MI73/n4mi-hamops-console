"""Wi-Fi networks from the touchscreen (2026-11-17; mockups approved 2026-11-05).

The Settings Wi-Fi tab lists saved networks in preference order, scans for nearby ones, joins
a new one with a password typed on the on-screen keyboard, reorders and forgets. The work is
done by NetworkManager through `nmcli`, run as user hamops. NetworkManager asks polkit; a
service has no login session, so saving, joining and scanning are refused until the narrow
rule in pi/polkit/50-hamops-wifi.rules is installed (checked on the Pi 2026-11-05: `auth` for
settings.modify.system, network-control and wifi.scan). Until then the tab says so.

Passwords go only to NetworkManager. They are never logged, never returned to the page and
never written to the console's settings; NetworkManager stores them in its own connection
files (root-only). One limit, accepted for a single-user console: nmcli takes the password
on its command line, so it is visible to `ps` on the Pi for the second nmcli runs.

Preference order is NetworkManager's `connection.autoconnect-priority` (higher is tried
first); the console numbers saved networks top to bottom and a newly joined network goes to
the bottom (Dan, 2026-11-05). Not supported (Dan, 2026-11-02): hidden networks,
WPA-Enterprise (username logins) and captive portals.
"""

import logging
import subprocess
import threading

from . import freshness

log = logging.getLogger(__name__)

WIFI_TYPE = "802-11-wireless"
SCAN_TIMEOUT = 20
JOIN_TIMEOUT = 45
CMD_TIMEOUT = 10
PERMISSIONS = {"scan": "org.freedesktop.NetworkManager.wifi.scan",
               "modify": "org.freedesktop.NetworkManager.settings.modify.system",
               "control": "org.freedesktop.NetworkManager.network-control"}
NO_RULE = "This Pi doesn't allow it yet (the Wi-Fi polkit rule isn't installed)."


def _run_in_thread(job):
    threading.Thread(target=job, name="wifi", daemon=True).start()


def split_terse(line):
    """One line of `nmcli -t` output -> fields (':' separates; '\\:' and '\\\\' are escapes)."""
    out, cur, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def ssid_ok(ssid):
    return isinstance(ssid, str) and 0 < len(ssid.encode("utf-8")) <= 32 and \
        all(c.isprintable() for c in ssid)


def password_ok(password):
    """WPA-PSK pass phrase: 8-63 printable ASCII characters."""
    return isinstance(password, str) and 8 <= len(password) <= 63 and \
        all(32 <= ord(c) < 127 for c in password)


def _bars(signal):
    if not isinstance(signal, int):
        return 0
    return 4 if signal >= 75 else 3 if signal >= 50 else 2 if signal >= 25 else 1


def _reason(result):
    """nmcli's last error line, turned into words for the page (never contains a password:
    nmcli doesn't repeat it)."""
    text = (result.stderr or result.stdout or "").strip().splitlines()
    line = text[-1] if text else f"nmcli exit {result.returncode}"
    low = line.lower()
    if "not authorized" in low or "insufficient privileges" in low or "permission" in low:
        return NO_RULE
    if "secrets were required" in low or "802-1x supplicant" in low or "no secrets" in low:
        return "The password wasn't accepted."
    if "no network with ssid" in low:
        return "That network isn't in range any more."
    return line.replace("Error: ", "")


class _Failed(Exception):
    pass


class WiFi:
    """The Wi-Fi tab's data and actions. Thread-safe; one per service."""

    def __init__(self, runner=subprocess.run, clock=freshness.utcnow, run=_run_in_thread):
        self._runner = runner
        self._clock = clock
        self._run = run
        self._lock = threading.Lock()
        self._scanning = False
        self._scanned_at = None
        self._scan_error = None
        self._job = None            # {"ssid", "state": joining|failed|joined, "message"}

    # ------------------------------------------------------------ nmcli
    def _nmcli(self, args, timeout=CMD_TIMEOUT):
        """-> CompletedProcess, or None if nmcli couldn't run. Arguments are never logged
        (a join carries the password)."""
        try:
            return self._runner(["nmcli"] + args, capture_output=True, text=True,
                                timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("nmcli %s failed: %s", args[0] if args else "", e.__class__.__name__)
            return None

    def _lines(self, args):
        r = self._nmcli(args)
        if r is None or r.returncode != 0:
            return None
        return [split_terse(l) for l in r.stdout.splitlines() if l.strip()]

    def permissions(self):
        rows = self._lines(["-t", "-f", "PERMISSION,VALUE", "general", "permissions"]) or []
        have = {r[0]: r[1] for r in rows if len(r) >= 2}
        return {k: have.get(v) == "yes" for k, v in PERMISSIONS.items()}

    def saved(self):
        """Saved Wi-Fi connections, first choice first: [{id, name, ssid, priority, active}]."""
        rows = self._lines(["-t", "-f", "NAME,UUID,TYPE,AUTOCONNECT-PRIORITY,ACTIVE", "connection", "show"])
        if rows is None:
            return None
        out = []
        for r in rows:
            if len(r) < 5 or r[2] != WIFI_TYPE:
                continue
            ssid_rows = self._lines(["-t", "-g", "802-11-wireless.ssid", "connection", "show", "uuid", r[1]])
            ssid = ssid_rows[0][0] if ssid_rows and ssid_rows[0] else r[0]
            try:
                prio = int(r[3])
            except ValueError:
                prio = 0
            out.append({"id": r[1], "name": r[0], "ssid": ssid, "priority": prio, "active": r[4] == "yes"})
        out.sort(key=lambda c: (-c["priority"], c["ssid"].lower()))
        return out

    def nearby(self):
        """Networks heard in the last scan (no new scan): [{ssid, signal, security, open}],
        strongest first, one row per name; hidden networks (no name) left out."""
        rows = self._lines(["-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "no"])
        if rows is None:
            return None
        best = {}
        for r in rows:
            if len(r) < 3 or not r[0]:
                continue
            try:
                signal = int(r[1])
            except ValueError:
                signal = None
            sec = r[2].strip()
            entry = {"ssid": r[0], "signal": signal, "open": sec in ("", "--"),
                     "enterprise": "802.1X" in sec, "security": sec}
            old = best.get(r[0])
            if old is None or (signal or 0) > (old["signal"] or 0):
                best[r[0]] = entry
        return sorted(best.values(), key=lambda n: -(n["signal"] or 0))

    # ------------------------------------------------------------ the tab
    def block(self):
        perms = self.permissions()
        saved, nearby = self.saved(), self.nearby()
        with self._lock:
            job = dict(self._job) if self._job else None
            scanning, scanned_at, scan_error = self._scanning, self._scanned_at, self._scan_error
        if saved is None or nearby is None:
            return {"available": False, "message": "NetworkManager isn't answering on this Pi",
                    "permissions": perms, "can_change": False, "saved": [], "nearby": [],
                    "scanning": scanning, "scanned_at": freshness.iso(scanned_at), "job": job}
        heard = {n["ssid"]: n for n in nearby}
        by_ssid = {c["ssid"]: c for c in saved}
        out_saved = []
        for rank, c in enumerate(saved, 1):
            n = heard.get(c["ssid"])
            out_saved.append({"id": c["id"], "ssid": c["ssid"], "rank": rank, "active": c["active"],
                              "in_range": n is not None, "signal": n["signal"] if n else None,
                              "bars": _bars(n["signal"]) if n else 0})
        out_nearby = [dict(n, bars=_bars(n["signal"]), saved_id=by_ssid[n["ssid"]]["id"] if n["ssid"] in by_ssid else None)
                      for n in nearby]
        can = all(perms.values())
        return {"available": True, "message": None if can else NO_RULE, "permissions": perms,
                "can_change": can, "saved": out_saved, "nearby": out_nearby, "scanning": scanning,
                "scanned_at": freshness.iso(scanned_at), "scan_error": scan_error, "job": job}

    # ------------------------------------------------------------ actions
    def scan(self):
        """Start a scan in the background. -> (started, error)."""
        with self._lock:
            if self._scanning:
                return False, "A scan is already running"
            self._scanning = True

        def job():
            r = self._nmcli(["device", "wifi", "rescan"], timeout=SCAN_TIMEOUT)
            # A rescan answers at once; NetworkManager fills the list over the next seconds.
            error = None if r is not None and r.returncode == 0 else (_reason(r) if r else "nmcli didn't run")
            with self._lock:
                self._scanning = False
                self._scan_error = error
                if error is None:
                    self._scanned_at = self._clock()
        self._run(job)
        return True, None

    def join(self, ssid, password=None):
        """Join a nearby network: an already saved one is simply brought up; a new one is
        saved (as the last choice) and joined. Runs in the background; the tab shows the
        result in `job`. -> (started, error)."""
        if not ssid_ok(ssid):
            return False, "That network name can't be used"
        if password not in (None, "") and not password_ok(password):
            return False, "A Wi-Fi password is 8 to 63 letters, digits or symbols"
        with self._lock:
            if self._job and self._job["state"] == "joining":
                return False, "Already joining a network"
            self._job = {"ssid": ssid, "state": "joining", "message": None}
        self._run(lambda: self._join(ssid, password or None))
        return True, None

    def _join(self, ssid, password):
        try:
            saved = self.saved() or []
            known = next((c for c in saved if c["ssid"] == ssid), None)
            before = next((c for c in saved if c["active"]), None)
            if known:
                if password:        # a saved network whose password changed
                    r = self._nmcli(["connection", "modify", "uuid", known["id"], "wifi-sec.psk", password])
                    if r is None or r.returncode != 0:
                        raise _Failed(_reason(r) if r else "nmcli didn't run")
                r = self._nmcli(["connection", "up", "uuid", known["id"]], timeout=JOIN_TIMEOUT)
            else:
                lowest = min((c["priority"] for c in saved), default=1)
                args = ["device", "wifi", "connect", ssid]
                if password:
                    args += ["password", password]
                r = self._nmcli(args, timeout=JOIN_TIMEOUT)
                if r is not None and r.returncode == 0:
                    now = next((c for c in (self.saved() or []) if c["ssid"] == ssid), None)
                    if now:     # a new network goes to the bottom of the preference order
                        self._nmcli(["connection", "modify", "uuid", now["id"],
                                     "connection.autoconnect-priority", str(lowest - 1)])
                elif not known:
                    # A failed first join can leave a half-made profile behind: remove it,
                    # so a wrong password isn't saved as a network that never works.
                    left = [c for c in (self.saved() or []) if c["ssid"] == ssid]
                    for c in left:
                        self._nmcli(["connection", "delete", "uuid", c["id"]])
                    # nmcli drops the current network before trying the new one: go back to
                    # it, so a mistyped password at a field site doesn't leave the console
                    # offline (code review, 2026-11-17).
                    if before:
                        self._nmcli(["connection", "up", "uuid", before["id"]], timeout=JOIN_TIMEOUT)
            ok = r is not None and r.returncode == 0
            message = None if ok else (_reason(r) if r else "nmcli didn't run")
        except _Failed as e:
            ok, message = False, str(e)
        except Exception as e:  # noqa: BLE001 -- the job must always end
            ok, message = False, f"Join failed ({e.__class__.__name__})"
        log.info("Wi-Fi join %s", "succeeded" if ok else "failed")    # no name, no password
        with self._lock:
            self._job = {"ssid": ssid, "state": "joined" if ok else "failed", "message": message}

    def reorder(self, ids):
        """ids: every saved network's id, first choice first. -> error or None."""
        saved = self.saved()
        if saved is None:
            return "NetworkManager isn't answering on this Pi"
        if not isinstance(ids, list) or sorted(ids) != sorted(c["id"] for c in saved):
            return "Send every saved network once, first choice first"
        n = len(ids)
        for i, uuid in enumerate(ids):
            r = self._nmcli(["connection", "modify", "uuid", uuid, "connection.autoconnect-priority", str(n - i)])
            if r is None or r.returncode != 0:
                return _reason(r) if r else "nmcli didn't run"
        return None

    def forget(self, uuid):
        """Delete a saved network. -> error or None."""
        saved = self.saved()
        if saved is None:
            return "NetworkManager isn't answering on this Pi"
        if uuid not in {c["id"] for c in saved}:
            return "That network isn't saved"
        r = self._nmcli(["connection", "delete", "uuid", uuid])
        if r is None or r.returncode != 0:
            return _reason(r) if r else "nmcli didn't run"
        log.info("a saved Wi-Fi network was forgotten")
        return None

    def clear_job(self):
        with self._lock:
            if self._job and self._job["state"] != "joining":
                self._job = None
