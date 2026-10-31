import copy
import json
import os
import threading
import uuid
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "data/config.json"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
IMAGES_DIR = DATA_DIR / "cctv"
TIMELAPSE_DIR = DATA_DIR / "timelapse"

DEFAULTS = {
    "cameras": [],
    # cards: [{"cam": <camera id>, "x", "y", "w", "h"}] as 0-1 fractions of the output canvas
    "layout": {"width": 1920, "height": 1080, "jpeg_quality": 80, "show_labels": True, "cards": []},
    # Live page: screen-sized pages, cards as 0-1 fractions of the screen area
    "live": {"fps": 10, "width": 960},
    "live_layout": {"pages": [{"name": "Page 1", "cards": []}]},
    "capture": {"interval_seconds": 5, "cooldown_seconds": 10, "timeout_seconds": 10,
                "heartbeat_enabled": True, "heartbeat_minutes": 15},
    "motion": {"compare_width": 320, "compare_height": 180, "pixel_threshold": 25, "change_percent": 1.5},
    "timelapse": {"fps": 5, "auto_generate": True, "generate_time": "00:05"},
    "retention": {"images_days": 7, "timelapse_days": 30},
}

_lock = threading.Lock()


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    with _lock:
        if not CONFIG_PATH.exists():
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2))
            return copy.deepcopy(DEFAULTS)
        cfg = _merge(DEFAULTS, json.loads(CONFIG_PATH.read_text()))
    if ensure_ids(cfg):
        save(cfg)
    return cfg


def ensure_ids(cfg: dict) -> bool:
    """Give each camera a stable id so layout cards survive renames and reordering."""
    changed = False
    for c in cfg["cameras"]:
        if not c.get("id"):
            c["id"] = uuid.uuid4().hex[:8]
            changed = True
    if cfg.pop("collage", None) is not None:
        changed = True
    return changed


def save(cfg: dict) -> None:
    with _lock:
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2))
        tmp.replace(CONFIG_PATH)
