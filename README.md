# MotoData

A lightweight desktop viewer and Python reader for **motorsport data-logger
session files**. It reads logged laps directly — no vendor software required —
and plots any of the ~700 logged channels with units, descriptions, a movable
cursor, and zoom.

> Working name — to be renamed later.

---

## Features
- **Direct file reading** — session archives are opened and decoded in pure Python.
- **Graph-first GUI** — the plot fills the window; lap and channel pickers are
  pop-up windows opened from the menu bar.
- **Lap browser** — navigate `Track ▸ Session ▸ Car ▸ Run ▸ Lap` with lap times.
- **Channel picker** — filter ~700 channels by name *or* description; channels you
  pick **persist when you switch laps**.
- **Units & descriptions** — inferred per channel (description keywords → naming
  convention), with a right-click override that's saved and reused.
- **Time / Distance x-axis** — toggle with `z`; distance uses the lap-distance metric.
- **Cursor & zoom** — left-drag moves a cursor (live per-channel values), right-drag
  zooms into a span, hover shows a value box, `h` resets.

## Requirements
- Python 3.10+
- `pip install matplotlib numpy`
- (Tkinter ships with Python.)
- Optional, only to regenerate channel descriptions: `pip install pdfplumber`

## Run
```bash
python viewer.py                 # opens the default data folder
python viewer.py "D:\path\to\Data"
# or set a default once:
#   MOTODATA_ROOT=...   (env var)
```

## Controls
**Mouse on the graph**

| Gesture | Result |
|---------|--------|
| Left-drag | move the cursor |
| Right-drag | highlight a span, release to zoom in |
| Hover | value box for that channel at the cursor x |

**Keyboard**

| Key | Action |
|-----|--------|
| `z` | toggle Time / Distance x-axis |
| `h` | reset zoom |
| `←` / `→` | nudge cursor |
| `Ctrl+L` | choose lap |
| `Ctrl+K` | choose channels |

(Also under `Help ▸ Shortcuts & mouse…`.)

## Project layout
```
MotoData/
├── viewer.py                 # the GUI app (entry point)
├── motodata/
│   ├── reader.py             # session-file parser (.ztx / .sar → numpy)
│   └── catalog.py            # unit inference + channel descriptions
└── examples/
    └── plot_fastlap.py       # headless example (no GUI)
```

### Channel descriptions (optional, not included)
Human-readable channel descriptions are loaded from
`motodata/channel_descriptions.json` (a `{ "ChannelName": "description" }` map) if
present. It is **not bundled** — supply your own to enable descriptions and
description-based unit inference. Without it, units still infer from channel-name
conventions and everything else works.

## Programmatic use
```python
from motodata import fastest_flying_lap, Lap

info = fastest_flying_lap(CAR_DIR)     # scans lap headers, picks fastest timed lap
lap  = Lap(info.ztx, info.lap_time)
lap.channels()                         # ~700 channel names
t, y = lap.channel("vCar")             # (time_seconds, values)
lap.rate_snapped("RPM")                # e.g. 50.0 Hz
```

## File format (reverse-engineered)
Each lap's `FlashData.ztx` is a plain ZIP archive containing one `<channel>.sar`
file per channel — a headerless little-endian `float64` array already in
engineering units. A channel's sample rate is `samples / lap_time`, which snaps to
a standard rate (1–500 Hz). Lap metadata lives in sibling XML files.

## Packaging (optional)
To build a standalone `.exe` (no Python needed on the target machine):
```bash
pip install pyinstaller
pyinstaller --onefile --windowed ^
  --add-data "motodata/channel_descriptions.json;motodata" viewer.py
```
The executable lands in `dist/`.
