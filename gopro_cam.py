"""Low-latency GoPro webcam for Windows.

Talks to the GoPro directly over USB (Open GoPro HTTP API), decodes the UDP
stream with ffmpeg using no buffering, and pushes frames into the OBS Virtual
Camera so Discord / Zoom / Teams see it as a normal webcam.

The virtual camera stays open the whole time (showing a "waiting" screen while
the GoPro is off), so apps never need restarting. Plugging in / turning on the
GoPro starts the live picture automatically; stalls and unplugs self-heal.

Usage:
    pythonw gopro_cam.py --background   # tray icon, no window (used at login)
    python  gopro_cam.py                # same engine, logs in the console
    options: --res 480/720/1080  --fov wide/linear/narrow/superview
"""

import argparse
import glob
import json
import logging
import logging.handlers
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import numpy as np
import pyvirtualcam

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(HERE, "gopro-webcam.log")
UDP_PORT = 8554
LOCK_PORT = 47554  # single-instance guard
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
STALL_SECONDS = 6

RES_CODES = {"480": (4, 848, 480), "720": (7, 1280, 720), "1080": (12, 1920, 1080)}
FOV_CODES = {"wide": 0, "narrow": 2, "superview": 3, "linear": 4}

# Image looks: (filters before scaling, filters after scaling). Pre-filters run on
# the camera's full-range signal so brightening can't clip; ~6 ms/frame at 1080p.
LOOKS = {
    "natural": ("", ""),
    "enhanced": ("hqdn3d=2:1.5:4:3,eq=gamma=1.18:saturation=1.08:contrast=1.04,"
                 "unsharp=5:5:0.5:5:5:0", ""),
    "bright": ("hqdn3d=4:3:6:4.5,eq=gamma=1.3:saturation=1.1:contrast=1.06,"
               "unsharp=5:5:0.7:5:5:0", ""),
}


def build_filter(width, height, look):
    pre, post = LOOKS[look]
    # GoPro sends FULL-range video; webcam apps expect LIMITED range. Without
    # out_range=tv the blacks get crushed and highlights clip.
    scale = (f"scale={width}:{height}:flags=lanczos+accurate_rnd+full_chroma_int"
             ":out_range=tv")
    return ",".join(f for f in (pre, scale, post) if f)

# ffmpeg input flags that strip out every buffer we can
LOW_LATENCY_INPUT = [
    "-fflags", "nobuffer+discardcorrupt",
    "-flags", "low_delay",
    "-probesize", "100000",
    "-analyzeduration", "0",
    "-i", f"udp://@0.0.0.0:{UDP_PORT}?overrun_nonfatal=1&fifo_size=1000000&buffer_size=2000000",
]

log = logging.getLogger("gopro")


def run_quiet(cmd, timeout=5):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          creationflags=NO_WINDOW)


# ---------------------------------------------------------------- discovery

def find_ffmpeg():
    found = shutil.which("ffmpeg")
    if found:
        return found
    pattern = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet",
                           "Packages", "Gyan.FFmpeg*", "*", "bin", "ffmpeg.exe")
    hits = glob.glob(pattern)
    return hits[0] if hits else None


def find_camera_ip():
    # GoPro USB networks are 172.2X.1YZ.0/24 and the camera is always .51
    try:
        out = run_quiet(["ipconfig"]).stdout
    except Exception:
        return None
    for ip in re.findall(r"IPv4[^:]*:\s*([\d.]+)", out):
        if re.fullmatch(r"172\.2\d\.1\d\d\.\d+", ip):
            return ip.rsplit(".", 1)[0] + ".51"
    return None


def official_app_running():
    try:
        out = run_quiet(["tasklist", "/FI", "IMAGENAME eq GoPro Webcam.exe"]).stdout
        return "GoPro Webcam.exe" in out
    except Exception:
        return False


def kill_official_app():
    try:
        run_quiet(["taskkill", "/IM", "GoPro Webcam.exe", "/F"])
    except Exception:
        pass


# ---------------------------------------------------------------- camera API

class GoPro:
    def __init__(self, ip):
        self.base = f"http://{ip}:8080"

    def get(self, path, timeout=3):
        with urllib.request.urlopen(self.base + path, timeout=timeout) as r:
            body = r.read().decode(errors="replace")
        try:
            return json.loads(body)
        except ValueError:
            return body

    def try_get(self, path):
        try:
            return self.get(path)
        except Exception as e:
            return e

    def status(self):
        return self.get("/gopro/webcam/status").get("status")

    def start(self, res, fov):
        self.try_get("/gopro/camera/control/wired_usb?p=1")  # needed on HERO12+
        self.try_get("/gopro/webcam/stop")  # force a fresh stream so SPS/PPS is sent
        time.sleep(0.3)
        r = self.get(f"/gopro/webcam/start?res={res}&fov={fov}&port={UDP_PORT}")
        if isinstance(r, dict) and r.get("error", 0) not in (0, None):
            r = self.get(f"/gopro/webcam/start?res={res}&fov={fov}")  # older firmware
        return r

    def stop(self):
        self.try_get("/gopro/webcam/stop")
        self.try_get("/gopro/webcam/exit")


