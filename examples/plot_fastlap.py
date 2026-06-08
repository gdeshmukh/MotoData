"""Headless example: plot rPedal / RPM / vCar / nGear for a car's fastest lap.

Edit CAR_DIR to point at any <Track>/<Session>/<Car> folder, then run:
    python examples/plot_fastlap.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from motodata.reader import fastest_flying_lap, Lap

_ROOT = os.environ.get("MOTODATA_ROOT", "")    # set this to your session-data folder
CAR_DIR = os.path.join(_ROOT, "Daytona", "Q1", "GRGT421033-1036_120_STR247_3_WRL")
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "out", "fastlap.png")

info = fastest_flying_lap(CAR_DIR)
print(f"Fastest flying lap: {info.lap_time:.3f}s  Run {info.run} Lap {info.lap}")
print(f"  {info.path}")
lap = Lap(info.ztx, info.lap_time)

panels = [
    ("rPedal", "Throttle pedal [%]",  "tab:green",  (-5, 105)),
    ("RPM",    "Engine speed [rpm]",  "tab:red",    None),
    ("vCar",   "Speed [km/h]",        "tab:blue",   None),
    ("nGear",  "Gear",                "tab:purple", (0.5, 6.5)),
]

fig, axes = plt.subplots(len(panels), 1, sharex=True, figsize=(14, 9))
for ax, (ch, label, color, ylim) in zip(axes, panels):
    t, y = lap.channel(ch)
    hz = lap.rate_snapped(ch)
    if ch == "nGear":
        ax.step(t, y, where="post", color=color, lw=1.3)
        ax.set_yticks(range(1, 7))
    else:
        ax.plot(t, y, color=color, lw=0.9)
    if ylim:
        ax.set_ylim(*ylim)
    ax.set_ylabel(label)
    ax.grid(True, alpha=0.3)
    ax.text(0.995, 0.92, f"{hz:g} Hz", transform=ax.transAxes,
            ha="right", va="top", fontsize=8, color="gray")

axes[-1].set_xlabel("Lap time [s]")
axes[0].set_title(
    f"Daytona Q1 - Car 120 - fastest flying lap: {info.lap_time:.3f}s "
    f"(Run {info.run}, Lap {info.lap})", fontsize=12)
fig.tight_layout()
os.makedirs(os.path.dirname(OUT), exist_ok=True)
fig.savefig(OUT, dpi=120)
print("wrote", OUT)
