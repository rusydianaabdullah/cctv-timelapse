import asyncio
import json
import logging
import shutil
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image

from . import config
from .config import IMAGES_DIR, TIMELAPSE_DIR
from .engine import engine
from . import discover
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@asynccontextmanager
async def lifespan(app):
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    TIMELAPSE_DIR.mkdir(parents=True, exist_ok=True)
    engine.start()
    yield
    engine.stop.set()
    for s in [*engine.streams.values(), *engine.live.values()]:
        s.close()


app = FastAPI(title="CCTV Mini Monitor", lifespan=lifespan)
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/sw.js")
def service_worker():
    # served from root so the worker's scope covers the whole app
    return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


def safe_day(day: str) -> str:
    try:
        return date.fromisoformat(day).isoformat()
    except ValueError:
        raise HTTPException(400, "bad date")


def safe_name(name: str) -> str:
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(400, "bad name")
    return name


# ---------- live ----------
@app.get("/")
def live(request: Request):
    return templates.TemplateResponse(request, "live.html", {"cfg": config.load(), "page": "live"})


@app.websocket("/ws/live/{i}")
async def ws_live(ws: WebSocket, i: int):
    """Push each new realtime JPEG frame of camera i as a binary message."""
    await ws.accept()
    s = engine.live_acquire(i)
    if s is None:
        await ws.close(code=4404)
        return
    try:
        last, quiet = 0.0, 0.0
        while True:
            if s.raw and s.frame_time != last:
                last = s.frame_time
                await ws.send_bytes(s.raw)
                quiet = 0.0
            elif quiet > 5:  # keep-alive; also how a closed tab is noticed
                await ws.send_text("wait")
                quiet = 0.0
            await asyncio.sleep(0.04)
            quiet += 0.04
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        engine.live_release(s)


@app.get("/api/collage.jpg")
def collage():
    data = engine.collage_jpeg
    if not data:
        raise HTTPException(503, "no frame yet")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/camera/{i}.jpg")
def camera(i: int):
    data = engine.camera_jpeg(i)
    if not data:
        raise HTTPException(404)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/status")
def status():
    return {"cameras": engine.status, "last_event": engine.last_event, "jobs": engine.jobs}


# ---------- media ----------
@app.get("/media/{day}/{name}")
def media(day: str, name: str):
    p = IMAGES_DIR / safe_day(day) / safe_name(name)
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)


THUMB_DIR = IMAGES_DIR.parent / "thumbs"


@app.get("/thumb/{day}/{name}")
def thumb(day: str, name: str):
    day, name = safe_day(day), safe_name(name)
    src = IMAGES_DIR / day / name
    if not src.is_file():
        raise HTTPException(404)
    t = THUMB_DIR / day / name
    if not t.is_file() or t.stat().st_mtime < src.stat().st_mtime:
        t.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im.thumbnail((320, 320))
            im.convert("RGB").save(t, "JPEG", quality=70)
    return FileResponse(t, headers={"Cache-Control": "max-age=86400"})


# ---------- pictures ----------
@app.get("/pictures")
def pictures(request: Request, day: str = ""):
    days = sorted((d.name for d in IMAGES_DIR.iterdir() if d.is_dir()), reverse=True) if IMAGES_DIR.exists() else []
    day = safe_day(day) if day else (days[0] if days else "")
    files = sorted(p.name for p in (IMAGES_DIR / day).glob("*.jpg")) if day and (IMAGES_DIR / day).is_dir() else []
    i = days.index(day) if day in days else -1
    nav = {"newer": days[i - 1] if i > 0 else None, "older": days[i + 1] if 0 <= i < len(days) - 1 else None}
    return templates.TemplateResponse(request, "pictures.html",
                                      {"day": day, "days": days, "files": files, "nav": nav, "page": "pic"})


@app.post("/pictures/{day}/delete")
def delete_day(day: str):
    p = IMAGES_DIR / safe_day(day)
    if p.is_dir():
        shutil.rmtree(p)
    shutil.rmtree(THUMB_DIR / safe_day(day), ignore_errors=True)
    return RedirectResponse("/timelapse", 303)


# ---------- timelapse ----------
@app.get("/timelapse")
def timelapse(request: Request):
    vids = {p.stem: p.stat().st_size for p in TIMELAPSE_DIR.glob("*.mp4") if not p.name.startswith(".")}
    pics = {d.name: sum(1 for _ in d.glob("*.jpg")) for d in IMAGES_DIR.iterdir() if d.is_dir()} if IMAGES_DIR.exists() else {}
    # one row per day that has a video and/or pictures
    rows = [{"day": d, "size": vids.get(d), "pics": pics.get(d, 0), "job": engine.jobs.get(d)}
            for d in sorted(vids.keys() | pics.keys(), reverse=True)]
    return templates.TemplateResponse(request, "timelapse.html", {"rows": rows, "jobs": engine.jobs,
                                                                   "today": date.today().isoformat(), "page": "tl"})


@app.post("/timelapse/generate")
def generate(day: str = Form(...), back: str = Form("/timelapse")):
    engine.make_timelapse_async(safe_day(day))
    return RedirectResponse(back if back.startswith("/") else "/timelapse", 303)


