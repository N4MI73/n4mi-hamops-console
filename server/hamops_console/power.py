"""Shut down and Restart from the touchscreen (2026-11-11).

Runs `systemctl poweroff` or `systemctl reboot` as the hamops user. systemd asks
polkit whether that's allowed; a service has no login session, so the default
answer is no until the polkit rule in pi/polkit/ is installed. The reply then
says so, and nothing happens: the dashboard shows the reason.

systemctl only queues the shutdown and returns at once, so the reply still
reaches the dashboard before the service is stopped.
"""

import logging
import subprocess

log = logging.getLogger(__name__)

COMMANDS = {"shutdown": ["systemctl", "poweroff"], "restart": ["systemctl", "reboot"]}


def run(action, runner=subprocess.run):
    """-> (ok, message)."""
    command = COMMANDS.get(action)
    if command is None:
        return False, "action must be 'shutdown' or 'restart'"
    log.info("%s requested from the touchscreen", action)
    try:
        result = runner(command, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("%s failed: %s", action, e)
        return False, f"couldn't run systemctl ({e.__class__.__name__})"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        detail = detail[-1] if detail else f"exit {result.returncode}"
        log.warning("%s refused: %s", action, detail)
        if "authentication" in detail.lower() or "access denied" in detail.lower():
            return False, "This Pi doesn't allow it yet (the polkit rule isn't installed)."
        return False, detail
    return True, None
