"""discovery.py -- filesystem discovery for lap folders (Qt-free).

Point at anything: a WinTAX Data root, a track/session, or a single car folder.
`find_lap_dirs` walks directories only (no header parsing) and groups the lap
leaves by car. Lap times/markers are parsed on demand via `lap_meta`, backed by
an mtime-keyed cache so re-opening a car is instant.
"""
from __future__ import annotations
import os, json
from .reader import read_lap_header

ZTX_NAMES = ("FlashData.ztx", "cableData.ztx")


def ztx_in(path: str):
    for z in ZTX_NAMES:
        p = os.path.join(path, z)
        if os.path.exists(p):
            return p
    return None


def is_lap_leaf(path: str) -> bool:
    return ztx_in(path) is not None


def subdirs(path: str) -> list[str]:
    try:
        ds = [e.path for e in os.scandir(path) if e.is_dir()]
    except OSError:
        return []
    ds.sort(key=lambda p: os.path.basename(p).lower())
    return ds


def find_lap_dirs(root: str, cap: int = 20000) -> list[str]:
    """Every lap-leaf dir under root (directory walk only, no header parse)."""
    out, stack = [], [root]
    while stack and len(out) < cap:
        d = stack.pop()
        if is_lap_leaf(d):
            out.append(d)                       # a lap dir holds no sub-laps
        else:
            stack.extend(subdirs(d))
    out.sort()
    return out


def car_of(lap_dir: str) -> str:
    """The car folder a lap belongs to (…/Car/Run_N/Lap_x -> …/Car)."""
    parent = os.path.dirname(lap_dir)
    if os.path.basename(parent).lower().startswith("run"):
        return os.path.dirname(parent)
    return parent


def group_by_car(lap_dirs: list[str]) -> dict[str, list[str]]:
    cars: dict[str, list[str]] = {}
    for ld in lap_dirs:
        cars.setdefault(car_of(ld), []).append(ld)
    for laps in cars.values():
        laps.sort()
    return cars


# ---- header cache (mtime-keyed) ----
def load_cache(path: str) -> dict:
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}


def save_cache(path: str, cache: dict):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"                  # atomic write so a crash can't truncate it
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, path)
    except Exception:
        pass


def lap_meta(lap_dir: str, cache: dict):
    """(lap_time|None, marker) for a lap, cached by LapHeader.xml mtime."""
    hdr = os.path.join(lap_dir, "LapHeader.xml")
    try:
        mt = os.path.getmtime(hdr)
    except OSError:
        mt = 0.0
    c = cache.get(lap_dir)
    if c and c.get("mt") == mt:
        return c["lt"], c["mk"]
    info = read_lap_header(lap_dir)
    lt = info.lap_time if info.lap_time == info.lap_time else None
    cache[lap_dir] = {"mt": mt, "lt": lt, "mk": info.marker}
    return lt, info.marker
