"""tests/ui.py -- drive the real MotoData window against the synthetic laps.

Primitives for UI checks. The things that are easy to get wrong live here once
instead of in every script:

  - app state is redirected, so a run never touches the real ~/.motodata
  - laps load on a QThreadPool, so waiting is pump-until-true with a timeout
  - x-link target ranges read [0, 1] until a paint happens, so settle() forces one
  - grab() hands back a device-pixel pixmap and DwmGetWindowAttribute returns
    physical pixels, while Qt geometry is logical; on a scaled display a crop or a
    pixel sample that ignores that lands hundreds of pixels away

Windows only (the frame helpers call dwmapi).
"""
from __future__ import annotations
import ctypes, json, os, shutil, sys, time
from collections import Counter
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), HERE]

from PyQt6 import QtCore, QtGui, QtWidgets
from make_laps import EXAMPLES, LAPS_JSON
from motodata import app as A

DATA = os.path.join(EXAMPLES, "Data")
STATE = os.path.join(EXAMPLES, "state")
DWMWA_EXTENDED_FRAME_BOUNDS = 9


def expected():
    """Lap time and distance per lap as written by make_laps, so checks compare the
    app against the generator rather than against itself."""
    return json.load(open(LAPS_JSON, encoding="utf-8"))


def wait(qapp, cond, what, timeout=25.0):
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if cond():
            return
        time.sleep(0.02)
    raise TimeoutError(f"timed out waiting for {what}")


def settle(qapp, win, rounds=3):
    """Force layout and a paint. Panel x-ranges are not readable before one."""
    for _ in range(rounds):
        win.grab()
        qapp.processEvents()


def launch(root=DATA, flags=None):
    """A shown MotoData with laps loaded and painted. Returns (qapp, win)."""
    if not os.path.exists(LAPS_JSON):
        raise SystemExit("run tests/make_laps.py first")
    shutil.rmtree(STATE, ignore_errors=True)
    A.STATE_DIR = STATE                             # never the real ~/.motodata
    A.CONFIG = os.path.join(STATE, "config.json")
    A.HEADER_CACHE = os.path.join(STATE, "headers.json")
    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = A.MotoData(root)
    if flags is not None:
        win.setWindowFlags(flags)
    win.show()
    wait(qapp, lambda: win.lapA and win.lapB, "both lap slots to fill")
    settle(qapp, win)
    return qapp, win


def place(qapp, win, size=(1100, 620), at=(60, 60)):
    """Un-maximise and put the whole window on screen. Edge samples and crops mean
    nothing for a window that runs off the display."""
    g = win.screen().availableGeometry()
    win.showNormal()
    win.resize(*size)
    win.move(g.x() + at[0], g.y() + at[1])
    settle(qapp, win)


def shot(win, widget=None, path=None, scale=1):
    """Screenshot the window, or crop one child widget out of it. grab() is a
    device-pixel pixmap, so a child's logical geometry has to be scaled first."""
    pm = win.grab()
    if widget is not None:
        r = win.devicePixelRatioF()
        o = widget.mapTo(win, QtCore.QPoint(0, 0))
        pm = pm.copy(round(o.x() * r), round(o.y() * r),
                     round(widget.width() * r), round(widget.height() * r))
    if scale != 1:
        pm = pm.scaled(pm.width() * scale, pm.height() * scale,
                       QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                       QtCore.Qt.TransformationMode.FastTransformation)
    if path:
        pm.save(str(path))
    return pm


def frame_rect(win):
    """Visible frame bounds in PHYSICAL pixels -- not win.geometry(), which is logical."""
    r = wintypes.RECT()
    ctypes.windll.dwmapi.DwmGetWindowAttribute(
        ctypes.c_void_p(int(win.winId())), DWMWA_EXTENDED_FRAME_BOUNDS,
        ctypes.byref(r), ctypes.sizeof(r))
    return r


def edge_colours(win, inset=40, samples=40):
    """Most common colour along each visible window edge. A light value means Windows
    is drawing a frame border. inset skips the rounded corners."""
    scr = win.screen()
    img, g = scr.grabWindow(0).toImage(), scr.geometry()
    dpr = img.width() / g.width()
    ox, oy = round(g.x() * dpr), round(g.y() * dpr)   # screen origin, physical pixels
    r = frame_rect(win)
    step_x = max(1, (r.right - r.left - 2 * inset) // samples)
    step_y = max(1, (r.bottom - r.top - 2 * inset) // samples)
    xs = range(r.left + inset, r.right - inset, step_x)
    ys = range(r.top + inset, r.bottom - inset, step_y)
    edges = {"top": [(x, r.top) for x in xs], "bottom": [(x, r.bottom - 1) for x in xs],
             "left": [(r.left, y) for y in ys], "right": [(r.right - 1, y) for y in ys]}
    seen = {}
    for name, pts in edges.items():
        on = [(x - ox, y - oy) for x, y in pts
              if 0 <= x - ox < img.width() and 0 <= y - oy < img.height()]
        seen[name] = (Counter(QtGui.QColor(img.pixel(x, y)).name()
                             for x, y in on).most_common(1)[0][0] if on else None)
    return seen                                       # None = that edge is off screen
