"""
reader.py -- standalone reader for binary motorsport logger session files.

Reads logged sessions directly with no vendor software required.

Layout (no extra tools needed):
  Data/<Track>/<Session>/<Car>/Run_<N>/Lap_<abs>_<id>/FlashData.ztx
    FlashData.ztx is a plain ZIP archive containing:
      - dltable.t04   channel-definition table (binary)
      - lap.bin       raw interleaved logger stream
      - lapheader.bin small per-lap header
      - <Channel>.sar one file per channel: a headerless array of
                      little-endian float64 values, already in engineering units.

  Sample rate is inferred from sample count and lap time. Nominal rates are
  matched against STD_RATES.

  Metadata lives in sibling XML files (LapHeader.xml etc.). The XML can contain
  stray binary bytes in the <Alias> field, so we parse fields with regex, not a
  strict XML parser.
"""
from __future__ import annotations

import glob
import os
import re
import zipfile
from dataclasses import dataclass

import numpy as np

STD_RATES = (0.5, 1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000)
SPEED_CHANNELS = ("vCar", "CarSpd_vCar", "GPS_CarSpeed")


def _field(txt: str, tag: str):
    m = re.search(r"<%s>(.*?)</%s>" % (tag, tag), txt, re.S)
    return m.group(1).strip() if m else None


def is_flying_marker(marker: str | None) -> bool:
    return (marker or "").strip().casefold() not in {"in", "out", "box"}


@dataclass
class LapInfo:
    path: str          # lap directory
    ztx: str           # FlashData.ztx path
    lap_time: float
    marker: str | None
    run: str | None
    lap: str | None
    distance: float | None
    start: int | None = None       # lap start, unix epoch seconds (unambiguous)

    @property
    def is_flying(self):
        return is_flying_marker(self.marker)


def read_lap_header(lap_dir: str) -> LapInfo:
    hdr = os.path.join(lap_dir, "LapHeader.xml")
    with open(hdr, "rb") as f:
        txt = f.read().decode("latin-1", "replace")
    lt = _field(txt, "LapTime")
    try:
        lt = float(lt)
        if not np.isfinite(lt):
            lt = float("nan")
    except (TypeError, ValueError):
        lt = float("nan")
    try:
        distance = float(_field(txt, "LapDistance"))
        if not np.isfinite(distance) or distance <= 0:
            distance = None
    except (TypeError, ValueError):
        distance = None
    ztx = os.path.join(lap_dir, "FlashData.ztx")
    if not os.path.isfile(ztx):
        ztx = os.path.join(lap_dir, "cableData.ztx")
    # STS is epoch seconds; the textual dates disagree on day/month order between
    # LapHeader.xml (DD/MM) and RunLapHeader.xml (MM/DD), so never parse those.
    try:
        start = int(_field(txt, "STS"))
    except (TypeError, ValueError):
        start = None
    return LapInfo(lap_dir, ztx, lt, _field(txt, "Marker"),
                   _field(txt, "Run"), _field(txt, "Lap"),
                   distance, start)


def find_laps(car_dir: str) -> list[LapInfo]:
    laps = []
    for hdr in glob.glob(os.path.join(car_dir, "Run_*", "Lap_*", "LapHeader.xml")):
        laps.append(read_lap_header(os.path.dirname(hdr)))
    laps.sort(key=lambda l: l.lap_time if l.lap_time == l.lap_time else float("inf"))
    return laps


def lap_moved(ztx_path: str, lap_time: float, min_kmh: float = 30.0,
              lap_distance: float | None = None) -> bool:
    if not ztx_path or not os.path.exists(ztx_path):
        return False
    try:
        with Lap(ztx_path, lap_time, lap_distance) as lap:
            speed_name = next((name for name in SPEED_CHANNELS
                               if name in lap.channels()), None)
            if speed_name is None:
                return True
            speed = lap.raw(speed_name)
            speed = speed[np.isfinite(speed)]
            return bool(len(speed) and np.median(speed) > min_kmh)
    except (OSError, KeyError, RuntimeError, ValueError, zipfile.BadZipFile):
        return False


def _moved(info: LapInfo, min_kmh: float = 30.0) -> bool:
    return lap_moved(info.ztx, info.lap_time, min_kmh, info.distance)


def fastest_flying_lap(car_dir: str) -> LapInfo:
    # Markers and speed reject obvious pit or garage fragments.
    for info in find_laps(car_dir):
        if info.is_flying and info.lap_time > 0 and _moved(info):
            return info
    raise ValueError("no flying laps under " + car_dir)


class Lap:
    """One lap of telemetry, read straight from the .ztx ZIP."""

    def __init__(self, ztx_path: str, lap_time: float | None = None,
                 lap_distance: float | None = None):
        self.ztx_path = ztx_path
        self.zip = zipfile.ZipFile(ztx_path, "r")
        if lap_time is None or lap_distance is None:
            try:
                info = read_lap_header(os.path.dirname(ztx_path))
            except OSError:
                if lap_time is None:
                    self.zip.close()
                    raise
            else:
                lap_time = info.lap_time if lap_time is None else lap_time
                lap_distance = info.distance if lap_distance is None else lap_distance
        self.lap_time = lap_time
        self.lap_distance = lap_distance
        self._sizes = {i.filename[:-4]: i.file_size
                       for i in self.zip.infolist() if i.filename.endswith(".sar")}
        self._time_axes: dict[int, np.ndarray] = {}

    def close(self):
        self._time_axes.clear()
        self.zip.close()

    def retain_time_axes(self, names):
        keep = {self.n_samples(name) for name in names if name in self._sizes}
        for count in list(self._time_axes):
            if count not in keep:
                del self._time_axes[count]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def channels(self) -> list[str]:
        return sorted(self._sizes)

    def n_samples(self, name: str) -> int:
        return self._sizes[name] // 8

    def rate(self, name: str) -> float:
        """Exact (un-snapped) sample rate in Hz; nan if lap_time is unusable."""
        lt = self.lap_time
        if not lt or not np.isfinite(lt) or lt <= 0:
            return float("nan")
        return self.n_samples(name) / lt

    def rate_snapped(self, name: str) -> float:
        r = self.rate(name)
        if r != r or r <= 0:
            return float("nan")
        nominal = min(STD_RATES, key=lambda s: abs(s - r))
        return nominal if abs(nominal - r) / nominal <= 0.1 else r

    def raw(self, name: str) -> np.ndarray:
        data = self.zip.read(name + ".sar")
        if len(data) % 8:
            raise ValueError(f"invalid float64 channel size: {name}")
        return np.frombuffer(data, dtype="<f8").copy()

    def time_axis(self, name: str) -> np.ndarray:
        n = self.n_samples(name)
        t = self._time_axes.get(n)
        if t is not None:
            return t
        lt = self.lap_time
        if not lt or not np.isfinite(lt) or lt <= 0:
            raise ValueError("lap time is unavailable")
        # .sar has no timestamps; distribute samples across the lap.
        t = np.arange(n, dtype=float) * (lt / n) if n else np.empty(0)
        t.setflags(write=False)
        self._time_axes[n] = t
        return t

    def channel(self, name: str):
        """Return elapsed seconds and stored engineering values."""
        values = self.raw(name)
        return self.time_axis(name), values

    def rate_table(self) -> list[tuple[str, int, float, float]]:
        out = []
        for nm in self.channels():
            out.append((nm, self.n_samples(nm), self.rate(nm), self.rate_snapped(nm)))
        return out
