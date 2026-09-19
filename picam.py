#!/usr/bin/env python3
"""
picam - Raspberry Pi Zero 2 W security camera
  - continuous motion detection on the lores stream (numpy)
  - Telegram alerts with a photo
  - on-demand MJPEG streaming over the LAN
Nothing is written to the microSD card: everything lives in /dev/shm.
"""

import base64
import io
import json
import logging
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import requests
from picamera2 import Picamera2
from picamera2.encoders import JpegEncoder
from picamera2.outputs import FileOutput

CONF_PATH = os.environ.get("PICAM_CONF", "/etc/picam.conf")
TMP = "/dev/shm"

# ---------------------------------------------------------------- config

DEFAULTS = {
    "telegram_token": "",
    "telegram_chat_ids": [],
    "location": "living room",
    "main_size": [1280, 960],
    "lores_size": [320, 240],
    "framerate": 10,
    "stream_port": 8080,
    "stream_user": "cam",
    "stream_pass": "changeme",
    "stream_quality": 60,
    "stream_idle_timeout": 300,
    "motion_pixel_threshold": 28,
    "motion_area_percent": 1.2,
    "motion_confirm_frames": 2,
    "motion_check_interval": 0.4,
    "alert_cooldown": 60,
    "warmup_seconds": 8,
    "rotate_180": False,
    "rotate_90": 0,
}


def load_conf():
    cfg = dict(DEFAULTS)
    try:
        with open(CONF_PATH) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        sys.exit(f"Missing config: {CONF_PATH}")
    except json.JSONDecodeError as e:
        sys.exit(f"Invalid config: {e}")
    if not cfg["telegram_token"] or not cfg["telegram_chat_ids"]:
        sys.exit("telegram_token and telegram_chat_ids are required")
    return cfg


CFG = load_conf()
API = f"https://api.telegram.org/bot{CFG['telegram_token']}"
CHAT_IDS = [int(c) for c in CFG["telegram_chat_ids"]]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("picam")

# ---------------------------------------------------------------- state

state = {
    "paused": False,
    "clients": 0,
    "last_client": 0.0,
    "last_alert": 0.0,
    "events": 0,
    "started": time.time(),
}
lock = threading.Lock()

# ---------------------------------------------------------------- camera

picam2 = Picamera2()
MW, MH = CFG["main_size"]
LW, LH = CFG["lores_size"]

_tr = {}
if CFG["rotate_180"]:
    from libcamera import Transform

    _tr = {"transform": Transform(hflip=1, vflip=1)}

picam2.configure(
    picam2.create_video_configuration(
        main={"size": (MW, MH), "format": "YUV420"},
        lores={"size": (LW, LH), "format": "YUV420"},
        controls={"FrameRate": CFG["framerate"]},
        **_tr,
    )
)
picam2.start()
log.info("camera started: main %dx%d, lores %dx%d", MW, MH, LW, LH)


class StreamingOutput(io.BufferedIOBase):
    """Holds the latest JPEG frame and wakes up every waiting client."""

    def __init__(self):
        self.frame = None
        self.cond = threading.Condition()

    def write(self, buf):
        with self.cond:
            self.frame = buf
            self.cond.notify_all()


output = StreamingOutput()
encoder = JpegEncoder(q=CFG["stream_quality"])
enc_lock = threading.Lock()
encoding = False


def encoder_start():
    """Start the MJPEG encoder on the first connected client."""
    global encoding
    with enc_lock:
        if not encoding:
            picam2.start_encoder(encoder, FileOutput(output))
            encoding = True
            log.info("MJPEG encoder ON")


def encoder_stop():
    """Stop the encoder once nobody is watching."""
    global encoding
    with enc_lock:
        if encoding:
            picam2.stop_encoder()
            encoding = False
            output.frame = None
            log.info("MJPEG encoder OFF")


cap_lock = threading.Lock()


def snapshot(path):
    """Grab a still from the main stream. The file stays in RAM."""
    with cap_lock:
        picam2.capture_file(path)
    rot = CFG.get("rotate_90", 0)
    if rot:
        # The ISP cannot rotate by a quarter turn, so stills are rotated here.
        # The live stream is rotated in the browser with CSS instead.
        from PIL import Image

        img = Image.open(path)
        img.rotate(-rot, expand=True).save(path, quality=85)
    return path


