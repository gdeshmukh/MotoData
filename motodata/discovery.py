"""discovery.py -- filesystem discovery for lap folders (Qt-free).

Point at anything: a WinTAX Data root, a track/session, or a single car folder.
`find_lap_dirs` walks directories only (no header parsing) and groups the lap
leaves by car. Lap times/markers are parsed on demand via `lap_meta`, backed by
an mtime-keyed cache so re-opening a car is instant.
"""
from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable

from .reader import read_lap_header

ZTX_NAMES = ("FlashData.ztx", "cableData.ztx")
DIRECTORY_CAP = 200000
_CAR_NUM = re.compile(r"_(\d{1,3})_")


class ScanLimitError(RuntimeError):
    def __init__(self, laps, limit):
        self.laps = sorted(laps)
        super().__init__(f"Folder scan stopped after {limit:,} directories.")


def ztx_in(path: str):
    for z in ZTX_NAMES:
        p = os.path.join(path, z)
        if os.path.isfile(p):
            return p
    return None


def is_lap_leaf(path: str) -> bool:
    return ztx_in(path) is not None


def subdirs(path: str, cancelled: Callable[[], bool] | None = None) -> list[str]:
    # Alias_ dirs are raw acquisition copies: no Run level, no .ztx, no Car.xml.
    ds = []
    try:
        with os.scandir(path) as entries:
            for e in entries:
                if cancelled and cancelled():
                    break
                try:
                    junction = getattr(e, "is_junction", None)
                    if (not e.name.startswith("Alias_") and not e.is_symlink()
                            and not (junction and junction())
                            and e.is_dir(follow_symlinks=False)):
                        ds.append(e.path)
                except OSError:
                    continue
    except OSError:
        return []
    ds.sort(key=lambda p: os.path.basename(p).lower())
    return ds


def find_lap_dirs(root: str, cap: int = 20000,
                  cancelled: Callable[[], bool] | None = None, *,
                  directory_cap: int = DIRECTORY_CAP) -> list[str]:
    """Every lap-leaf dir under root (directory walk only, no header parse)."""
    if cap <= 0:
        return []
    out, stack = [], [root]
    seen = set()
    while stack and len(out) < cap and len(seen) < directory_cap:
        if cancelled and cancelled():
            break
        d = stack.pop()
        try:
            stat = os.stat(d)
        except OSError:
            continue
        identity = (stat.st_dev, stat.st_ino) if stat.st_ino else os.path.normcase(os.path.realpath(d))
        if identity in seen:
            continue
        seen.add(identity)
        if is_lap_leaf(d):
            out.append(d)                       # a lap dir holds no sub-laps
        else:
            stack.extend(reversed(subdirs(d, cancelled)))
    if stack and len(out) < cap and not (cancelled and cancelled()):
        raise ScanLimitError(out, directory_cap)
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
        with open(path, "rb") as f:
            return f.read().decode("latin-1", "replace")
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
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
        return cache if isinstance(cache, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def save_cache(path: str, cache: dict):
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        pass
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def lap_header_meta(lap_dir: str, cache: dict):
    """Lap time, marker and distance cached by header mtime."""
    hdr = os.path.join(lap_dir, "LapHeader.xml")
    try:
        mt = os.path.getmtime(hdr)
    except OSError:
        mt = 0.0
    c = cache.get(lap_dir)
    if (isinstance(c, dict) and c.get("mt") == mt
            and "dist" in c and "start" in c):
        lt, marker, distance, start = (c.get(k) for k in ("lt", "mk", "dist", "start"))
        valid = ((lt is None or isinstance(lt, (int, float)) and math.isfinite(lt))
                 and (marker is None or isinstance(marker, str))
                 and (distance is None or isinstance(distance, (int, float))
                      and math.isfinite(distance) and distance > 0)
                 and (start is None or isinstance(start, (int, float))
                      and math.isfinite(start)))
        if valid:
            return lt, marker, distance
    info = read_lap_header(lap_dir)
    lt = info.lap_time if info.lap_time == info.lap_time else None
    cache[lap_dir] = {"mt": mt, "lt": lt, "mk": info.marker,
                      "dist": info.distance, "start": info.start}
    return lt, info.marker, info.distance


def lap_meta(lap_dir: str, cache: dict):
    """Lap time and marker, retained for callers using the original API."""
    lt, marker, _ = lap_header_meta(lap_dir, cache)
    return lt, marker
