# Raspberry Pi setup files

Files that turn a fresh Raspberry Pi OS Lite install into the console's kiosk display. The full step-by-step procedure, with checks and troubleshooting, is the printable *Kiosk setup guide*. This page lists what goes where.

| File here | Goes to (on the Pi) | Purpose |
|---|---|---|
| `labwc/rc.xml` | `~/.config/labwc/rc.xml` | Ties the touch panel (`11-005d Goodix Capacitive TouchScreen`, the name libinput reports) to the display (`DSI-2`) so touch follows the rotation |
| `labwc/autostart` | `~/.config/labwc/autostart` | Rotates the display to landscape and runs Chromium fullscreen, restarting it if it closes |
| `profile-kiosk.sh` | appended to `~/.profile` | Starts labwc on the Pi's own screen (tty1) only. SSH sessions are unaffected |
| `systemd/hamops-console.service` | `/etc/systemd/system/hamops-console.service` | Runs the console service at boot and restarts it if it stops |

Also needed:

```
sudo apt install -y labwc wlr-randr chromium
sudo raspi-config nonint do_boot_behaviour B2    # console autologin on the Pi's own screen
```

**Rotation:** `--transform 270` in `autostart` gives landscape the right way up on this build (confirmed on the real screen, 1 November 2026). If a different mount shows it upside down, use `90`.

**Touch name:** labwc matches the touch panel by its libinput name, which on this build is `11-005d Goodix Capacitive TouchScreen` (the `11-005d` is its I2C bus and address). Check yours with `sudo libinput list-devices | grep -i -A1 goodix`; if the name in `rc.xml` doesn't match exactly, touch lands a quarter-turn away from your finger.

**Off switch:** `touch ~/kiosk.off` and reboot to get the plain text console instead of the kiosk; `rm ~/kiosk.off` and reboot to turn it back on.

**Test page:** `diagnostics/kiosk-test.html` shows the screen size and orientation and drops a dot wherever you touch. `autostart` points at it until the console service is installed; then change the `KIOSK_URL` line to `http://127.0.0.1:8090/`.

## Console service

The service runs from a copy of this repository on the Pi at `/home/hamops/hamops-console` (cloned with git; updated with `git pull`). Settings live in `~/.config/hamops-console/settings.json`, never in the repository.

```
sudo apt install -y python3-flask git
git clone https://github.com/N4MI73/n4mi-hamops-console.git ~/hamops-console
sudo cp ~/hamops-console/pi/systemd/hamops-console.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hamops-console
systemctl status hamops-console          # should say active (running)
curl -s http://127.0.0.1:8090/healthz    # {"status": "ok", ...}
```

The unit file assumes the user `hamops` and the path above; edit both lines if yours differ. Logs: `journalctl -u hamops-console`.
