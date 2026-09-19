# picam

Security camera for the **Raspberry Pi Zero 2 W** with the **Camera Module v2 (imx219)**.

A single Python process opens the sensor once and uses it for three things in parallel:

- **continuous motion detection** on a low-resolution stream (numpy, negligible cost)
- **Telegram alerts** with a photo when an event fires
- **on-demand MJPEG streaming** over the LAN

Nothing is recorded. Photos live in `/dev/shm` (RAM) just long enough to be sent, then
they are deleted. The microSD card is never written to.

---

## Image rotation

**In my own setup the camera is rotated 90°** (`"rotate_90": 90`), because the wall
mount did not allow any other orientation. The rotation is compensated in software.

If you mount the camera upright, **turn the rotation off** or the image will come out
sideways.

### Getting back to upright

In `/etc/picam.conf`:

```json
"rotate_180": false,
"rotate_90": 0
```

Then:

```bash
sudo systemctl restart picam
```

### Available values

| Key | Values | Effect |
|---|---|---|
| `rotate_180` | `true` / `false` | Flips the image (hflip + vflip). Handled by the hardware ISP, zero cost. |
| `rotate_90` | `0`, `90`, `270` | Quarter-turn rotation. See the note below. |

The two settings are independent and stack. Most mounts only need one of them.

### Technical note on 90° rotation

The Pi's ISP cannot do quarter-turn rotation, so `rotate_90` is handled in a hybrid way:

- **Telegram photos** — rotated in Python with Pillow. These are occasional events, so
  the CPU cost is irrelevant.
- **MJPEG stream** — rotated in the browser with a CSS transform. Cost on the Pi: zero.

Side effect: opening `/stream.mjpg` directly (outside the HTML page) shows the image in
its native orientation. From a normal browser this is not an issue.

Rotating the whole stream in Python would have cost an extra 15–25% CPU on the Zero 2 W,
which is not worth it with 512 MB of RAM and motion detection running alongside.

**If you can, rotate the camera module physically instead of using `rotate_90`.** The
sensor is natively 4:3 landscape — rotating in software gains you no pixels, it just
turns them.

---

## Requirements

Raspberry Pi OS **Lite** (Trixie or later), no desktop environment.

```bash
sudo apt install -y --no-install-recommends \
  python3-picamera2 python3-numpy python3-requests python3-pil
```

---

## Installation

```bash
sudo mkdir -p /opt/picam
sudo cp picam.py /opt/picam/

sudo cp picam.conf.example /etc/picam.conf
sudo nano /etc/picam.conf          # token, chat_id, stream password
sudo chown root:$USER /etc/picam.conf
sudo chmod 640 /etc/picam.conf
```

Validate the JSON before starting:

```bash
python3 -c "import json;json.load(open('/etc/picam.conf'));print('json ok')"
```

Run it in the foreground first:

```bash
python3 /opt/picam/picam.py
```

Then install the service:

```bash
sudo cp picam.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now picam
journalctl -u picam -f
```

Edit `User=` and `SupplementaryGroups=` in `picam.service` to match your own username
before installing it.

---

## Telegram bot

1. Send `/newbot` to [@BotFather](https://t.me/BotFather) and get the token
2. Send any message to your new bot
3. Retrieve your chat id without leaking the token into your shell history:

```bash
TOKEN=$(python3 -c "import json;print(json.load(open('/etc/picam.conf'))['telegram_token'])")
curl -s "https://api.telegram.org/bot$TOKEN/getUpdates" | python3 -m json.tool | grep -A2 '"chat"'
```

### Commands

| Command | Effect |
|---|---|
| `/photo` | Take a photo right now |
| `/stream` | Link to the LAN stream |
| `/pause` | Suspend motion detection |
| `/resume` | Resume motion detection |
| `/status` | Uptime, temperature, free RAM, event count |

Register the command list with BotFather (`/setcommands`) so Telegram offers
autocompletion:

```
photo - Take a snapshot now
stream - LAN stream link
pause - Suspend motion detection
resume - Resume motion detection
status - System status
```

**Pause state is global, not per user.** If one account sends `/pause`, detection stops
for everyone. The other users get no notification of the change — they only see it via
`/status`.

To add a user, put their chat id in the `telegram_chat_ids` array and restart the
service. That user must have messaged the bot at least once, otherwise Telegram will not
deliver anything.

---

## Streaming

```
http://<pi-ip>:8080/
```

Protected by HTTP basic auth (`stream_user` / `stream_pass`).

Endpoints:

- `/` — page with the live stream
- `/stream.mjpg` — raw MJPEG feed
- `/snapshot.jpg` — single frame

The MJPEG encoder only starts when a client connects and shuts down after
`stream_idle_timeout` seconds with no viewers.

**Do not expose port 8080 to the internet.** Basic auth is not encrypted — the password
travels in the clear. Use a VPN for remote access.

The stream is bandwidth-hungry on mobile data (~2–3 Mbps at 1280x960/10fps). From
outside, prefer `/snapshot.jpg`, or lower `stream_quality`.

---

## Tuning motion detection

| Key | Default | Note |
|---|---|---|
| `motion_pixel_threshold` | `28` | How much a pixel must change to count. Raise it (35–40) if sensor noise causes false alerts at night. |
| `motion_area_percent` | `1.2` | Percentage of pixels that must change. Raise it (2–3) if you get too many alerts, lower it (0.5) if nothing triggers. |
| `motion_confirm_frames` | `2` | Consecutive frames required to confirm an event. |
| `motion_check_interval` | `0.4` | Seconds between checks. |
| `alert_cooldown` | `60` | Minimum seconds between two notifications. |
| `warmup_seconds` | `8` | Startup delay to let auto-exposure settle. |

The usual false positives are lighting changes and moving shadows. Let it run for a few
hours before tuning.

---

## Power consumption

With the camera active 24/7 and Wi-Fi associated: **~350–450 mA at 5 V**, roughly 2 W.

A 20000 mAh power bank lasts about 28–32 hours. Software optimisations (LEDs off,
Bluetooth disabled, `framerate: 5`, `motion_check_interval: 1.0`) buy you 15–20% — they
do not change the order of magnitude. Continuous operation needs mains power.

Recommended trimming in `/boot/firmware/config.txt`:

```
dtoverlay=disable-bt
disable_splash=1
dtparam=act_led_trigger=none
dtparam=act_led_activelow=off
```

```bash
sudo systemctl disable --now bluetooth hciuart getty@tty1
sudo apt install -y zram-tools log2ram
```

---

## Known limitations

- No recording: if Telegram is unreachable, the event is lost with no local trace.
- No face recognition or subject classification. With 512 MB of RAM, running an object
  detection model continuously is not practical. Anyone walking past triggers an alert.
- Basic auth supports a single credential pair for the stream.
- Only one process can open the sensor, so no other program can use the camera at the
  same time.

---

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).

Anyone redistributing this code, modified or not, has to keep it under the same licence
and make the source available.
