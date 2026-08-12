"""lapdata.py -- per-lap analysis wrapper (Qt-free).

Reads and caches only the channels asked for, maps a channel's samples onto a
time or distance x-axis, projects the GPS trace to local metres against a shared
origin (so two laps overlay correctly), and exposes the time-vs-distance mapping
used for the delta-time trace. One LapData owns one open Lap and closes it.
"""
from __future__ import annotations
import numpy as np
from .reader import Lap

DIST_CHANNELS = ("sLap", "In_xDistanceLap")
GPS_LAT, GPS_LON = "GPS_Latitude", "GPS_Longitude"
_EARTH_R = 6371000.0
_DEG = np.pi / 180.0


def _unwrap_distance(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, float).copy()
    if len(v) < 2:
        return v
    j = int(np.argmin(np.diff(v)))                     # biggest drop = lap reset
    if np.diff(v)[j] < -0.5 * float(v.max()):
        v[:j + 1] -= v[j]
    return np.maximum.accumulate(v)                    # force monotonic for interp


class LapData:
    def __init__(self, ztx_path: str, lap_time: float | None = None, label: str = ""):
        self.ztx_path = ztx_path
        self.label = label
        self.lap = Lap(ztx_path, lap_time)
        self.lap_time = self.lap.lap_time
        self.channels = set(self.lap.channels())
        self._cache: dict[str, tuple] = {}
        self._xcache: dict[tuple, np.ndarray] = {}
        self._dist = None
        self._dist_ready = False
        self._gps_raw_data = None
        self._gps_ready = False
        self._origin = None
        self._proj = None

    def close(self):
        self.lap.close()
        self._cache.clear()
        self._xcache.clear()

    # ---- channels ----
    def has(self, name: str) -> bool:
        return name in self.channels

    def ty(self, name: str):
        v = self._cache.get(name)
        if v is None:
            v = self.lap.channel(name)
            self._cache[name] = v
        return v

    def rate(self, name: str) -> float:
        return self.lap.rate_snapped(name)

    # ---- distance base ----
    def _build_dist(self):
        self._dist_ready = True
        for c in DIST_CHANNELS:
            if c in self.channels:
                t, v = self.lap.channel(c)
                v = _unwrap_distance(v)
                if v[-1] > 1.0:
                    self._dist = (t, v)
                    return
        if "vCar" in self.channels:                    # fallback: integrate speed
            t, v = self.ty("vCar")
            d = np.concatenate([[0.0], np.cumsum(np.diff(t) * np.maximum(v[:-1], 0) / 3.6)])
            self._dist = (t, np.maximum.accumulate(d))

    @property
    def has_distance(self) -> bool:
        if not self._dist_ready:
            self._build_dist()
        return self._dist is not None

    def dist_max(self) -> float:
        return float(self._dist[1][-1]) if self.has_distance else 0.0

    def to_dist(self, t: float) -> float:
        if not self.has_distance:
            return t
        dt, dv = self._dist
        return float(np.interp(t, dt, dv))

    def to_time(self, d: float) -> float:
        if not self.has_distance:
            return d
        dt, dv = self._dist
        return float(np.interp(d, dv, dt))

    def x(self, name: str, mode: str) -> np.ndarray:
        key = (name, mode)
        v = self._xcache.get(key)
        if v is None:
            t = self.ty(name)[0]
            if mode == "dist" and self.has_distance:
                dt, dv = self._dist
                v = np.interp(t, dt, dv)
            else:
                v = t
            self._xcache[key] = v
        return v

    def xy(self, name: str, mode: str):
        return self.x(name, mode), self.ty(name)[1]

    def value_at(self, name: str, xq: float, mode: str):
        if name not in self.channels:
            return None
        x, y = self.xy(name, mode)
        return float(np.interp(xq, x, y)) if len(x) else None

    def xmax(self, mode: str) -> float:
        if mode == "dist" and self.has_distance:
            return self.dist_max()
        lt = self.lap_time
        if lt and lt == lt and lt > 0:
            return float(lt)
        for c in ("vCar", *self._cache):
            if c in self.channels:
                return float(self.ty(c)[0][-1])
        return 1.0

    def t_of_distance(self, dgrid: np.ndarray):
        if not self.has_distance:
            return None
        dt, dv = self._dist
        return np.interp(dgrid, dv, dt)

    # ---- GPS ground track (local metres, shared origin) ----
    def _gps_raw(self):
        if not self._gps_ready:
            self._gps_ready = True
            if GPS_LAT in self.channels and GPS_LON in self.channels:
                tg, lat = self.lap.channel(GPS_LAT)
                _, lon = self.lap.channel(GPS_LON)
                n = min(len(lat), len(lon), len(tg))
                tg, lat, lon = tg[:n], lat[:n], lon[:n]
                ok = (np.isfinite(lat) & np.isfinite(lon)
                      & (np.abs(lat) > 1e-6) & (np.abs(lon) > 1e-6))
                if ok.sum() > 10:
                    self._gps_raw_data = (tg[ok], lat[ok], lon[ok])
        return self._gps_raw_data

    def gps_origin(self):
        r = self._gps_raw()
        return None if r is None else (float(np.mean(r[1])), float(np.mean(r[2])))

    def set_origin(self, lat0: float, lon0: float):
        if self._origin != (lat0, lon0):
            self._origin = (lat0, lon0)
            self._proj = None

    def _project(self):
        r = self._gps_raw()
        if r is None:
            return None
        if self._proj is None:
            tg, lat, lon = r
            lat0, lon0 = self._origin or (float(np.mean(lat)), float(np.mean(lon)))
            x = (lon - lon0) * np.cos(lat0 * _DEG) * _DEG * _EARTH_R
            y = (lat - lat0) * _DEG * _EARTH_R
            self._proj = (tg, x, y)
        return self._proj

    def _gps_x(self, mode: str):
        p = self._project()
        if p is None:
            return None
        if mode == "dist" and self.has_distance:
            dt, dv = self._dist
            return np.interp(p[0], dt, dv)
        return p[0]

    def gps_track(self):
        p = self._project()
        return None if p is None else (p[1], p[2])

    def gps_point(self, xq: float, mode: str):
        p = self._project()
        if p is None:
            return None
        gx = self._gps_x(mode)
        return float(np.interp(xq, gx, p[1])), float(np.interp(xq, gx, p[2]))

    def nearest_x(self, px: float, py: float, mode: str):
        p = self._project()
        if p is None:
            return None
        i = int(np.argmin((p[1] - px) ** 2 + (p[2] - py) ** 2))
        return float(self._gps_x(mode)[i])