@app.get("/timelapse/file/{name}")
def timelapse_file(name: str):
    p = TIMELAPSE_DIR / safe_name(name)
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, media_type="video/mp4")


@app.post("/timelapse/{name}/delete")
def delete_timelapse(name: str):
    (TIMELAPSE_DIR / safe_name(name)).unlink(missing_ok=True)
    return RedirectResponse("/timelapse", 303)


@app.post("/cleanup")
def cleanup():
    engine.cleanup()
    return RedirectResponse("/timelapse", 303)


# ---------- config ----------
@app.get("/config")
def config_page(request: Request, msg: str = ""):
    cfg = config.load()
    return templates.TemplateResponse(request, "config.html", {"cfg": cfg, "raw": json.dumps(cfg, indent=2),
                                                               "msg": msg, "page": "cfg",
                                                               "subnet": os.environ.get("SCAN_SUBNET", "192.168.1.0/24")})


@app.post("/config")
async def config_save(request: Request):
    form = await request.form()
    cfg = config.load()
    ids, names, urls = form.getlist("cam_id"), form.getlist("cam_name"), form.getlist("cam_url")
    enabled = set(form.getlist("cam_enabled"))
    cfg["cameras"] = [{"id": cid, "name": n.strip() or f"CAM {i + 1:02d}", "url": u.strip(), "enabled": str(i) in enabled}
                      for i, (cid, n, u) in enumerate(zip(ids, names, urls)) if u.strip()]
    config.ensure_ids(cfg)

    def num(key, cast=int):
        return cast(form.get(key))

    try:
        cfg["capture"].update(interval_seconds=num("interval_seconds", float), cooldown_seconds=num("cooldown_seconds", float),
                              timeout_seconds=num("timeout_seconds"),
                              heartbeat_enabled="heartbeat_enabled" in form, heartbeat_minutes=num("heartbeat_minutes", float))
        cfg["motion"].update(compare_width=num("compare_width"), compare_height=num("compare_height"),
                             pixel_threshold=num("pixel_threshold"), change_percent=num("change_percent", float))
        cfg["timelapse"].update(fps=num("fps", float), auto_generate="auto_generate" in form,
                                generate_time=form.get("generate_time") or "00:05")
        cfg["live"].update(fps=max(1.0, min(25.0, num("live_fps", float))), width=max(160, min(1920, num("live_width"))) // 2 * 2)
        cfg["retention"].update(images_days=num("images_days"), timelapse_days=num("timelapse_days"))
    except (TypeError, ValueError) as e:
        return RedirectResponse(f"/config?msg=Invalid value: {e}", 303)
    config.save(cfg)
    engine.small.clear()
    engine.frames.clear()
    engine.status.clear()
    return RedirectResponse("/config?msg=Saved", 303)


# ---------- layout ----------
@app.get("/layout")
def layout_page(request: Request):
    cfg = config.load()
    cams = [{"id": c["id"], "name": c["name"], "index": i, "enabled": c.get("enabled", True)}
            for i, c in enumerate(cfg["cameras"])]
    return templates.TemplateResponse(request, "layout.html", {"cfg": cfg, "cams": cams, "page": "lay"})


@app.post("/api/layout")
async def layout_save(request: Request):
    body = await request.json()
    cfg = config.load()
    ids = {c["id"] for c in cfg["cameras"]}

    def frac(v):
        return min(1.0, max(0.0, float(v)))

    def clean(cards):
        return [{"cam": c["cam"], "x": frac(c["x"]), "y": frac(c["y"]), "w": frac(c["w"]), "h": frac(c["h"])}
                for c in cards if c.get("cam") in ids]

    try:
        cards = clean(body.get("cards", []))
        if "live" in body:
            pages = [{"name": str(p.get("name") or f"Page {i + 1}")[:40], "cards": clean(p.get("cards", []))}
                     for i, p in enumerate(body["live"].get("pages", []))]
            cfg["live_layout"]["pages"] = pages or [{"name": "Page 1", "cards": []}]
        cfg["layout"].update(width=max(160, min(7680, int(body["width"]))) // 2 * 2,
                             height=max(90, min(4320, int(body["height"]))) // 2 * 2,
                             jpeg_quality=max(10, min(95, int(body["jpeg_quality"]))),
                             show_labels=bool(body.get("show_labels")), cards=cards)
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(400, f"Invalid layout: {e}")
    config.save(cfg)
    return {"ok": True}


@app.post("/config/raw")
def config_raw(raw: str = Form(...)):
    try:
        config.save(json.loads(raw))
    except json.JSONDecodeError as e:
        return RedirectResponse(f"/config?msg=Invalid JSON: {e}", 303)
    engine.small.clear()
    engine.frames.clear()
    engine.status.clear()
    return RedirectResponse("/config?msg=Saved", 303)


@app.post("/api/discover")
def api_discover(subnet: str = Form(...), user: str = Form(""), password: str = Form("")):
    try:
        return {"results": discover.scan(subnet.strip(), user.strip(), password)}
    except ValueError as e:
        raise HTTPException(400, str(e))
