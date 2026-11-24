#!/bin/sh
# Ham Ops Console -- mirror the touchscreen onto an HDMI monitor whenever one is plugged
# in, and stop when it is unplugged (Dan, 2026-11-22; no setting). Started by the kiosk's
# autostart, inside labwc, so the Wayland session is already there.
#
# labwc on its own extends the desktop onto a new monitor; wl-mirror (a Raspberry Pi OS
# package) shows a full-screen copy of the touchscreen on it instead, the right way up.
# Touch stays on the touchscreen (rc.xml ties it to DSI-2). Every few seconds this looks
# at the outputs wlr-randr lists (an unplugged monitor isn't listed) and starts or stops
# wl-mirror to match. Either micro-HDMI port works (HDMI-A-1 or HDMI-A-2).

TOUCH=DSI-2
CHECK_SEC=3

command -v wl-mirror >/dev/null 2>&1 || exit 0     # not installed: nothing to do
command -v wlr-randr >/dev/null 2>&1 || exit 0

pid=""
on=""
while true; do
  out=$(wlr-randr 2>/dev/null | awk '/^HDMI-A-[0-9]+ / {print $1; exit}')
  # Stop a mirror that has ended, or one on a monitor that has gone or changed port.
  if [ -n "$pid" ] && { ! kill -0 "$pid" 2>/dev/null || [ "$out" != "$on" ]; }; then
    kill "$pid" 2>/dev/null
    pid=""
    on=""
  fi
  if [ -n "$out" ] && [ -z "$pid" ]; then
    wl-mirror --fullscreen-output "$out" "$TOUCH" >/dev/null 2>&1 &
    pid=$!
    on=$out
  fi
  sleep "$CHECK_SEC"
done
