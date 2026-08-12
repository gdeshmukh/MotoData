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
  Scanning the whole 4,700-lap root takes well under a second.
- **Lap chooser** (`Ctrl+L`) — a Track / Session / Car / Run tree on the left,
  that node's laps as rows on the right with lap time and gap to best, each row
  carrying an **A** and a **B** checkbox so either slot is one click.
- **Two-lap compare** — lap **A** (red) and lap **B** (green) on every trace, in
  the cursor readout and as a dot on the track map. On open it auto-picks the
  fastest lap vs the next-fastest.
- **Session metadata** — driver, car number, track, session and date are read
  from the XML WinTAX writes beside each run and shown in the header.
- **Stacked synced traces** — one panel per channel, X-linked. Left-click or drag
  moves the cursor; right-drag and the wheel zoom, and zooming out stops at the
  full lap. Adding a channel keeps the zoom you were on.
- **Delta-time** — a Δt panel shows time gained/lost along the lap. It only means
  anything against distance, so the Δ button flips the x-axis to distance when you
  turn it on, and greys out when there is nothing to compare.
- **Track map** — the circuit drawn from GPS as one outline, a checkered mark where
  the lap is cut and a dot per lap at the cursor; click the map to jump the graphs
  to that point on track.
- **Channel picker** (`Ctrl+K`) — filter ~700 channels by name *or* description,
  grouped by module; your selection persists as you switch laps.
- **Time / Distance x-axis**, **focus mode** (hide the side panel), PNG export.

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
**Mouse on the graph** — left-click or drag moves the cursor; right-drag and the
wheel zoom. **Click the track map** to move the cursor to that point on track.

The window has no title bar: minimise / maximise / close sit at the right-hand end
of the menu bar, and dragging the empty part of the menu bar moves the window
(double-click it to maximise).

| Key | Action |
|-----|--------|
| `Ctrl+O` | open a data folder |
| `Ctrl+L` | choose laps |
| `Ctrl+K` | select channels |
| `Ctrl+S` | swap A / B |
| `z` | toggle Time / Distance x-axis |
| `h` | reset zoom (auto-range) |
| `f` | focus mode — hide the side panel |
| `F11` | full screen |

## Project layout
```
MotoData/
├── viewer.py                 # launcher (python viewer.py [folder])
├── requirements.txt
└── motodata/
    ├── app.py                # main window: graph stack, track map, menus
    ├── pickers.py            # lap chooser (tree + A/B rows) and channel chooser
    ├── reader.py             # session-file parser (.ztx / .sar -> numpy)
    ├── lapdata.py            # per-lap analysis (time/distance x, GPS, delta-t)
    ├── discovery.py          # folder scan, session metadata, lap-header cache
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
