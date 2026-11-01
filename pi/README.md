# Raspberry Pi setup files

Files that turn a fresh Raspberry Pi OS Lite install into the console's kiosk display. The full step-by-step procedure, with checks and troubleshooting, is the printable *Kiosk setup guide*. This page lists what goes where.

| File here | Goes to (on the Pi) | Purpose |
|---|---|---|
| `labwc/rc.xml` | `~/.config/labwc/rc.xml` | Ties the touch panel (`Goodix Capacitive TouchScreen`) to the display (`DSI-2`) so touch follows the rotation |
| `labwc/autostart` | `~/.config/labwc/autostart` | Rotates the display to landscape and runs Chromium fullscreen, restarting it if it closes |
| `profile-kiosk.sh` | appended to `~/.profile` | Starts labwc on the Pi's own screen (tty1) only. SSH sessions are unaffected |

Also needed:

```
sudo apt install -y labwc wlr-randr chromium
sudo raspi-config nonint do_boot_behaviour B2    # console autologin on the Pi's own screen
```

**Rotation:** `--transform 90` in `autostart` gives landscape on this build. If the picture comes out upside down, use `270`.

**Off switch:** `touch ~/kiosk.off` and reboot to get the plain text console instead of the kiosk; `rm ~/kiosk.off` and reboot to turn it back on.

**Test page:** `diagnostics/kiosk-test.html` shows the screen size and orientation and drops a dot wherever you touch. `autostart` points at it until the dashboard exists; change the `KIOSK_URL` line to switch.