# ---------------------------------------------------------------- telegram


def tg(method, **kw):
    try:
        r = requests.post(f"{API}/{method}", timeout=30, **kw)
        return r.json()
    except Exception as e:
        log.warning("telegram %s failed: %s", method, e)
        return None


def tg_text(text, chat_id=None):
    targets = [chat_id] if chat_id else CHAT_IDS
    for c in targets:
        tg("sendMessage", data={"chat_id": c, "text": text})


def tg_photo(path, caption, chat_id=None):
    targets = [chat_id] if chat_id else CHAT_IDS
    for c in targets:
        try:
            with open(path, "rb") as f:
                tg(
                    "sendPhoto",
                    data={"chat_id": c, "caption": caption},
                    files={"photo": f},
                )
        except OSError as e:
            log.warning("photo not sent: %s", e)


def lan_ip():
    """Local address, resolved without sending anything on the wire."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def uptime_str(sec):
    d, r = divmod(int(sec), 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    return f"{d}d {h}h {m}m" if d else f"{h}h {m}m"


def cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read()) / 1000
    except OSError:
        return 0.0


def mem_free_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


# ---------------------------------------------------------------- bot commands


def cmd_status(chat_id):
    with lock:
        paused, clients, events = state["paused"], state["clients"], state["events"]
        up = time.time() - state["started"]
    tg_text(
        "\n".join(
            [
                f"Camera {CFG['location']}",
                f"state: {'PAUSED' if paused else 'active'}",
                f"uptime: {uptime_str(up)}",
                f"events: {events}",
                f"stream clients: {clients}",
                f"temp: {cpu_temp():.1f}C  free RAM: {mem_free_mb()}MB",
            ]
        ),
        chat_id,
    )


def cmd_photo(chat_id):
    path = f"{TMP}/manual.jpg"
    try:
        snapshot(path)
        tg_photo(path, f"Manual snapshot - {CFG['location']}", chat_id)
    except Exception as e:
        tg_text(f"Snapshot failed: {e}", chat_id)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def cmd_stream(chat_id):
    tg_text(
        f"Stream (LAN only):\nhttp://{lan_ip()}:{CFG['stream_port']}/\n"
        f"user: {CFG['stream_user']}",
        chat_id,
    )


HELP = (
    "/photo - take a snapshot now\n"
    "/stream - LAN stream link\n"
    "/pause - suspend motion detection\n"
    "/resume - resume motion detection\n"
    "/status - system status"
)


def handle(text, chat_id):
    cmd = text.split()[0].split("@")[0].lower()
    if cmd in ("/start", "/help"):
        tg_text(HELP, chat_id)
    elif cmd == "/photo":
        cmd_photo(chat_id)
    elif cmd == "/stream":
        cmd_stream(chat_id)
    elif cmd == "/pause":
        with lock:
            state["paused"] = True
        tg_text("Motion detection suspended.", chat_id)
    elif cmd == "/resume":
        with lock:
            state["paused"] = False
            # Reset the cooldown so the first post-resume event is not swallowed.
            state["last_alert"] = time.time()
        tg_text("Motion detection resumed.", chat_id)
    elif cmd == "/status":
        cmd_status(chat_id)


def telegram_loop():
    """Long-polling loop. Only the configured chat ids are accepted."""
    offset = None
    while True:
        try:
            r = requests.get(
                f"{API}/getUpdates",
                params={"timeout": 50, "offset": offset},
                timeout=60,
            ).json()
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat_id = msg.get("chat", {}).get("id")
                text = msg.get("text", "")
                if chat_id in CHAT_IDS and text.startswith("/"):
                    handle(text, chat_id)
        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            log.warning("polling: %s", e)
            time.sleep(5)


# ---------------------------------------------------------------- motion


def motion_loop():
    """Frame differencing on the lores stream. Cheap enough to run forever."""
    prev = None
    hits = 0
    area_px = LW * LH * CFG["motion_area_percent"] / 100.0
    time.sleep(CFG["warmup_seconds"])
    log.info("motion detection active (threshold %.0f px)", area_px)

    while True:
        time.sleep(CFG["motion_check_interval"])
        with lock:
            paused = state["paused"]
        if paused:
            prev = None
            continue

        try:
            # YUV420: the first LH rows are the luma plane, which is all we need.
            cur = picam2.capture_array("lores")[:LH, :LW].astype(np.int16)
        except Exception as e:
            log.warning("lores capture: %s", e)
            time.sleep(2)
            continue

        if prev is None:
            prev = cur
            continue

        changed = int(
            np.count_nonzero(np.abs(cur - prev) > CFG["motion_pixel_threshold"])
        )
        prev = cur

        if changed < area_px:
            hits = 0
            continue

        hits += 1
        if hits < CFG["motion_confirm_frames"]:
            continue
        hits = 0

        now = time.time()
        with lock:
            if now - state["last_alert"] < CFG["alert_cooldown"]:
                continue
            state["last_alert"] = now
            state["events"] += 1

        log.info("motion detected (%d px)", changed)
        path = f"{TMP}/alert.jpg"
        try:
            snapshot(path)
            tg_photo(
                path,
                f"Motion detected in {CFG['location']} - "
                f"{time.strftime('%d/%m %H:%M:%S')}",
            )
        except Exception as e:
            log.warning("alert failed: %s", e)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


# ---------------------------------------------------------------- http

_ROT = CFG.get("rotate_90", 0)
_CSS_ROT = (
    f"transform:rotate({_ROT}deg);max-height:100vw;max-width:100vh"
    if _ROT in (90, 270)
    else f"transform:rotate({_ROT}deg);max-width:100%"
)

PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Camera {loc}</title>
<style>body{{background:#111;color:#ccc;font-family:sans-serif;margin:0}}
.wrap{{display:flex;justify-content:center;align-items:center;
height:100vh;overflow:hidden}}
.wrap img{{{css}}}</style></head>
<body><div class="wrap"><img src="/stream.mjpg"></div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Silence the default per-request logging.
        pass

    def _auth_ok(self):
        want = base64.b64encode(
            f"{CFG['stream_user']}:{CFG['stream_pass']}".encode()
        ).decode()
        got = self.headers.get("Authorization", "")
        if got == "Basic " + want:
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="picam"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if not self._auth_ok():
            return

        if self.path in ("/", "/index.html"):
            body = PAGE.format(loc=CFG["location"], css=_CSS_ROT).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/snapshot.jpg":
            path = f"{TMP}/http.jpg"
            try:
                snapshot(path)
                with open(path, "rb") as f:
                    body = f.read()
                os.remove(path)
            except Exception:
                self.send_error(500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/stream.mjpg":
            with lock:
                state["clients"] += 1
                state["last_client"] = time.time()
            encoder_start()
            self.send_response(200)
            self.send_header("Age", "0")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header(
                "Content-Type", "multipart/x-mixed-replace; boundary=FRAME"
            )
            self.end_headers()
            try:
                while True:
                    with output.cond:
                        output.cond.wait(timeout=5)
                        frame = output.frame
                    if frame is None:
                        continue
                    self.wfile.write(b"--FRAME\r\n")
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(frame)))
                    self.end_headers()
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError, OSError):
                # Client went away: normal, nothing to report.
                pass
            finally:
                with lock:
                    state["clients"] -= 1
                    state["last_client"] = time.time()
        else:
            self.send_error(404)


def idle_watch():
    """Shut the encoder down after the configured idle timeout."""
    while True:
        time.sleep(10)
        with lock:
            idle = state["clients"] == 0
            since = time.time() - state["last_client"]
        if idle and encoding and since > CFG["stream_idle_timeout"]:
            encoder_stop()


# ---------------------------------------------------------------- main


def main():
    for target in (telegram_loop, motion_loop, idle_watch):
        threading.Thread(target=target, daemon=True).start()

    tg_text(f"Camera {CFG['location']} online.")
    srv = ThreadingHTTPServer(("", CFG["stream_port"]), Handler)
    srv.daemon_threads = True
    log.info("http on :%d", CFG["stream_port"])
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        encoder_stop()
        picam2.stop()


if __name__ == "__main__":
    main()
