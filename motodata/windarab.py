"""windarab.py -- reader for Bosch WinDarab ``.bmsbin`` logger files.

A ``.bmsbin`` stores each channel as a bare array of samples with no channel
identity: the catalogue that names/scales them is encrypted in the file and only
WinDarab (with the ECU password) can read it. We decode the samples directly and
recover names/scaling from a per-file *channel map* built once from a WinDarab
text export of the same file (see :func:`build_map`). Stream ids are assigned per
file, so the map does not transfer between files -- but it is small and cached
beside the ``.bmsbin`` (``<file>.map.json``), so the export is a one-time step.

Sample encoding (verified byte-exact against WinDarab exports -- ``gear`` and
``pbrake_r_fer`` reproduce to machine precision):

  container -> scattered zlib blocks, each *followed* by a 24-byte descriptor
    ``{u32 chunk_count; u64 uncompressed_len; u64 self_offset; u32 stream_id}``;
    concatenating the blocks that share a ``stream_id`` in file order gives one
    channel's raw byte array.
  a channel array is fixed-width, offset-binary, and stored BYTE-PLANE split
  (all low bytes, then all high bytes -- this is why an interleaved ``int16``
  read fails). For a width of ``w`` bytes over ``n`` samples the raw integer is
  ``sum(plane_i * 256**i)``; the physical value is ``offset + gain * raw`` with
  ``gain``/``offset`` fitted per channel in :func:`build_map` (the bias term of
  the offset binary is absorbed into ``offset``).
"""
from __future__ import annotations

import collections
import json
import os
import struct
import zlib

import numpy as np

STD_RATES = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000)
_ZLIB_FLG = (0x01, 0x9C, 0xDA, 0x5E)


def _reassemble(data: bytes) -> dict[int, bytes]:
    """Return ``{stream_id: concatenated raw bytes}`` for every channel stream."""
    n = len(data)
    b = np.frombuffer(data, np.uint8)
    # A descriptor's chunk_count is small, so its top two bytes are zero -- a cheap
    # prefilter before the (rarer) full validation.
    cand = np.flatnonzero((b[2:n - 21] == 0) & (b[3:n - 20] == 0))
    groups: dict[int, list] = collections.defaultdict(list)
    for p in cand.tolist():
        count = int.from_bytes(data[p:p + 4], "little")
        if not 1 <= count <= 4096:
            continue
        declen, off, sid = struct.unpack_from("<QQI", data, p + 4)
        if not (0x120 <= off < p and 0 < declen < 20_000_000 and sid < 0x10000):
            continue
        if data[off] != 0x78 or data[off + 1] not in _ZLIB_FLG:
            continue
        parts, o = [], off
        for _ in range(count):
            if o >= p:
                parts = None
                break
            dobj = zlib.decompressobj()
            try:
                parts.append(dobj.decompress(data[o:p]))
            except zlib.error:
                parts = None
                break
            o += (p - o) - len(dobj.unused_data)
            while o < p and data[o] != 0x78:   # skip inter-chunk padding
                o += 1
        if parts is None:
            continue
        blob = b"".join(parts)
        if len(blob) == declen:
            groups[sid].append((off, blob))
    return {sid: b"".join(x[1] for x in sorted(v)) for sid, v in groups.items()}


def _planar(blob: bytes, bits: int) -> np.ndarray:
    """Decode a byte-plane-split stream to unsigned integers (bias not removed)."""
    width = bits // 8
    n = len(blob) // width
    b = np.frombuffer(blob[:n * width], np.uint8).astype(np.float64)
    val = b[:n].copy()
    for i in range(1, width):
        val += b[i * n:(i + 1) * n] * (256.0 ** i)
    return val


def load_map(bmsbin_path: str) -> dict:
    with open(map_path(bmsbin_path), encoding="utf-8") as f:
        return json.load(f)


def map_path(bmsbin_path: str) -> str:
    return os.path.splitext(bmsbin_path)[0] + ".map.json"


