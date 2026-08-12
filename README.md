# MotoData

A desktop viewer for **motorsport data-logger session files**. It reads logged
laps directly — no vendor software required — and plots any of the ~700 logged
channels against a synced crosshair, a delta-time trace, and a GPS track map.
Built for post-session analysis: open a folder, scroll the laps, compare two.

> Working name — to be renamed later.

Reads WinTAX-family `.ztx` archives; it's car-agnostic, so a Toyota GR Supra GT4
and a Ferrari export open through the same reader.

---

## Features
- **Point at a folder** — a WinTAX `Data` root, a session, or a single car
  folder. Laps are discovered automatically; the last folder is remembered.
- **Two-lap compare** — lap **A** (amber) and lap **B** (cyan) everywhere. On
  open it auto-picks the fastest lap vs the next-fastest; step through laps with
  the arrow keys, `Shift`-click to set the other slot, or swap/clear.
- **Stacked synced traces** — one panel per channel, X-linked, with a crosshair
  that follows the mouse and live values per channel. Fast pan/zoom (pyqtgraph).
- **Delta-time** — when comparing on the distance axis, a Δt panel shows time
  gained/lost along the lap.
- **Track map** — the circuit drawn from GPS, with a cursor dot; hover the map to
  jump the graphs to that point on track (and vice-versa).
- **Channel picker** — filter ~700 channels by name *or* description, grouped by
  module; your selection persists as you switch laps.
- **Time / Distance x-axis**, **focus mode** (hide side panels), PNG export.

## Requirements
- Python 3.10+
- `pip install -r requirements.txt`  (numpy, PyQt6, pyqtgraph)

A virtual environment is recommended:
```bat
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## Run
```bat
.venv\Scripts\python viewer.py                       "open the last folder"
.venv\Scripts\python viewer.py "C:\path\to\Data"     "or point at a folder"
```
Or set a default once with the `MOTODATA_ROOT` environment variable.

## Controls
**Mouse on the graph** — hover moves the crosshair; left-drag pans; wheel zooms;
right-drag scales. **Hover the track map** to move the crosshair to that spot.

| Key | Action |
|-----|--------|
| `z` | toggle Time / Distance x-axis |
| `h` | reset zoom (auto-range) |
| `f` | focus mode — hide the side panels |
| `F11` | full screen |
| `Tab` | flip which slot (A/B) a lap click fills |
| `←` / `→` | step the active lap through the list |

Click a lap to load it into the active slot; `Shift`-click to load it into the
other slot.

## Project layout
```
MotoData/
├── viewer.py                 # launcher (python viewer.py [folder])
├── requirements.txt
└── motodata/
    ├── app.py                # the PyQt6 + pyqtgraph GUI
    ├── reader.py             # session-file parser (.ztx / .sar -> numpy)
    ├── lapdata.py            # per-lap analysis (time/distance x, GPS, delta-t)
    ├── discovery.py          # folder scan + lap-header cache
    └── catalog.py            # unit inference + channel descriptions
```

App state (last folder, selected channels, window sizes) and a lap-header cache
live in `~/.motodata/`.

### Channel descriptions (optional, not included)
Human-readable channel descriptions are loaded from
`motodata/channel_descriptions.json` (a `{ "ChannelName": "description" }` map) if
present. It is **not bundled** — supply your own to enable descriptions and
description-based unit inference. Without it, units still infer from channel-name
conventions and everything else works.

## Programmatic use
```python
from motodata import fastest_flying_lap, Lap, LapData

info = fastest_flying_lap(CAR_DIR)      # fastest lap the car actually drove
lap  = LapData(info.ztx, info.lap_time)
lap.xy("vCar", "dist")                  # (distance_m, speed) for plotting
lap.value_at("RPM", 1200.0, "dist")     # RPM at 1200 m into the lap
lap.gps_track()                         # (x, y) ground track in metres
```

## File format (reverse-engineered)
Each lap's `FlashData.ztx` is a plain ZIP archive containing one `<channel>.sar`
file per channel — a headerless little-endian `float64` array already in
engineering units. A channel's sample rate is `samples / lap_time`, which snaps to
a standard rate (1–500 Hz). Lap metadata lives in sibling XML files.

## Packaging (later)
A single-file `.exe` can be built with PyInstaller (`--onefile --windowed`) from
the virtual environment. For the most reliable bundle, build from a python.org
Python rather than the Microsoft Store build.
