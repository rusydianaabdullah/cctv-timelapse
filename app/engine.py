"""Capture loop: RTSP -> low-res JPEG (ffmpeg) -> motion diff (Pillow) -> collage."""
import io
import math
import logging
import shutil
import subprocess
import threading
import time
from datetime import date, datetime, timedelta

from PIL import Image, ImageChops, ImageDraw

from . import config
from .config import IMAGES_DIR, TIMELAPSE_DIR

log = logging.getLogger("cctv")


class CameraStream:
    """One long-lived ffmpeg per camera; keeps the latest decoded frame.

    Re-opening RTSP every tick costs a handshake + keyframe wait (2-30 s) and leaves
    half-closed sessions on cameras with few session slots, which showed up as NO SIGNAL.
    """

    def __init__(self, url: str, width: int, height: int, fps: float = 1, decode: bool = True):
        self.url, self.width, self.height, self.fps, self.decode = url, width, height, fps, decode
        self.frame: Image.Image | None = None
        self.raw: bytes | None = None  # latest JPEG as received from ffmpeg
        self.viewers = 0
        self.idle_since = time.time()
        self.frame_time = 0.0
        self.started = 0.0
        self.proc: subprocess.Popen | None = None
        self.closed = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _cmd(self):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if self.url.startswith("rtsp"):
            cmd += ["-rtsp_transport", "tcp", "-timeout", "10000000"]  # 10 s socket timeout (us)
        return cmd + [
            "-i", self.url, "-an", "-vf", f"fps={self.fps},scale={self.width}:{self.height}:force_original_aspect_ratio=decrease",
            "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "5", "-",
        ]

    def _run(self):
        backoff = 2
        while not self.closed.is_set():
            self.started = time.time()
            got = False
            try:
                self.proc = subprocess.Popen(self._cmd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                buf = b""
                while chunk := self.proc.stdout.read1(65536):
                    buf += chunk
                    # mjpeg pipe = back-to-back JPEGs; keep only the newest complete one
                    end = buf.rfind(b"\xff\xd9")
                    if end < 0:
                        continue
                    start = buf.rfind(b"\xff\xd8", 0, end)
                    if start >= 0:
                        try:
                            self.raw = buf[start:end + 2]
                            if self.decode:
                                self.frame = Image.open(io.BytesIO(self.raw)).convert("RGB")
                            self.frame_time = time.time()
                            got = True
                        except Exception:
                            pass
                    buf = buf[end + 2:]
                err = self.proc.stderr.read().decode(errors="replace").strip()
                if not self.closed.is_set():
                    log.warning("stream %s ended: %s", self.url, err[-200:] or f"rc={self.proc.wait()}")
            except Exception:
                log.exception("stream %s failed", self.url)
            finally:
                self.kill()
            backoff = 2 if got else min(backoff * 2, 30)
            self.closed.wait(backoff)

    def kill(self):
        p = self.proc
        if p and p.poll() is None:
            p.terminate()  # SIGTERM lets ffmpeg send RTSP TEARDOWN so the camera frees the session slot
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    def latest(self, max_age: float) -> Image.Image | None:
        now = time.time()
        # watchdog: ffmpeg can hang on a dead RTSP session without exiting;
        # allow a longer grace for the first frame (handshake + keyframe wait)
        connecting = self.frame_time < self.started
        if now - max(self.frame_time, self.started) > (max(max_age, 45) if connecting else max_age):
            self.kill()
        return self.frame if now - self.frame_time <= max_age else None

    def close(self):
        self.closed.set()
        self.kill()


def placeholder(w, h, text):
    img = Image.new("RGB", (w, h), (30, 30, 30))
    ImageDraw.Draw(img).text((10, h // 2 - 6), text, fill=(200, 80, 80))
    return img


def change_percent(prev: Image.Image, cur: Image.Image, pixel_threshold: int) -> float:
    diff = ImageChops.difference(prev, cur).point(lambda p: 255 if p > pixel_threshold else 0)
    hist = diff.histogram()
    return hist[255] * 100.0 / (cur.width * cur.height)


def card_box(card, W, H):
    """Card fractions -> pixel box (x, y, w, h) on a W x H canvas."""
    x, y = round(card["x"] * W), round(card["y"] * H)
    return x, y, max(1, round((card["x"] + card["w"]) * W) - x), max(1, round((card["y"] + card["h"]) * H) - y)


def layout_cards(lay, cam_ids):
    """Saved cards for enabled cameras; auto grid when no layout is saved yet."""
    cards = [c for c in lay.get("cards", []) if c.get("cam") in cam_ids]
    if cards or lay.get("cards"):
        return cards
    n = len(cam_ids)
    cols = max(1, math.ceil(math.sqrt(n)))
    rows = max(1, math.ceil(n / cols))
    return [{"cam": cid, "x": (k % cols) / cols, "y": (k // cols) / rows, "w": 1 / cols, "h": 1 / rows}
            for k, cid in enumerate(cam_ids)]


def stream_sizes(cards, lay):
    """Decode each camera at the size of its largest card so nothing is upscaled."""
    sizes = {}
    for c in cards:
        _, _, w, h = card_box(c, lay["width"], lay["height"])
        pw, ph = sizes.get(c["cam"], (0, 0))
        sizes[c["cam"]] = (max(pw, w, 64) // 2 * 2, max(ph, h, 64) // 2 * 2)
    return sizes


class Engine:
    def __init__(self):
        self.lock = threading.Lock()
        self.frames: dict[int, Image.Image] = {}
        self.small: dict[int, Image.Image] = {}
        self.status: dict[int, dict] = {}
        self.collage_jpeg: bytes | None = None
        self.last_save = 0.0
        self.last_event = ""
        self.stop = threading.Event()
        self.streams: dict[tuple, CameraStream] = {}
        self.jobs: dict[str, str] = {}
        self._last_daily = None
        self.live: dict[int, CameraStream] = {}  # on-demand realtime streams, by camera index

    # ---------- realtime ----------
    def live_acquire(self, i: int) -> CameraStream | None:
        """Shared realtime stream for camera i; opened on first viewer."""
        cfg = config.load()
        if not 0 <= i < len(cfg["cameras"]):
            return None
        url, lv = cfg["cameras"][i]["url"], cfg["live"]
        with self.lock:
            s = self.live.get(i)
            if s is None or s.url != url or s.fps != lv["fps"] or s.width != lv["width"]:
                if s:
                    s.close()
                s = self.live[i] = CameraStream(url, lv["width"], lv["width"], lv["fps"], decode=False)
            s.viewers += 1
            return s

    def live_release(self, s: CameraStream):
        with self.lock:
            s.viewers -= 1
            s.idle_since = time.time()

    def live_reap(self, idle: float = 20):
        """Close realtime streams nobody has watched for `idle` seconds (frees camera RTSP slots)."""
        with self.lock:
            for i, s in list(self.live.items()):
                if s.viewers <= 0 and time.time() - s.idle_since > idle:
                    s.close()
                    del self.live[i]

    # ---------- capture ----------
    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._scheduler, daemon=True).start()

    def _loop(self):
        while not self.stop.is_set():
            t0 = time.time()
            try:
                self.live_reap()
                self.tick()
            except Exception:
                log.exception("tick failed")
            interval = max(1, config.load()["capture"]["interval_seconds"])
            self.stop.wait(max(0.2, interval - (time.time() - t0)))

    def tick(self):
        cfg = config.load()
        lay, cap, mo = cfg["layout"], cfg["capture"], cfg["motion"]
        cams = [(i, c) for i, c in enumerate(cfg["cameras"]) if c.get("enabled", True)]
        cards = layout_cards(lay, [c["id"] for _, c in cams])
        sizes = stream_sizes(cards, lay)
        keys = {i: (c["url"], *sizes.get(c["id"], (640, 360))) for i, c in cams}
        wanted = set(keys.values())
        for key in list(self.streams):
            if key not in wanted:
                self.streams.pop(key).close()
        for key in wanted:
            if key not in self.streams:
                self.streams[key] = CameraStream(*key)
        # a frame older than this counts as offline (first connect of a slow HEVC cam can take ~30 s)
        max_age = max(cap["timeout_seconds"], 3 * cap["interval_seconds"])

        motion_cams = []
        tiles = {}
        for i, c in cams:
            img = self.streams[keys[i]].latest(max_age)
            st = {"name": c["name"], "online": img is not None, "change": 0.0, "time": datetime.now().strftime("%H:%M:%S")}
            if img is None:
                img = self.frames[i] if i in self.frames else placeholder(*keys[i][1:], "NO SIGNAL")
            else:
                small = img.convert("L").resize((mo["compare_width"], mo["compare_height"]))
                prev = self.small.get(i)
                if prev is not None and prev.size == small.size:
                    st["change"] = round(change_percent(prev, small, mo["pixel_threshold"]), 2)
                    if st["change"] >= mo["change_percent"]:
                        motion_cams.append(c["name"])
                self.small[i] = small
                self.frames[i] = img
            st["motion"] = c["name"] in motion_cams
            self.status[i] = st
            tiles[c["id"]] = (c["name"], img, st)

        collage = self.build_collage(tiles, cards, lay)
        buf = io.BytesIO()
        collage.save(buf, "JPEG", quality=lay["jpeg_quality"])
        with self.lock:
            self.collage_jpeg = buf.getvalue()

        now = time.time()
        motion = motion_cams and now - self.last_save >= cap["cooldown_seconds"]
        # Heartbeat: keep at least one frame every N minutes, even when nothing moves.
        heartbeat = (cap.get("heartbeat_enabled") and cams
                     and now - self.last_save >= cap.get("heartbeat_minutes", 15) * 60)
        if motion or heartbeat:
            dt = datetime.now()
            d = IMAGES_DIR / dt.strftime("%Y-%m-%d")
            d.mkdir(parents=True, exist_ok=True)
            (d / dt.strftime("%H%M%S.jpg")).write_bytes(self.collage_jpeg)
            self.last_save = now
            reason = f"motion: {', '.join(motion_cams)}" if motion else "heartbeat"
            self.last_event = f"{dt:%Y-%m-%d %H:%M:%S} {reason}"
            log.info(self.last_event)

    def build_collage(self, tiles, cards, lay):
        W, H = lay["width"], lay["height"]
        if not cards:
            return placeholder(W, H, "No cameras in layout")
        out = Image.new("RGB", (W, H), (0, 0, 0))
        draw = ImageDraw.Draw(out)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for card in cards:
            if card["cam"] not in tiles:
                continue
            name, img, st = tiles[card["cam"]]
            x, y, w, h = card_box(card, W, H)
            fit = img.copy()
            fit.thumbnail((w, h))  # keep aspect ratio, letterbox inside the card
            out.paste(fit, (x + (w - fit.width) // 2, y + (h - fit.height) // 2))
            if lay.get("show_labels", True):
                label = f"{name}  {ts}"
                draw.rectangle([x, y, x + 8 + 6 * len(label), y + 16], fill=(0, 0, 0))
                draw.text((x + 4, y + 3), label, fill=(255, 60, 60) if st["motion"] else (255, 255, 255))
        return out

    def camera_jpeg(self, i: int) -> bytes | None:
        img = self.frames.get(i)
        if img is None:
            return None
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        return buf.getvalue()

    # ---------- timelapse ----------
    def make_timelapse(self, day: str) -> str:
        src = IMAGES_DIR / day
        imgs = sorted(src.glob("*.jpg")) if src.is_dir() else []
        if not imgs:
            self.jobs[day] = "no images"
            return self.jobs[day]
        self.jobs[day] = "running"
        TIMELAPSE_DIR.mkdir(parents=True, exist_ok=True)
        fps = config.load()["timelapse"]["fps"]
        # lock output to the largest frame size of the day (even dims); smaller frames are scaled up and padded
        def size(p):
            with Image.open(p) as im:
                return im.size
        w, h = (d // 2 * 2 for d in max((size(p) for p in imgs), key=lambda s: s[0] * s[1]))
        out = TIMELAPSE_DIR / f"{day}.mp4"
        tmp = TIMELAPSE_DIR / f".{day}.tmp.mp4"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            # one image = one frame; image2pipe avoids concat-demuxer timestamp issues that dropped frames
            "-f", "image2pipe", "-framerate", str(fps), "-c:v", "mjpeg", "-i", "-",
            "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1", "-r", str(fps), "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(tmp),
        ]
        r = subprocess.run(cmd, input=b"".join(p.read_bytes() for p in imgs), capture_output=True)
        if r.returncode == 0:
            tmp.replace(out)
            self.jobs[day] = f"done ({len(imgs)} frames)"
        else:
            tmp.unlink(missing_ok=True)
            self.jobs[day] = "error: " + r.stderr.decode(errors="replace")[-300:]
        log.info("timelapse %s: %s", day, self.jobs[day])
        return self.jobs[day]

    def make_timelapse_async(self, day: str):
        if self.jobs.get(day) == "running":
            return
        self.jobs[day] = "running"
        threading.Thread(target=self.make_timelapse, args=(day,), daemon=True).start()

    # ---------- retention / scheduler ----------
    def cleanup(self):
        r = config.load()["retention"]
        today = date.today()
        for base, days, is_dir in ((IMAGES_DIR, r["images_days"], True), (IMAGES_DIR.parent / "thumbs", r["images_days"], True), (TIMELAPSE_DIR, r["timelapse_days"], False)):
            if not days or days <= 0 or not base.exists():
                continue
            cutoff = today - timedelta(days=days)
            for p in base.iterdir():
                try:
                    d = date.fromisoformat(p.stem if not is_dir else p.name)
                except ValueError:
                    continue
                if d < cutoff:
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
                    log.info("retention: deleted %s", p)

    def _scheduler(self):
        while not self.stop.wait(30):
            try:
                cfg = config.load()["timelapse"]
                now = datetime.now()
                if now.strftime("%H:%M") >= cfg["generate_time"] and self._last_daily != now.date():
                    self._last_daily = now.date()
                    if cfg["auto_generate"]:
                        self.make_timelapse((now.date() - timedelta(days=1)).isoformat())
                    self.cleanup()
            except Exception:
                log.exception("scheduler failed")


engine = Engine()