# ---------------------------------------------------------------- GoPro feed

class GoProFeed:
    """Keeps a GoPro stream alive forever and exposes the newest frame."""

    def __init__(self, ffmpeg, width, height, res_code, fov_code, look="enhanced"):
        self.ffmpeg, self.width, self.height = ffmpeg, width, height
        self.vf = build_filter(width, height, look)
        self.res_code, self.fov_code = res_code, fov_code
        self.frame_bytes = width * height * 3 // 2  # NV12
        self.cond = threading.Condition()
        self.frame, self.seq, self.frame_time = None, 0, 0.0
        self.status = "starting"
        self.on_status = lambda s: None
        self.stop_evt = threading.Event()
        self.restart_evt = threading.Event()
        self.proc = None

    def set_status(self, s):
        if s != self.status:
            self.status = s
            log.info(s)
            self.on_status(s)

    def run_forever(self):
        while not self.stop_evt.is_set():
            self.restart_evt.clear()
            ip = find_camera_ip()
            if not ip:
                self.set_status("Waiting for GoPro (plug in + turn on)")
                self.stop_evt.wait(2)
                continue
            cam = GoPro(ip)
            try:
                cam.status()
            except Exception:
                self.set_status("GoPro found, waiting for it to respond")
                self.stop_evt.wait(2)
                continue
            if official_app_running():
                log.info("closing the official GoPro Webcam app (it holds the stream)")
                kill_official_app()
                time.sleep(1.5)
            try:
                self.session(cam)
            except Exception:
                log.exception("stream session crashed")
            if not self.stop_evt.is_set():
                self.stop_evt.wait(1.5)

    def session(self, cam):
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "fatal"] + LOW_LATENCY_INPUT + [
            "-an", "-fps_mode", "passthrough",  # never duplicate frames to fill gaps
            "-vf", self.vf,
            "-pix_fmt", "nv12", "-f", "rawvideo", "pipe:1",
        ]
        # ffmpeg must be listening before the stream starts: the camera only
        # sends SPS/PPS at stream start.
        self.proc = proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            bufsize=0, creationflags=NO_WINDOW)
        session_done = threading.Event()
        started = time.time()
        try:
            time.sleep(0.7)
            self.set_status("Starting GoPro stream")
            cam.start(self.res_code, self.fov_code)
            threading.Thread(target=self.watchdog, args=(cam, proc, started, session_done),
                             daemon=True).start()

            buf = bytearray(self.frame_bytes)
            view = memoryview(buf)
            while not self.stop_evt.is_set():
                got = 0
                while got < self.frame_bytes:
                    n = proc.stdout.readinto(view[got:])
                    if not n:
                        return
                    got += n
                with self.cond:
                    self.frame = bytes(buf)
                    self.seq += 1
                    self.frame_time = time.time()
                    self.cond.notify_all()
                if self.status != "Live":
                    self.set_status("Live")
        finally:
            session_done.set()
            proc.kill()
            cam.stop()

    def watchdog(self, cam, proc, started, session_done):
        tick = 0
        while not session_done.wait(1):
            tick += 1
            last = max(self.frame_time, started + 4)  # grace period for first frame
            if self.stop_evt.is_set() or self.restart_evt.is_set():
                proc.kill()
                return
            if time.time() - last > STALL_SECONDS:
                self.set_status("Stream stalled, restarting")
                proc.kill()
                return
            if tick % 3 == 0:
                cam.try_get("/gopro/camera/keep_alive")
                if official_app_running():
                    log.info("official GoPro app was relaunched (an app has 'GoPro Webcam' "
                             "selected as camera - pick 'OBS Virtual Camera' instead); closing it")
                    kill_official_app()

    def restart(self):
        self.restart_evt.set()

    def stop(self):
        self.stop_evt.set()
        if self.proc:
            self.proc.kill()


# ---------------------------------------------------------------- virtual camera