class DarabLap:
    """One ``.bmsbin`` outing, presented with the same surface as ``reader.Lap``."""

    def __init__(self, bmsbin_path: str, lap_time: float | None = None,
                 lap_distance: float | None = None):
        with open(bmsbin_path, "rb") as f:
            self._streams = _reassemble(f.read())
        channel_map = load_map(bmsbin_path)
        self._map: dict[str, dict] = channel_map["channels"]
        self.lap_time = lap_time if lap_time is not None else channel_map.get("lap_time")
        self.lap_distance = (lap_distance if lap_distance is not None
                             else channel_map.get("lap_distance"))
        self._values: dict[str, np.ndarray] = {}
        self._time_axes: dict[int, np.ndarray] = {}

    def close(self):
        self._streams.clear()
        self._values.clear()
        self._time_axes.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def channels(self) -> list[str]:
        return sorted(m for m, c in self._map.items() if c["stream"] in self._streams)

    def n_samples(self, name: str) -> int:
        c = self._map[name]
        return len(self._streams[c["stream"]]) // (c["bits"] // 8)

    def rate(self, name: str) -> float:
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
        v = self._values.get(name)
        if v is None:
            c = self._map[name]
            blob = self._streams[c["stream"]]
            v = c["gain"] * _planar(blob, c["bits"]) + c["offset"]
            v.setflags(write=False)
            self._values[name] = v
        return v

    def time_axis(self, name: str) -> np.ndarray:
        n = self.n_samples(name)
        t = self._time_axes.get(n)
        if t is not None:
            return t
        lt = self.lap_time
        if not lt or not np.isfinite(lt) or lt <= 0:
            raise ValueError("lap time is unavailable")
        t = np.arange(n, dtype=float) * (lt / n) if n else np.empty(0)
        t.setflags(write=False)
        self._time_axes[n] = t
        return t

    def channel(self, name: str):
        return self.time_axis(name), self.raw(name)

    def retain_time_axes(self, names):
        keep = {self.n_samples(name) for name in names if name in self._map}
        for count in list(self._time_axes):
            if count not in keep:
                del self._time_axes[count]

    def units(self) -> dict[str, str]:
        """Per-channel units from the map (for the catalog to label panels)."""
        return {n: c["unit"] for n, c in self._map.items() if c.get("unit")}


# --------------------------------------------------------------------------- #
# Building the per-file channel map from a WinDarab text export
# --------------------------------------------------------------------------- #

def _read_export(path: str):
    """WinDarab TSV -> (names, units, matrix[rows, cols]). Line 0 is ``#`` comment,
    the header is the first non-comment line, values follow."""
    names, units, rows = None, None, []
    with open(path, encoding="latin-1") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            if names is None:
                names, units = [], []
                for h in line.rstrip("\n").split("\t"):
                    n, _, u = h.partition("[")
                    names.append(n.strip())
                    units.append(u.rstrip("]").strip())
                continue
            rows.append(line.rstrip("\n"))
    M = np.full((len(rows), len(names)), np.nan)
    for i, r in enumerate(rows):
        for j, cell in enumerate(r.split("\t")[:len(names)]):
            try:
                M[i, j] = float(cell)
            except ValueError:
                pass
    return names, units, M


# channels used to anchor lap_time / lap_distance, best first
_TIME_COLS = ("xtime",)
_DIST_COLS = ("lap_dist_fer", "xdist", "gps_dist", "lapdist")


