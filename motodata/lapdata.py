"""lapdata.py -- per-lap analysis wrapper (Qt-free).

Reads and caches only the channels asked for, maps a channel's samples onto a
time or distance x-axis, projects the GPS trace to local metres against a shared
origin (so two laps overlay correctly), and exposes the time-vs-distance mapping
used for the delta-time trace. One LapData owns one open Lap and closes it.
"""
from __future__ import annotations

import zipfile

import numpy as np

from .reader import Lap, SPEED_CHANNELS

DIST_CHANNELS = ("sLap", "In_xDistanceLap")
GPS_LAT, GPS_LON = "GPS_Latitude", "GPS_Longitude"
_EARTH_R = 6371000.0
_DEG = np.pi / 180.0


def _distance_axis(v: np.ndarray, lap_length: float | None = None) -> np.ndarray:
    raw = np.asarray(v, float)
    good = np.isfinite(raw)
    if good.sum() < 2:
        return np.empty(0)
    index = np.flatnonzero(good)
    values = raw[good]
    steps = np.diff(values)
    length = lap_length if lap_length and np.isfinite(lap_length) else None
    if length:
        steps[steps < -0.5 * length] += length
    else:
        span = np.ptp(values)
        if span and np.any(steps < -0.5 * span):
            return np.empty(0)
    finite_d = np.concatenate(([0.0], np.cumsum(steps)))
    np.maximum.accumulate(finite_d, out=finite_d)
    return np.interp(np.arange(len(raw)), index, finite_d)


