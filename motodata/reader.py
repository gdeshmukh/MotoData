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

  Sample rate of a channel = (#samples) / lap_time, which snaps cleanly to
  a standard rate (1,2,5,10,20,50,100,200,500 Hz).

  Metadata lives in sibling XML files (LapHeader.xml etc.). The XML can contain
  stray binary bytes in the <Alias> field, so we parse fields with regex, not a
  strict XML parser.
"""
from __future__ import annotations
import os, re, glob, zipfile, array
from dataclasses import dataclass
import numpy as np

STD_RATES = [0.5, 1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000]

# Channels whose stored value carries a known fixed offset vs. the displayed
# engineering value. Verified against physics (speed vs. gear extremes).
# real_gear = raw - 4  -> top gear 6 at max speed, 3rd at the slowest corner.
GEAR_OFFSET = 4


def _field(txt: str, tag: str):
    m = re.search(r"<%s>(.*?)</%s>" % (tag, tag), txt, re.S)
    return m.group(1).strip() if m else None


@dataclass
class LapInfo:
    path: str          # lap directory
    ztx: str           # FlashData.ztx path
    lap_time: float
    marker: str | None
    run: str | None
    lap: str | None
    distance: str | None

    @property
    def is_flying(self):
        # a flying/timed lap has no In/Box marker
        return self.marker not in ("Out", "Box", "in", "In")


def read_lap_header(lap_dir: str) -> LapInfo:
    hdr = os.path.join(lap_dir, "LapHeader.xml")
    txt = open(hdr, "rb").read().decode("latin-1", "replace")
    lt = _field(txt, "LapTime")
    try:
        lt = float(lt)
    except (TypeError, ValueError):
        lt = float("nan")
    ztx = os.path.join(lap_dir, "FlashData.ztx")
    if not os.path.exists(ztx):
        ztx = os.path.join(lap_dir, "cableData.ztx")
    return LapInfo(lap_dir, ztx, lt, _field(txt, "Marker"),
                   _field(txt, "Run"), _field(txt, "Lap"),
                   _field(txt, "LapDistance"))


def find_laps(car_dir: str) -> list[LapInfo]:
    laps = []
    for hdr in glob.glob(os.path.join(car_dir, "Run_*", "Lap_*", "LapHeader.xml")):
        laps.append(read_lap_header(os.path.dirname(hdr)))
    laps.sort(key=lambda l: l.lap_time)
    return laps


def fastest_flying_lap(car_dir: str) -> LapInfo:
    laps = [l for l in find_laps(car_dir) if l.is_flying and l.lap_time > 0]
    if not laps:
        raise ValueError("no flying laps under " + car_dir)
    return laps[0]


class Lap:
    """One lap of telemetry, read straight from the .ztx ZIP."""

    def __init__(self, ztx_path: str, lap_time: float | None = None):
        self.ztx_path = ztx_path
        self.zip = zipfile.ZipFile(ztx_path)
        if lap_time is None:
            info = read_lap_header(os.path.dirname(ztx_path))
            lap_time = info.lap_time
        self.lap_time = lap_time
        self._sizes = {i.filename[:-4]: i.file_size
                       for i in self.zip.infolist() if i.filename.endswith(".sar")}

    def channels(self) -> list[str]:
        return sorted(self._sizes)

    def n_samples(self, name: str) -> int:
        return self._sizes[name] // 8

    def rate(self, name: str) -> float:
        """Exact (un-snapped) sample rate in Hz."""
        return self.n_samples(name) / self.lap_time

    def rate_snapped(self, name: str) -> float:
        r = self.rate(name)
        return min(STD_RATES, key=lambda s: abs(s - r))

    def raw(self, name: str) -> np.ndarray:
        a = array.array("d")
        a.frombytes(self.zip.read(name + ".sar"))
        return np.frombuffer(a, dtype=np.float64).copy()

    def channel(self, name: str):
        """Return (time_s, values) with the gear offset applied for nGear."""
        y = self.raw(name)
        if name == "nGear":
            y = y - GEAR_OFFSET
        t = np.arange(len(y)) / self.rate_snapped(name)
        return t, y

    def rate_table(self) -> list[tuple[str, int, float, float]]:
        out = []
        for nm in self.channels():
            out.append((nm, self.n_samples(nm), self.rate(nm), self.rate_snapped(nm)))
        return out
