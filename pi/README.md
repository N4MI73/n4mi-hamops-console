# Raspberry Pi setup files

Files that turn a fresh Raspberry Pi OS Lite install into the console's kiosk display. The full step-by-step procedure, with checks and troubleshooting, is the printable *Kiosk setup guide*. This page lists what goes where.

| File here | Goes to (on the Pi) | Purpose |
|---|---|---|
| `labwc/rc.xml` | `~/.config/labwc/rc.xml` | Ties the touch panel (`11-005d Goodix Capacitive TouchScreen`, the name libinput reports) to the display (`DSI-2`) so touch follows the rotation |
| `labwc/autostart` | `~/.config/labwc/autostart` | Rotates the display to landscape, waits for the console service, and runs Chromium fullscreen on it, restarting it if it closes |
| `profile-kiosk.sh` | appended to `~/.profile` | Starts labwc on the Pi's own screen (tty1) only. SSH sessions are unaffected |
| `systemd/hamops-console.service` | `/etc/systemd/system/hamops-console.service` | Runs the console service at boot and restarts it if it stops |
| `udev/99-hamops-backlight.rules` | `/etc/udev/rules.d/99-hamops-backlight.rules` | Lets the console service set the screen brightness and switch the backlight off for Sleep (gives the `video` group write access to the backlight files) |
| `polkit/49-hamops-power.rules` | `/etc/polkit-1/rules.d/49-hamops-power.rules` | Lets the `hamops` user shut down and restart the Pi, for the touchscreen's Shut down dialog. Power-off and reboot only |

Also needed:

```
sudo apt install -y labwc wlr-randr chromium
sudo raspi-config nonint do_boot_behaviour B2    # console autologin on the Pi's own screen
```

**Rotation:** `--transform 270` in `autostart` gives landscape the right way up on this build (confirmed on the real screen, 1 November 2026). If a different mount shows it upside down, use `90`.

**Touch name:** labwc matches the touch panel by its libinput name, which on this build is `11-005d Goodix Capacitive TouchScreen` (the `11-005d` is its I2C bus and address). Check yours with `sudo libinput list-devices | grep -i -A1 goodix`; if the name in `rc.xml` doesn't match exactly, touch lands a quarter-turn away from your finger.

**Off switch:** `touch ~/kiosk.off` and reboot to get the plain text console instead of the kiosk; `rm ~/kiosk.off` and reboot to turn it back on.

**Test page:** `diagnostics/kiosk-test.html` shows the screen size and orientation and drops a dot wherever you touch. `autostart` shows the console service (`http://127.0.0.1:8090/`); to show the test page instead, switch the `KIOSK_URL` line to the commented-out file line above it. At boot, `autostart` waits up to 60 seconds for the service before opening Chromium.

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

Then copy in the kiosk start-up file, which points Chromium at the service: `cp ~/hamops-console/pi/labwc/autostart ~/.config/labwc/autostart` and reboot.

The unit file assumes the user `hamops` and the path above; edit both lines if yours differ. Logs: `journalctl -u hamops-console`.

**Updating** after new commits: `cd ~/hamops-console && git pull && sudo systemctl restart hamops-console`. Don't edit files inside `~/hamops-console` on the Pi; `git pull` expects it unchanged. Settings stay in `~/.config/hamops-console/`.

## Screen brightness, Sleep and Shut down (added 11 November 2026)

Two permission rules, installed once (SSH window):

```
sudo cp ~/hamops-console/pi/udev/99-hamops-backlight.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=backlight --action=add
ls -l /sys/class/backlight/*/brightness /sys/class/backlight/*/bl_power   # both group video, rw for the group
sudo cp ~/hamops-console/pi/polkit/49-hamops-power.rules /etc/polkit-1/rules.d/
sudo chmod 644 /etc/polkit-1/rules.d/49-hamops-power.rules
```

The `hamops` user must be in the `video` group (`id hamops`); it is on a standard Raspberry Pi OS install. polkit reads new rules by itself (its rules folder can only be listed with `sudo`). Raspberry Pi OS already makes `brightness` writable by `video`; the udev rule adds `bl_power`, which Sleep uses for a full backlight-off. **To undo:** `sudo rm /etc/udev/rules.d/99-hamops-backlight.rules /etc/polkit-1/rules.d/49-hamops-power.rules` and reboot.

Since 11 November 2026 the pages are sent so Chromium checks for new versions each time: after `git pull` and `sudo systemctl restart hamops-console` the screen shows the change at its next page load, with no reboot.
