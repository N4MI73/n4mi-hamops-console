#!/bin/sh
# Ham Ops Console -- mirror the touchscreen onto an HDMI monitor whenever one is plugged
# in (Dan, 2026-11-22; no setting). Started by the kiosk's autostart, inside labwc, so
# the Wayland session is already there.
#
# labwc on its own extends the desktop onto a new monitor; wl-mirror (a Raspberry Pi OS
# package) shows a full-screen copy of the touchscreen on it instead, the right way up.
# Touch stays on the touchscreen (rc.xml ties it to DSI-2). Either micro-HDMI port works.
#
# How it behaves (changed 2026-11-24 after tests on the Pi):
# - A monitor connected at power-up: autostart turns it off before Chromium opens, so the
#   kiosk window opens on the touchscreen (with the monitor on, labwc put it on the
#   monitor and left the touchscreen blank). Once Chromium has had time to open, this
#   turns the monitor back on.
# - wl-mirror is started once and left running. It carries on by itself when the monitor
#   is unplugged and plugged in again. Stopping it on unplug and starting a new one on
#   re-plug (the first version) once left touch landing to the right of the finger until
#   a restart; a wl-mirror left running came through several re-plugs with touch correct.
#   A new one is started only if none is running (e.g. it ended).

TOUCH=DSI-2
CHECK_SEC=3
SETTLE_SEC=15        # after Chromium starts, before a monitor held off at power-up comes on

command -v wl-mirror >/dev/null 2>&1 || exit 0     # not installed: nothing to do
command -v wlr-randr >/dev/null 2>&1 || exit 0

# "HDMI-A-1 yes" for each HDMI output wlr-randr lists (an unplugged monitor isn't listed).
hdmi_outputs() {
  wlr-randr 2>/dev/null | awk '
    /^[^ ]/ { name = ($1 ~ /^HDMI-A-[0-9]+$/) ? $1 : "" }
    name != "" && /^ +Enabled:/ { print name, $2; name = "" }'
}

# Wait for the kiosk window: Chromium running, then a little longer for it to open.
until pgrep -f "chromium.*--kiosk" >/dev/null 2>&1; do
  sleep 1
done
sleep "$SETTLE_SEC"

while true; do
  set -- $(hdmi_outputs | head -n 1)
  out=$1 enabled=$2
  if [ -n "$out" ]; then
    if [ "$enabled" = "no" ]; then
      wlr-randr --output "$out" --on >/dev/null 2>&1
    elif ! pgrep -x wl-mirror >/dev/null 2>&1; then
      wl-mirror --fullscreen-output "$out" "$TOUCH" >/dev/null 2>&1 &
    fi
  fi
  sleep "$CHECK_SEC"
done
