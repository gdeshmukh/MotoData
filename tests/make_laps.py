"""Write two synthetic laps under examples/Data (git-ignored) for the UI checks.

Run once:  .venv\\Scripts\\python tests\\make_laps.py

No real telemetry is involved: channels are generated here, so the tree is safe to
keep around and safe to screenshot. Sample counts must equal rate * lap_time for a
rate in reader.STD_RATES, or the app infers the wrong rate and every x-axis is off.
"""
import json, os, zipfile
import numpy as np

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")
LAPS_JSON = os.path.join(EXAMPLES, "laps.json")
RUN = os.path.join(EXAMPLES, "Data", "Testville", "Test_Session", "Car_5_Test", "Run_1")
LAPS = [("Lap_001_1", 95.0, 240.0), ("Lap_002_2", 96.5, 236.0)]


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