class LapData:
    def __init__(self, ztx_path: str, lap_time: float | None = None,
                 label: str = "", *, lap_distance: float | None = None):
        self.ztx_path = ztx_path
        self.label = label
        self.lap = Lap(ztx_path, lap_time, lap_distance)
        self.lap_time = self.lap.lap_time
        self.channels = set(self.lap.channels())
        self._cache: dict[str, tuple] = {}
        self._errors: dict[str, str] = {}
        self._xcache: dict[int, np.ndarray] = {}
        self._dist = None
        self._dist_inv = None
        self._dist_ready = False
        self._gps_raw_data = None
        self._gps_ready = False
        self._gpsx: dict[str, np.ndarray] = {}
        self._origin = None
        self._proj = None

    def close(self):
        self.lap.close()
        self._cache.clear()
        self._errors.clear()
        self._xcache.clear()
        self._gpsx.clear()
        self._dist = self._dist_inv = None
        self._gps_raw_data = self._proj = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- channels ----
    def has(self, name: str) -> bool:
        return name in self.channels

    def ty(self, name: str):
        v = self._cache.get(name)
        if v is None:
            try:
                v = self.lap.channel(name)
            except (OSError, KeyError, RuntimeError, ValueError, zipfile.BadZipFile) as e:
                self._errors[name] = str(e)
                v = np.empty(0), np.empty(0)
            self._cache[name] = v
        return v

    def channel_error(self, name: str):
        return self._errors.get(name)

    def retain_channels(self, names):
        keep = set(names)
        for name in list(self._cache):
            if name not in keep:
                del self._cache[name]
                self._errors.pop(name, None)
        counts = {self.lap.n_samples(name) for name in keep if name in self.channels}
        for count in list(self._xcache):
            if count not in counts:
                del self._xcache[count]
        self.lap.retain_time_axes(keep)

    def rate(self, name: str) -> float:
        return self.lap.rate_snapped(name)

    # ---- distance base ----
    def _build_dist(self):
        self._dist_ready = True
        for c in DIST_CHANNELS:
            if c in self.channels:
                try:
                    t, v = self.lap.channel(c)
                except (OSError, KeyError, RuntimeError, ValueError, zipfile.BadZipFile):
                    continue
                d = _distance_axis(v, self.lap.lap_distance)
                if len(d) < 2:
                    continue
                length = self.lap.lap_distance
                plausible = not length or 0.95 * length <= d[-1] <= 1.05 * length
                if np.isfinite(d[-1]) and d[-1] > 1.0 and plausible:
                    self._set_dist(t, d)
                    return
        for speed_name in SPEED_CHANNELS:
            if speed_name not in self.channels:
                continue
            t, v = self.ty(speed_name)
            good = np.isfinite(t) & np.isfinite(v)
            t, v = t[good], v[good]
            if len(t) > 1:
                if t[0] > 0:
                    t, v = np.insert(t, 0, 0.0), np.insert(v, 0, v[0])
                if self.lap_time and np.isfinite(self.lap_time) and t[-1] < self.lap_time:
                    t, v = np.append(t, self.lap_time), np.append(v, v[-1])
                speed = np.maximum((v[:-1] + v[1:]) * 0.5, 0) / 3.6
                d = np.concatenate(([0.0], np.cumsum(np.diff(t) * speed)))
                length = self.lap.lap_distance
                plausible = not length or 0.8 * length <= d[-1] <= 1.2 * length
                if np.isfinite(d[-1]) and d[-1] > 1.0 and plausible:
                    self._set_dist(t, d)
                    return

    def _set_dist(self, t: np.ndarray, d: np.ndarray):
        n = min(len(t), len(d))
        t, d = t[:n], d[:n]
        good = np.isfinite(t) & np.isfinite(d)
        t, d = t[good], d[good]
        if len(d) < 2:
            return
        lt, length = self.lap_time, self.lap.lap_distance
        if lt and np.isfinite(lt) and t[-1] < lt:
            t = np.append(t, lt)
            endpoint = length if length and d[-1] <= length else d[-1]
            d = np.append(d, endpoint)
        self._dist = (t, d)
        keep = np.r_[np.diff(d) > 1e-9, True]
        self._dist_inv = (d[keep], t[keep])

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
        dv, dt = self._dist_inv
        return float(np.interp(d, dv, dt))

    def x(self, name: str, mode: str) -> np.ndarray:
        t = self.ty(name)[0]
        if mode != "dist" or not self.has_distance:
            return t
        count = len(t)
        v = self._xcache.get(count)
        if v is None:
            dt, dv = self._dist
            v = np.interp(t, dt, dv)
            self._xcache[count] = v
        return v

    def xy(self, name: str, mode: str):
        return self.x(name, mode), self.ty(name)[1]

    def value_at(self, name: str, xq: float, mode: str):
        if name not in self.channels:
            return None
        x, y = self.xy(name, mode)
        if not len(x):
            return None
        value = float(np.interp(xq, x, y))
        return value if np.isfinite(value) else None

    def xmax(self, mode: str) -> float:
        if mode == "dist" and self.has_distance:
            return self.dist_max()
        lt = self.lap_time
        if lt and lt == lt and lt > 0:
            return float(lt)
        for c in ("vCar", *self._cache):
            if c in self.channels:
                t = self.ty(c)[0]
                if len(t):
                    return float(t[-1])
        return 1.0

    def t_of_distance(self, dgrid: np.ndarray):
        if not self.has_distance:
            return None
        dv, dt = self._dist_inv
        return np.interp(dgrid, dv, dt)

    # ---- GPS ground track (local metres, shared origin) ----
    def _gps_raw(self):
        if not self._gps_ready:
            self._gps_ready = True
            if GPS_LAT in self.channels and GPS_LON in self.channels:
                try:
                    tg, lat = self.lap.channel(GPS_LAT)
                    tlon, lon = self.lap.channel(GPS_LON)
                except (OSError, KeyError, RuntimeError, ValueError, zipfile.BadZipFile):
                    return None
                lon_ok = np.isfinite(tlon) & np.isfinite(lon)
                if lon_ok.sum() < 2:
                    return None
                lon = np.interp(tg, tlon[lon_ok], lon[lon_ok])
                ok = (np.isfinite(lat) & np.isfinite(lon)
                      & (np.abs(lat) <= 90) & (np.abs(lon) <= 180)
                      & ((np.abs(lat) > 1e-6) | (np.abs(lon) > 1e-6)))
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
        """GPS sample positions on the x-axis. Cached like x(): the cursor asks for
        this on every move, and it does not change when the origin moves."""
        p = self._project()
        if p is None:
            return None
        v = self._gpsx.get(mode)
        if v is None:
            if mode == "dist" and self.has_distance:
                dt, dv = self._dist
                v = np.interp(p[0], dt, dv)
            else:
                v = p[0]
            self._gpsx[mode] = v
        return v

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