def build_map(bmsbin_path: str, export_path: str, *, min_sep: float = 3.0,
              max_resid: float = 0.05, save: bool = True) -> dict:
    """Match every decoded stream to its named export column and return a channel
    map. For each stream we fit gain/offset by least squares to its best-correlating
    columns and score by the MEDIAN residual relative to the channel's range.
    Identity is decided by SEPARATION -- how much better the best stream fits a
    column than the runner-up -- not by correlation, which confuses near-identical
    channels (two throttle sensors, xtime vs lap_time). A match is kept only if it is
    1:1, clears the runner-up by ``min_sep``, and fits to within ``max_resid`` of the
    channel's range (channels WinDarab resamples, e.g. nmot, fit loosely but well
    within this)."""
    names, units, M = _read_export(export_path)
    ng = M.shape[0]
    usable = [j for j in range(M.shape[1])
              if np.isfinite(M[:, j]).all() and M[:, j].std() > 1e-9]
    G = M[:, usable]
    # score residuals relative to each channel's own range: a per-unit (printed
    # precision) score is scale-dependent and lets a coarse-unit channel (ltdist in
    # km) steal a stream from a fine-unit one (lap_dist_fer in m).
    rng = np.ptp(G, axis=0)
    rng[rng <= 0] = 1.0

    cache: dict = {}
    def grid(step, n):
        key = (step, n)
        if key not in cache:
            idx = (np.arange(n) * step)
            idx = idx[idx < ng]
            sub = G[idx]
            sd = sub.std(0)
            ok = sd > 1e-9
            z = (sub[:, ok] - sub[:, ok].mean(0)) / sd[ok]
            cache[key] = (idx, sub, z, np.flatnonzero(ok))
        return cache[key]

    with open(bmsbin_path, "rb") as f:
        streams = _reassemble(f.read())
    rows = []
    for sid, blob in streams.items():
        for width in (1, 2, 4):
            if len(blob) % width:
                continue
            n = len(blob) // width
            if n < 300:
                continue
            ratio = ng / n
            step = round(ratio)
            if step < 1 or abs(ratio - step) > 0.01:
                continue
            idx, sub, z, okidx = grid(step, n)
            if len(idx) != n:
                continue
            x = _planar(blob, width * 8)
            if x.std() < 1e-9:
                continue
            corr = np.abs((x - x.mean()) / x.std() @ z) / n
            # consider ALL strongly-correlating columns, not just the top few: monotonic
            # ramps (xtime, xdist, lap_dist_fer, ...) all correlate ~1.0, so the true
            # column can rank well below the best correlator and only the residual fit
            # tells them apart.
            for t in np.argsort(-corr)[:20]:
                if corr[t] < 0.99:
                    break
                j = okidx[t]
                gd = sub[:, j]
                gain, off = np.linalg.lstsq(np.vstack([x, np.ones(n)]).T, gd, rcond=None)[0]
                med = float(np.median(np.abs(gain * x + off - gd)))
                rows.append((med / rng[j], sid, width * 8, usable[j],
                             float(gain), float(off)))
    rows.sort(key=lambda r: r[0])

    # best runner-up per column from a different stream -> separation margin
    second: dict[int, float] = {}
    seen_first: dict[int, int] = {}
    for score, sid, _bits, col, _g, _o in rows:
        if col not in seen_first:
            seen_first[col] = sid
        elif col not in second and sid != seen_first[col]:
            second[col] = score

    channels, used_sid, used_col = {}, set(), set()
    for score, sid, bits, col, gain, off in rows:
        if sid in used_sid or col in used_col:
            continue
        sep = second.get(col, float("inf")) / score if score > 0 else float("inf")
        if sep < min_sep or score > max_resid:
            continue
        used_sid.add(sid)
        used_col.add(col)
        channels[names[col]] = {"stream": sid, "bits": bits, "gain": gain,
                                "offset": off, "unit": units[col],
                                "err": round(score, 2),
                                "separation": None if sep == float("inf") else round(sep, 2)}

    # lap_time / lap_distance are read straight from the export columns (max of
    # xtime / a distance channel); they do not depend on the stream mapping.
    col_index = {n: j for j, n in enumerate(names)}
    def anchor(cols):
        for name in cols:
            j = col_index.get(name)
            if j is None:
                continue
            v = M[:, j]
            v = v[np.isfinite(v)]
            if len(v):
                return float(v.max())
        return None

    result = {
        "bmsbin": os.path.basename(bmsbin_path),
        "export": os.path.basename(export_path),
        "lap_time": anchor(_TIME_COLS),
        "lap_distance": anchor(_DIST_COLS),
        "channels": channels,
    }
    if save:
        with open(map_path(bmsbin_path), "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1)
    return result
