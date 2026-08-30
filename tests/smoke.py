"""The standing UI checks. Add one here for every UI behaviour you change.

    .venv\\Scripts\\python tests\\make_laps.py    (once)
    .venv\\Scripts\\python tests\\smoke.py
"""
import json
import sys
from PyQt6 import QtGui, QtWidgets
from ui import expected, launch
from make_laps import BMSBIN, BMSBIN_JSON
from motodata import app as A

DMAX = max(e["dist"] for e in expected().values())
LT = min(e["lap_time"] for e in expected().values())
qapp, win = launch()


def xlim(p):
    return p.getViewBox().state["limits"]["xLimits"]


lim = [xlim(p) for p in win._plots()]
win._plots()[0].getViewBox().scaleBy((25, 1))
qapp.processEvents()
lo, hi = win.xref.getViewBox().viewRange()[0]
win.delta_btn.click()
qapp.processEvents()

checks = {
    "both laps loaded": win.lapA and win.lapB,
    "lap time matches the generator": abs(win.lapA.lap_time - LT) < 1e-6,
    "distance matches the generator": abs(win.lapA.dist_max() - DMAX) < 5,
    "x limits pinned to the lap": all(l[0] == 0 and abs(l[1] - DMAX) < 5 for l in lim),
    "zoom-out clamped to the lap": lo >= -0.5 and hi <= DMAX + 0.5,
    "one track outline": (len(win.track.getData()[0]) > 100
                          and QtGui.QColor(A.MUTED) == win.track.opts["pen"].color()),
    "start/finish mark on the lap cut": (len(win.sf.getData()[0]) == 1
                                         and win.sf.getData()[0][0] == win.track.getData()[0][0]),
    "delta button hides the panel": win.dt is None,
    # not a name match: the third fallback is Consolas, which has no "mono" in it
    "font resolves to a mono family": QtGui.QFontInfo(A.mono(9)).fixedPitch(),
    "no session menu": not [m for m in win.menuBar().findChildren(QtWidgets.QMenu)
                            if m.title() == "&Session"],
}
win.delta_btn.click()
qapp.processEvents()
checks["delta button brings it back"] = win.dt is not None

# WinDarab .bmsbin reader path: open the synthetic file (its .map.json exists, so no
# export prompt) into slot A and confirm channels are named and decode by construction.
bm = json.load(open(BMSBIN_JSON, encoding="utf-8"))
win.open_bmsbin(BMSBIN, "A")
qapp.processEvents()
la = win.lapA
gear = la.ty("gear")[1]
checks["bmsbin loads with named Bosch channels"] = set(bm["channels"]) <= la.channels
checks["bmsbin lap time / distance from the map"] = (
    abs(la.lap_time - bm["lap_time"]) < 1e-6 and abs(la.dist_max() - bm["dist"]) < 5)
checks["bmsbin channels decode exactly"] = (
    len(gear) == bm["n_gear"] and abs(gear[0] - bm["gear0"]) < 1e-9
    and abs(la.ty("gps_speed")[1][0] - bm["speed0"]) < 1e-6)
win.close()

for label, ok in checks.items():
    print(("  ok  " if ok else "  FAIL") + "  " + label)
bad = [k for k, v in checks.items() if not v]
print(f"\n{len(bad)} failed" + (f": {bad}" if bad else ""))
sys.exit(1 if bad else 0)
