"""discovery.py -- filesystem discovery for lap folders (Qt-free).

Point at anything: a WinTAX Data root, a track/session, or a single car folder.
`find_lap_dirs` walks directories only (no header parsing) and groups the lap
leaves by car. Lap times/markers are parsed on demand via `lap_meta`, backed by
an mtime-keyed cache so re-opening a car is instant.
"""
from __future__ import annotations
import os, re, json
from .reader import read_lap_header

ZTX_NAMES = ("FlashData.ztx", "cableData.ztx")
_CAR_NUM = re.compile(r"_(\d{1,3})_")


def ztx_in(path: str):
    for z in ZTX_NAMES:
        p = os.path.join(path, z)
        if os.path.exists(p):
            return p
    return None


def is_lap_leaf(path: str) -> bool:
    return ztx_in(path) is not None


def subdirs(path: str) -> list[str]:
    # Alias_ dirs are raw acquisition copies: no Run level, no .ztx, no Car.xml.
    try:
        ds = [e.path for e in os.scandir(path)
              if e.is_dir() and not e.name.startswith("Alias_")]
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


# ---- session metadata (Car.xml / Session.xml next to the laps) ----
def _xml(path: str) -> str:
    try:
        return open(path, "rb").read().decode("latin-1", "replace")
    except OSError:
        return ""


def _tag(txt: str, tag: str) -> str:
    m = re.search(r"<%s>(.*?)</%s>" % (tag, tag), txt, re.S)
    return m.group(1).strip() if m else ""


def car_number(path: str) -> str:
    m = _CAR_NUM.search(os.path.basename(path))
    return m.group(1) if m else ""


def run_info(run_dir: str, cache: dict | None = None) -> dict:
    """Driver, car and session names from the XML WinTAX writes beside each run."""
    if cache is not None and run_dir in cache:
        return cache[run_dir]
    car, ses = _xml(os.path.join(run_dir, "Car.xml")), _xml(os.path.join(run_dir, "Session.xml"))
    info = {"driver": _tag(car, "DriverName"), "car": _tag(car, "Name"),
            "session": _tag(ses, "Name"), "track": _tag(ses, "Track"),
            "number": car_number(_tag(car, "Name") or run_dir)}
    if cache is not None:
        cache[run_dir] = info
    return info


def lap_info(lap_dir: str, cache: dict | None = None) -> dict:
    """run_info for the run this lap belongs to."""
    return run_info(os.path.dirname(lap_dir), cache)


def describe(lap_dir: str, cache: dict | None = None) -> str:
    i = lap_info(lap_dir, cache)
    parts = [i["track"], i["session"]]
    if i["number"]:
        parts.append("Car " + i["number"])
    if i["driver"]:
        parts.append(i["driver"])
    return "  ·  ".join(p for p in parts if p)


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