def rgb_to_nv12(rgb):
    a = rgb.astype(np.float32)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    y = 16 + 0.18259 * r + 0.61423 * g + 0.06201 * b  # BT.709 limited range
    u = 128 - 0.10064 * r - 0.33857 * g + 0.43922 * b
    v = 128 + 0.43922 * r - 0.39894 * g - 0.04027 * b
    h, w = y.shape
    u = u.reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3))
    v = v.reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3))
    uv = np.stack([u, v], axis=-1).reshape(h // 2, w)
    return np.clip(np.concatenate([y, uv]), 0, 255).astype(np.uint8).ravel()


def make_placeholder(width, height, line1, line2):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (width, height), (16, 17, 22))
    d = ImageDraw.Draw(img)

    def font(size):
        for name in ("segoeuib.ttf", "segoeui.ttf", "arial.ttf"):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                pass
        return ImageFont.load_default()

    for text, f, y, col in ((line1, font(height // 16), 0.45, (235, 235, 240)),
                            (line2, font(height // 32), 0.56, (140, 145, 155))):
        w = d.textlength(text, font=f)
        d.text(((width - w) / 2, height * y), text, font=f, fill=col)
    return rgb_to_nv12(np.asarray(img))


def serve_virtualcam(feed, width, height, fps, stop_evt):
    placeholders = {}

    def placeholder():
        key = feed.status
        if key not in placeholders:
            line2 = ("Plug the GoPro in via USB and turn it on" if "Waiting" in key
                     else "Reconnecting..." if key == "Live" else key)
            placeholders[key] = make_placeholder(width, height, "GoPro webcam", line2)
        return placeholders[key]

    while not stop_evt.is_set():
        try:
            vcam = pyvirtualcam.Camera(width, height, fps, fmt=pyvirtualcam.PixelFormat.NV12,
                                       backend="obs")
        except Exception as e:
            log.warning(f"OBS Virtual Camera unavailable ({e}); is OBS using it? retrying in 10 s")
            stop_evt.wait(10)
            continue
        log.info(f"virtual camera '{vcam.device}' open at {width}x{height}")
        last_seq, last_ph, sent, t0 = -1, 0.0, 0, time.time()
        with vcam:
            while not stop_evt.is_set():
                with feed.cond:
                    feed.cond.wait_for(lambda: feed.seq != last_seq or stop_evt.is_set(),
                                       timeout=0.25)
                    seq, frame, ftime = feed.seq, feed.frame, feed.frame_time
                now = time.time()
                if seq != last_seq and now - ftime < 1:
                    last_seq = seq
                    vcam.send(np.frombuffer(frame, np.uint8))
                    sent += 1
                elif now - ftime >= 1 and now - last_ph >= 0.25:
                    vcam.send(placeholder())  # ~4 fps keeps apps from timing out
                    last_ph = now
                if now - t0 >= 60:
                    if sent:
                        log.info(f"{sent / (now - t0):.1f} fps live")
                    sent, t0 = 0, now


# ---------------------------------------------------------------- tray

def icon_image(live, size=64):
    from PIL import Image, ImageDraw
    s = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4 * s, 14 * s, 60 * s, 52 * s), radius=10 * s,
                        fill=(30, 144, 255) if live else (110, 110, 120))
    d.ellipse((20 * s, 19 * s, 44 * s, 47 * s), fill=(255, 255, 255))
    d.ellipse((26 * s, 25 * s, 38 * s, 41 * s), fill=(20, 20, 25))
    return img


def run_tray(feed, stop_evt):
    import pystray

    def on_quit(icon, _):
        stop_evt.set()
        feed.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(lambda _: f"Status: {feed.status}", None, enabled=False),
        pystray.MenuItem("Restart stream", lambda *_: feed.restart()),
        pystray.MenuItem("Open log", lambda *_: os.startfile(LOG_FILE)),
        pystray.MenuItem("Quit", on_quit),
    )
    icon = pystray.Icon("gopro-webcam", icon_image(False), "GoPro webcam", menu)

    def on_status(s):
        icon.icon = icon_image(s == "Live")
        icon.title = f"GoPro webcam: {s}"
        icon.update_menu()

    feed.on_status = on_status
    icon.run()


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Low-latency GoPro webcam")
    ap.add_argument("--res", choices=RES_CODES, default="1080")
    ap.add_argument("--fov", choices=FOV_CODES, default="linear")
    ap.add_argument("--look", choices=LOOKS, default="enhanced",
                    help="natural = untouched, enhanced = denoise + shadow lift + sharpen, "
                         "bright = stronger version for very dark rooms")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--background", action="store_true", help="tray icon, log to file")
    ap.add_argument("--write-icon", metavar="PATH", help=argparse.SUPPRESS)  # used by install.ps1
    args = ap.parse_args()

    if args.write_icon:
        icon_image(True, 256).save(args.write_icon, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
        return 0

    if args.background:
        handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000,
                                                       backupCount=1, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    sys.excepthook = lambda *exc: log.critical("crash", exc_info=exc)

    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        log.info("already running (check the tray icon) - exiting")
        return 0

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log.critical("ffmpeg not found. Install it with: winget install Gyan.FFmpeg")
        return 1

    res_code, width, height = RES_CODES[args.res]
    feed = GoProFeed(ffmpeg, width, height, res_code, FOV_CODES[args.fov], args.look)
    stop_evt = threading.Event()
    log.info(f"GoPro webcam starting: {args.res}p, {args.fov} FOV, {args.look} look")

    feed_thread = threading.Thread(target=feed.run_forever, daemon=True)
    feed_thread.start()
    vcam_thread = threading.Thread(target=serve_virtualcam,
                                   args=(feed, width, height, args.fps, stop_evt), daemon=True)
    vcam_thread.start()

    try:
        if args.background:
            run_tray(feed, stop_evt)
        else:
            log.info("pick 'OBS Virtual Camera' in Discord. Ctrl+C to stop.")
            while vcam_thread.is_alive():
                vcam_thread.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_evt.set()
        feed.stop()
        feed_thread.join(8)  # let the session put the camera back to idle
    return 0


if __name__ == "__main__":
    sys.exit(main())
