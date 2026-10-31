# CCTV Mini Monitor

A lightweight, self-hosted CCTV monitor for RTSP cameras. It records a motion-triggered collage of all cameras, builds a daily timelapse video, and shows a realtime multi-camera view in the browser. It installs on your phone as a PWA.

No AI, no OpenCV, no database. It only needs **ffmpeg** and **Pillow**, and all settings live in one `config.json`.

```
RTSP ──ffmpeg──▶ low-res JPEG ──Pillow pixel-diff──▶ motion? ──▶ collage saved
                                                              └▶ daily ffmpeg timelapse
RTSP ──ffmpeg (on demand)──▶ JPEG frames ──WebSocket──▶ Live page (realtime)
```

## Features

- **Live page**
  - **Last capture**: the latest recording collage, refreshed every capture interval.
  - **Realtime**: live camera streams over WebSocket, arranged by your own *Live layout*, with multiple pages. A stream only runs while someone is watching it.
- **Layout editor**: drag-and-resize cards on a snap grid, in two modes.
  - **Recording**: the collage that gets saved, with output resolution presets (360p to 4K), JPEG quality and labels.
  - **Live**: screen-sized pages for the Realtime view.
- **Motion detection**: compares pixels in a small grayscale copy of each frame. Sensitivity presets go from *Very high* to *Very low*, or you can set custom values.
- **Heartbeat**: saves a frame every N minutes even with no motion, so timelapses have no gaps.
- **Pictures**: a day-by-day frame viewer with a scrubbable timeline, playback at 1–30 fps and keyboard shortcuts (← → Space Home End).
- **Timelapse**
  - The daily MP4 is generated automatically after midnight.
  - Each day has its own row with Play (full screen), Download, Regenerate, Delete video and Delete pictures.
- **Retention**: pictures and videos are deleted automatically after N days.
- **Config UI**
  - Cameras, including an RTSP scanner that probes common Hikvision, Dahua, Tapo and Reolink paths.
  - Capture, motion, realtime, timelapse and retention settings.
  - A raw JSON editor.
- **Responsive and installable**: there's a mobile menu, and the app can be installed as a PWA on Android and iOS.

## Quick start (Docker)

```bash
git clone https://github.com/rusydianaabdullah/cctv-timelapse.git
cd cctv-timelapse
docker compose up -d --build
```

Open `http://<host>:8003`, go to **Config** and add your cameras, then arrange them under **Layout**.

### docker-compose.yml

```yaml
services:
  cctv:
    build: .
    container_name: cctv-mini
    restart: unless-stopped
    user: "1000:1000"          # owner of ./data on the host
    ports:
      - "8003:8000"
    environment:
      - TZ=Asia/Jakarta        # folder dates and the timelapse schedule use this zone
      - SCAN_SUBNET=192.168.0.0/24   # default subnet for the RTSP scanner
    volumes:
      - ./data:/data
```

### Run without Docker

You need Python 3.12+ and `ffmpeg` on your PATH.

```bash
pip install -r requirements.txt
CONFIG_PATH=data/config.json DATA_DIR=data uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Install as an app (PWA)

Browsers only allow installing from **HTTPS** (or `localhost`), so put the app behind a reverse proxy with TLS, such as Caddy, nginx, Traefik or Cloudflare Tunnel. The proxy must forward WebSocket connections for `/ws/` so Realtime works.

- **Android (Chrome)**: menu ⋮ → *Install app*
- **iOS (Safari)**: Share → *Add to Home Screen*

Example nginx location:

```nginx
location / {
    proxy_pass http://127.0.0.1:8003;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
}
```

## Camera tips

- Use the camera's **sub-stream** to save CPU and bandwidth. Examples:
  - Hikvision: `rtsp://user:pass@ip:554/Streaming/Channels/102`
  - Dahua: `rtsp://user:pass@ip:554/cam/realmonitor?channel=1&subtype=1`
- While Realtime is being watched, each camera has **two** RTSP connections open: recording and live. If a camera starts reporting *NO SIGNAL*, it may only allow one or two connections. Lower the Realtime fps/width, or watch fewer cameras at once.
- Each camera gets a stable `id`, so renaming or reordering cameras keeps your layouts.

## Data layout

Everything lives in `./data`, mounted at `/data` in the container:

```
data/
├── config.json              # all settings (contains camera credentials!)
├── cctv/YYYY-MM-DD/HHMMSS.jpg   # saved collages (motion + heartbeat)
├── thumbs/YYYY-MM-DD/…      # cached thumbnails for the Pictures timeline
└── timelapse/YYYY-MM-DD.mp4 # daily timelapse videos
```

`config.json` is created with defaults on first start. You can edit it in the UI (**Config → Raw JSON**) or by hand. Its main sections are `cameras`, `layout` (recording), `live_layout` (realtime pages), `live`, `capture`, `motion`, `timelapse` and `retention`.

> ⚠️ `data/` holds camera passwords and footage. It is git-ignored — never commit it.

## Project structure

```
app/
├── main.py        # FastAPI routes: pages, API, WebSocket, media
├── engine.py      # capture loop, motion, collage, realtime streams, timelapse, retention
├── config.py      # config.json load/save with defaults
├── discover.py    # RTSP network scanner
├── static/        # PWA manifest, service worker, icons
└── templates/     # Jinja2 pages: live, layout, pictures, timelapse, config
```

## API (short)

| Method | Path | Description |
|---|---|---|
| GET | `/api/collage.jpg` | latest recording collage |
| GET | `/api/camera/{i}.jpg` | latest frame of camera *i* |
| GET | `/api/status` | per-camera online/motion status, last event, jobs |
| WS | `/ws/live/{i}` | realtime JPEG frames of camera *i* (binary messages) |
| POST | `/api/layout` | save recording + live layouts (JSON) |
| POST | `/api/discover` | scan a subnet for RTSP streams |

## Security

There is **no built-in login**. Run it on a trusted LAN, or put it behind a reverse proxy with authentication (basic auth, Authelia, Cloudflare Access, a VPN such as Tailscale, …). Don't expose it to the internet as-is.

## License

MIT
