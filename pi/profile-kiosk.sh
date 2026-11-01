# Appended to ~/.profile on the Pi (see pi/README.md).
# Ham Ops Console kiosk: start labwc on the Pi's own screen (tty1) only.
# To boot to the plain console instead: touch ~/kiosk.off
if [ -z "$WAYLAND_DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ] \
   && [ ! -f "$HOME/kiosk.off" ]; then
  exec labwc
fi
