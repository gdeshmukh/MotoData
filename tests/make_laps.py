"""Write two synthetic laps under examples/Data (git-ignored) for the UI checks.

Run once:  .venv\\Scripts\\python tests\\make_laps.py

No real telemetry is involved: channels are generated here, so the tree is safe to
keep around and safe to screenshot. Sample counts must equal rate * lap_time for a
rate in reader.STD_RATES, or the app infers the wrong rate and every x-axis is off.
"""
import json, os, struct, zipfile, zlib
import numpy as np

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")
LAPS_JSON = os.path.join(EXAMPLES, "laps.json")
RUN = os.path.join(EXAMPLES, "Data", "Testville", "Test_Session", "Car_5_Test", "Run_1")
LAPS = [("Lap_001_1", 95.0, 240.0), ("Lap_002_2", 96.5, 236.0)]

BMSBIN = os.path.join(EXAMPLES, "SynthDDU.bmsbin")   # WinDarab reader fixture (+ .map.json)
BMSBIN_JSON = os.path.join(EXAMPLES, "bmsbin.json")


def write_lap(lap_dir, lap_time, vmax):
    os.makedirs(lap_dir, exist_ok=True)
    t = np.arange(int(round(20 * lap_time))) / 20.0                   # 20 Hz base
    frac = t / lap_time
    v = vmax * (0.45 + 0.55 * np.abs(np.sin(2 * np.pi * 2.5 * frac)) ** 0.6)
    d = np.concatenate([[0.0], np.cumsum(np.diff(t) * v[:-1] / 3.6)])
    ang = 2 * np.pi * d / d[-1]
    t10 = np.arange(int(round(10 * lap_time))) / 10.0                 # 10 Hz GPS
    chans = {
        "vCar": v,
        "sLap": d,
        "rPedal": 100 * (v / vmax) ** 2,
        "pBrakeMCF": np.clip(-np.gradient(v, t) / 8, 0, None),
        "EPS_aSteering": 60 * np.sin(2 * np.pi * 5 * frac),
        "nGear": np.clip(np.round(v / 40), 1, 6) + 4,                 # raw = gear + GEAR_OFFSET
        "GPS_Latitude": np.interp(t10, t, 45.0 + 0.0045 * np.sin(ang)),
        "GPS_Longitude": np.interp(t10, t, 9.0 + 0.0060 * np.cos(ang)),
    }
    with zipfile.ZipFile(os.path.join(lap_dir, "FlashData.ztx"), "w") as z:
        for name, arr in chans.items():
            z.writestr(name + ".sar", np.asarray(arr, "<f8").tobytes())
    with open(os.path.join(lap_dir, "LapHeader.xml"), "w") as f:
        f.write(f"<Lap><LapTime>{lap_time:.3f}</LapTime><Marker></Marker><Run>1</Run>"
                f"<Lap>1</Lap><LapDistance>{d[-1]:.0f}</LapDistance><STS>1700000000</STS></Lap>")
    return float(d[-1])


def _plane_split(vals, bits, gain, offset):
    """Encode physical values the way a .bmsbin stores them: offset-binary integers
    written as separate byte planes (all low bytes, then all high bytes)."""
    raw = np.round((np.asarray(vals, float) - offset) / gain).astype(np.int64)
    width = bits // 8
    return b"".join(bytes(((raw >> (8 * i)) & 0xFF).astype(np.uint8)) for i in range(width))


def write_bmsbin(path=BMSBIN, lap_time=100.0):
    """Write a synthetic .bmsbin in the real container layout (zlib blocks + 24-byte
    descriptors) plus its .map.json, so the WinDarab reader path has a fixture with no
    real telemetry. Channels are generated here and the map is exact by construction."""
    t50 = np.arange(int(50 * lap_time)) / 50.0
    f = t50 / lap_time
    speed = 100 + 90 * (1 + np.sin(2 * np.pi * 3 * f))
    f100 = np.arange(int(100 * lap_time)) / 100.0 / lap_time
    s100 = np.interp(f100, f, speed)
    d = np.concatenate([[0.0], np.cumsum(np.diff(t50) * speed[:-1] / 3.6)])
    dist10 = np.interp(np.arange(int(10 * lap_time)) / 10.0 / lap_time, f, d)
    chans = {                       # name: (values, bits, gain, offset, unit)
        "gps_speed":    (speed, 16, 0.01, 0.0, "km/h"),
        "nmot":         (3000 + 40 * s100, 16, 0.5, 0.0, "rpm"),
        "aps_fer":      (100 * np.clip(np.sin(2*np.pi*3*f100)*0.5 + 0.5, 0, 1), 16, 0.01, 0.0, "%"),
        "pbrake_f_fer": (8 * np.clip(-np.sin(2*np.pi*3*f100), 0, 1), 16, 0.01, 0.0, "bar"),
        "gear":         (np.clip(np.round(s100 / 45), 1, 6), 8, 1.0, -128.0, ""),
        "lap_dist_fer": (dist10, 16, 0.5, 0.0, "m"),
    }
    out = bytearray(0x120)                          # header; streams must start >= 0x120
    out[0:16] = b"Darab v7.10.078\x00"
    cmap, sid = {}, 0x0b
    for name, (vals, bits, gain, offset, unit) in chans.items():
        blob = _plane_split(vals, bits, gain, offset)
        start = len(out)
        out += zlib.compress(blob)
        out += struct.pack("<IQQI", 1, len(blob), start, sid)   # {count, declen, self, id}
        cmap[name] = {"stream": sid, "bits": bits, "gain": gain, "offset": offset,
                      "unit": unit}
        sid += 1
    struct.pack_into("<I", out, 0x20, len(out))     # file size (informational)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(out)
    with open(os.path.splitext(path)[0] + ".map.json", "w") as fh:
        json.dump({"lap_time": lap_time, "lap_distance": float(d[-1]), "channels": cmap}, fh)
    exp = {"lap_time": lap_time, "dist": float(d[-1]), "channels": list(cmap),
           "n_gear": len(chans["gear"][0]), "gear0": float(chans["gear"][0][0]),
           "speed0": float(speed[0])}
    with open(BMSBIN_JSON, "w") as fh:
        json.dump(exp, fh, indent=1)
    return exp


if __name__ == "__main__":
    os.makedirs(RUN, exist_ok=True)
    open(os.path.join(RUN, "Car.xml"), "w").write("<Car><Name>Car_5_Test</Name></Car>")
    open(os.path.join(RUN, "Session.xml"), "w").write(
        "<S><Name>Test</Name><Track>Testville</Track></S>")
    expect = {n: {"lap_time": lt, "dist": write_lap(os.path.join(RUN, n), lt, vmax)}
              for n, lt, vmax in LAPS}
    with open(LAPS_JSON, "w") as f:
        json.dump(expect, f, indent=1)
    for n, e in expect.items():
        print(f"{n}: {e['lap_time']:.3f} s, {e['dist']:.1f} m")
    b = write_bmsbin()
    print(f"SynthDDU.bmsbin: {b['lap_time']:.1f} s, {b['dist']:.1f} m, {len(b['channels'])} channels")
