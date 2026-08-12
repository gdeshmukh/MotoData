"""app.py -- MotoData PyQt6 + pyqtgraph telemetry viewer.

Channels pick on the left, the graph stack fills the middle, the track map and
cursor readout sit on the right. Laps are chosen in their own window (Ctrl+L).
Data comes through motodata.reader / .lapdata / .discovery.
"""
from __future__ import annotations
import os, sys, ctypes, json, time
import numpy as np

from PyQt6 import QtGui, QtWidgets
from PyQt6.QtCore import Qt, QEvent, QObject, QRunnable, QThreadPool, QTimer, pyqtSignal
import pyqtgraph as pg

from .catalog import Catalog
from .lapdata import LapData
from .pickers import LapPicker, ChannelPicker, Scan, fmt_time
from . import discovery

A_COLOR = "#ff3b30"     # lap A (red)
B_COLOR = "#34d158"     # lap B (green)
BG, PANEL, GRID = "#101317", "#161a1f", "#262c34"
INK, MUTED, CROSS = "#c8cdd4", "#7d8894", "#e8ecf1"
EDGE = "#39424e"        # panel divider

# used when only one lap is shown, so channels stay tellable apart
CHAN_COLORS = ["#37d3d0", "#f2b03d", "#a98bff", "#4d9dff", "#ff77b0", "#9ada5a",
               "#ff8a3c", "#7ee6a0", "#e0e0e0", "#ffd166", "#8ec7ff", "#c3a6ff"]

ROLE_CHANNELS = {
    "speed":    ["vCar", "CarSpd_vCar", "GPS_CarSpeed"],
    "throttle": ["rPedal", "rThrottle1"],
    "brake":    ["pBrakeMCF", "pBrakeMCR"],
    "gear":     ["nGear", "GBX_NGearLeverOut"],
    "steer":    ["EPS_aSteering", "aSteer"],
}
MAX_PANELS = 12
FRAME_MS = 0.016
CAPTION_CLICKS = (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)
FONT = ("JetBrains Mono", "Cascadia Mono", "Consolas")   # first one installed wins
FONT_CSS = ",".join(f"'{f}'" for f in FONT)
DWMWA_BORDER_COLOR = 34
STATE_DIR = os.path.join(os.path.expanduser("~"), ".motodata")
CONFIG = os.path.join(STATE_DIR, "config.json")
HEADER_CACHE = os.path.join(STATE_DIR, "headers.json")

STYLE = f"""
QWidget {{ background:{PANEL}; color:{INK}; font-family:{FONT_CSS};
           font-size:12px; font-weight:300; }}
QMainWindow, QSplitter {{ background:{BG}; }}
QSplitter::handle {{ background:{EDGE}; }}
QMenuBar {{ background:{BG}; }}
QMenuBar::item:selected {{ background:#2b3037; }}
#wbtn, #wclose {{ background:transparent; border:0; border-radius:0; color:{MUTED}; }}
#wbtn:hover {{ background:#2b3037; color:{INK}; }}
#wclose:hover {{ background:#c0392b; color:#ffffff; }}
QMenu {{ background:{PANEL}; border:1px solid {GRID}; }}
QMenu::item:selected {{ background:#2b3037; }}
QPushButton {{ background:#22262c; border:1px solid #30353c; padding:3px 9px; border-radius:3px; }}
QPushButton:hover {{ background:#2b3037; }}
QPushButton:checked {{ background:#2f3944; border-color:#4a5563; }}
QLineEdit {{ background:#0e1114; border:1px solid #2a2f36; padding:4px; border-radius:3px; }}
QTreeWidget {{ background:#0e1114; border:1px solid #23282f; outline:0; }}
QHeaderView::section {{ background:#1b2026; border:0; border-right:1px solid {GRID}; padding:4px; }}
#compbar {{ background:{BG}; border-bottom:1px solid {EDGE}; }}
QStatusBar {{ background:{BG}; color:{MUTED}; }}
QScrollBar:vertical {{ background:{PANEL}; width:11px; margin:0; }}
QScrollBar::handle:vertical {{ background:#2b323a; border-radius:5px; min-height:24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height:0; }}
"""

pg.setConfigOptions(antialias=False, background=BG, foreground=MUTED)


def dark_frame(hwnd):
    """Pin the DWM window border to the background. Around a captionless window
    Windows draws it light grey, brightest just after the window is raised again."""
    c = QtGui.QColor(BG)
    ref = ctypes.c_int(c.blue() << 16 | c.green() << 8 | c.red())    # COLORREF is 0x00BBGGRR
    try:
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_BORDER_COLOR, ctypes.byref(ref), ctypes.sizeof(ref))
    except (AttributeError, OSError):
        pass                                    # not Windows, or no dwmapi to talk to


def sf_symbol(cols=3):
    """A 2 x cols checkerboard in a unit box: the start/finish mark."""
    p = QtGui.QPainterPath()
    for row in range(2):
        for col in range(row % 2, cols, 2):
            p.addRect(col / cols - 0.5, row / 2 - 0.5, 1 / cols, 0.5)
    return p


def mono(pt, weight=QtGui.QFont.Weight.Light):
    f = QtGui.QFont()
    f.setFamilies(FONT)
    f.setPointSize(pt)
    f.setWeight(weight)
    return f


def fmt_val(name, v):
    if v is None or v != v:
        return "--"
    return str(int(round(v))) if "gear" in name.lower() else f"{v:.1f}"


def cache_curve(item):
    """Rasterise a curve once so moving the cursor blits instead of redrawing it."""
    c = getattr(item, "curve", item)
    c.setCacheMode(QtWidgets.QGraphicsItem.CacheMode.DeviceCoordinateCache)


def add_grid(plot):
    """Cached grid item. The axis-drawn grid (showGrid) is regenerated on every
    repaint, which costs ~20 ms per cursor move across a stack of panels."""
    g = pg.GridItem()
    g.setTextPen(None)
    g.setZValue(-1000)
    g.setCacheMode(QtWidgets.QGraphicsItem.CacheMode.DeviceCoordinateCache)
    plot.addItem(g, ignoreBounds=True)
    return g


class CursorViewBox(pg.ViewBox):
    """Left click/drag moves the cursor; right-drag and wheel still zoom."""

    def __init__(self, on_cursor):
        super().__init__()
        self._on_cursor = on_cursor

    def _emit(self, ev):
        ev.accept()
        self._on_cursor(self.mapSceneToView(ev.scenePos()).x())

    def mouseDragEvent(self, ev, axis=None):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._emit(ev)
        else:
            super().mouseDragEvent(ev, axis)

    def mouseClickEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._emit(ev)
        else:
            super().mouseClickEvent(ev)


class MapViewBox(pg.ViewBox):
    """Click/drag on the track map jumps the cursor to that point on track."""

    def __init__(self, on_point):
        super().__init__()
        self._on_point = on_point
        self.setMouseEnabled(False, False)
        self.setAspectLocked(True)

    def _emit(self, ev):
        ev.accept()
        p = self.mapSceneToView(ev.scenePos())
        self._on_point(p.x(), p.y())

    def mouseDragEvent(self, ev, axis=None):
        self._emit(ev)

    def mouseClickEvent(self, ev):
        self._emit(ev)


class _WalkSig(QObject):
    result = pyqtSignal(int, object)


class Walk(QRunnable):
    def __init__(self, root, gen):
        super().__init__()
        self.root, self.gen, self.sig = root, gen, _WalkSig()
        self.finished = False
        self.setAutoDelete(False)          # Python side must outlive run()

    def run(self):
        try:
            cars = discovery.group_by_car(discovery.find_lap_dirs(self.root))
        except Exception:
            cars = {}
        self.sig.result.emit(self.gen, cars)
        self.finished = True


class MotoData(QtWidgets.QMainWindow):
    def __init__(self, root=""):
        super().__init__()
        self.setWindowTitle("MotoData")                 # taskbar only; there is no title bar
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.CustomizeWindowHint
                            | Qt.WindowType.WindowSystemMenuHint)
        self.resize(1600, 950)
        self.setStyleSheet(STYLE)
        self.cat = Catalog()
        self.pool = QThreadPool.globalInstance()
        self.hdr_cache = discovery.load_cache(HEADER_CACHE)
        self.meta_cache = {}
        self.cfg = self._load_cfg()

        self.root = ""
        self.cars = {}
        self.lap_rows = []
        self.scan = None
        self.lapA = self.lapB = None
        self.showA = self.showB = True
        self.show_dt = True
        self.mode = "dist"
        self._m = "dist"
        self.plotted = list(self.cfg.get("plotted") or [])
        self.cursor_x = 0.0
        self.panels = {}
        self.dt = None
        self.xref = None
        self._gen = self._walk_gen = 0
        self._jobs = []
        self._auto_done = False
        self._focus = False
        self._xset = False
        self._read_rows = {}
        self._pending_x = None
        self._last_move = 0.0
        self._tick = QTimer(self)
        self._tick.setSingleShot(True)
        self._tick.timeout.connect(self._flush_cursor)

        self.laps_win = LapPicker(self.pool, self.hdr_cache, STYLE)
        self.laps_win.assign.connect(self._assign)
        self.laps_win.unassign.connect(self._unassign)
        self.chan_panel = ChannelPicker(self.cat)
        self.chan_panel.changed.connect(self._set_channels)

        self._build_ui()
        self._build_menu()

        start = root or self.cfg.get("last_folder") or os.environ.get("MOTODATA_ROOT", "")
        if start and os.path.isdir(start):
            self.open_folder(start)

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        self.split = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        self.split.setHandleWidth(2)
        self.setCentralWidget(self.split)
        self.chan_panel.setMinimumWidth(200)
        self.split.addWidget(self.chan_panel)
        self.split.addWidget(self._build_center())
        right = self._build_right()
        right.setMinimumWidth(250)
        self.split.addWidget(right)
        self.split.setStretchFactor(1, 1)
        sizes = self.cfg.get("splitter_open") or [250, 1050, 300]
        if len(sizes) != 3 or sizes[1] == 0:
            sizes = [250, 1050, 300]
        self.split.setSizes(sizes)
        self.status = QtWidgets.QLabel("Open a folder to begin.")
        self.statusBar().addWidget(self.status)

    def _slot_row(self, slot, color, toggle):
        btn = QtWidgets.QPushButton(f"{slot}  --")
        btn.setCheckable(True)
        btn.setChecked(True)
        btn.setFont(mono(10))
        btn.setStyleSheet(f"QPushButton{{color:{color}; text-align:left;}}")
        btn.setToolTip(f"click to show / hide lap {slot}")
        btn.setMinimumWidth(112)
        btn.clicked.connect(toggle)
        meta = QtWidgets.QLabel("")
        meta.setStyleSheet(f"color:{MUTED};")
        return btn, meta

    def _build_center(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        bar = QtWidgets.QWidget()
        bar.setObjectName("compbar")
        h = QtWidgets.QHBoxLayout(bar)
        h.setContentsMargins(8, 5, 8, 5)
        h.setSpacing(10)
        btns = QtWidgets.QVBoxLayout()
        btns.setSpacing(3)
        b = QtWidgets.QPushButton("Laps…")
        b.clicked.connect(self.show_laps)
        btns.addWidget(b)
        self.delta_btn = QtWidgets.QPushButton("Δ  --")
        self.delta_btn.setCheckable(True)
        self.delta_btn.setChecked(True)
        self.delta_btn.setFont(mono(9))
        self.delta_btn.setToolTip("show / hide the Δt panel (flips the x-axis to distance)")
        self.delta_btn.clicked.connect(self._toggle_dt)
        btns.addWidget(self.delta_btn)
        h.addLayout(btns)

        slots = QtWidgets.QGridLayout()
        slots.setHorizontalSpacing(10)
        slots.setVerticalSpacing(3)
        self.btnA, self.metaA = self._slot_row("A", A_COLOR, lambda: self._toggle_lap("A"))
        self.btnB, self.metaB = self._slot_row("B", B_COLOR, lambda: self._toggle_lap("B"))
        slots.addWidget(self.btnA, 0, 0)
        slots.addWidget(self.metaA, 0, 1)
        slots.addWidget(self.btnB, 1, 0)
        slots.addWidget(self.metaB, 1, 1)
        slots.setColumnStretch(1, 1)
        h.addLayout(slots, 1)

        right = QtWidgets.QVBoxLayout()
        right.setSpacing(3)
        top = QtWidgets.QHBoxLayout()
        self.mode_btn = QtWidgets.QPushButton("x: Distance")
        self.mode_btn.clicked.connect(self.toggle_mode)
        top.addWidget(self.mode_btn)
        b = QtWidgets.QPushButton("reset")
        b.clicked.connect(self.autorange)
        top.addWidget(b)
        right.addLayout(top)
        bot = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("focus")
        b.clicked.connect(self.toggle_focus)
        bot.addWidget(b)
        b = QtWidgets.QPushButton("PNG")
        b.clicked.connect(self.save_png)
        bot.addWidget(b)
        right.addLayout(bot)
        h.addLayout(right)
        v.addWidget(bar)

        self.glw = pg.GraphicsLayoutWidget()
        self.glw.ci.layout.setSpacing(10)
        self.glw.ci.layout.setContentsMargins(2, 2, 2, 2)
        v.addWidget(self.glw, 1)
        return w

    def _build_right(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(6)
        v.addWidget(self._h("TRACK MAP"))
        self.map = pg.PlotWidget(viewBox=MapViewBox(self._map_cursor_to))
        self.map.hideAxis("left")
        self.map.hideAxis("bottom")
        self.map.setMenuEnabled(False)
        self.map.setMinimumHeight(240)
        self.track = self.map.plot(pen=pg.mkPen(MUTED, width=2))
        cache_curve(self.track)
        self.sf = pg.ScatterPlotItem(size=13, symbol=sf_symbol(), brush=INK, pen=None)
        self.map.addItem(self.sf)
        self.dotB = pg.ScatterPlotItem(size=8, brush=B_COLOR, pen=None)
        self.dotA = pg.ScatterPlotItem(size=11, brush=A_COLOR, pen=pg.mkPen(BG))
        self.map.addItem(self.dotB)
        self.map.addItem(self.dotA)
        v.addWidget(self.map, 3)
        v.addWidget(self._h("AT CURSOR"))
        self.cursor_lbl = QtWidgets.QLabel("cursor  --")
        self.cursor_lbl.setFont(mono(9))
        self.cursor_lbl.setStyleSheet(f"color:{MUTED};")
        v.addWidget(self.cursor_lbl)
        self.readout = QtWidgets.QWidget()
        self.read_grid = QtWidgets.QGridLayout(self.readout)
        self.read_grid.setContentsMargins(0, 0, 0, 0)
        self.read_grid.setVerticalSpacing(2)
        self.read_grid.setHorizontalSpacing(10)
        v.addWidget(self.readout)
        v.addStretch(1)
        return w

    def _h(self, text):
        lab = QtWidgets.QLabel(text)
        lab.setStyleSheet(f"color:{MUTED}; letter-spacing:1px; font-size:10px;")
        return lab

    def _win_buttons(self):
        """Minimise / maximise / close, at the right-hand end of the menu bar."""
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        for glyph, name, fn in (("—", "wbtn", self.showMinimized),
                                ("□", "wbtn", self._toggle_max),
                                ("✕", "wclose", self.close)):
            b = QtWidgets.QPushButton(glyph)
            b.setObjectName(name)
            b.setFixedSize(38, 22)
            b.clicked.connect(fn)
            h.addWidget(b)
        return w

    def eventFilter(self, obj, ev):
        """Empty menu bar stands in for the title bar: drag moves the window,
        double-click maximises it."""
        if (ev.type() in CAPTION_CLICKS and obj is self.menuBar()
                and ev.button() == Qt.MouseButton.LeftButton
                and not obj.actionAt(ev.position().toPoint())):
            if ev.type() == QEvent.Type.MouseButtonDblClick:
                self._toggle_max()
            else:
                self.windowHandle().startSystemMove()
            return True
        return super().eventFilter(obj, ev)

    def _build_menu(self):
        mb = self.menuBar()
        mb.setCornerWidget(self._win_buttons(), Qt.Corner.TopRightCorner)
        mb.installEventFilter(self)
        m = mb.addMenu("&File")
        m.addAction("Open folder…", "Ctrl+O", self.open_folder_dialog)
        m.addAction("Save graph as PNG…", self.save_png)
        m.addSeparator()
        m.addAction("Exit", self.close)

        m = mb.addMenu("&Laps")
        m.addAction("Choose laps…", "Ctrl+L", self.show_laps)
        m.addSeparator()
        m.addAction("Show / hide lap A", "Ctrl+1", lambda: self._toggle_lap("A", flip=True))
        m.addAction("Show / hide lap B", "Ctrl+2", lambda: self._toggle_lap("B", flip=True))
        m.addAction("Swap A / B", "Ctrl+S", self._swap)
        m.addAction("Clear B", self._clear_b)

        m = mb.addMenu("&Channels")
        m.addAction("Focus channel filter", "Ctrl+K", self.focus_channels)
        m.addAction("Clear all", lambda: self._set_channels([]))

        m = mb.addMenu("&View")
        m.addAction("Time / Distance x-axis", "Z", self.toggle_mode)
        m.addAction("Reset zoom", "H", self.autorange)
        m.addAction("Focus mode (hide side panels)", "F", self.toggle_focus)
        m.addAction("Full screen", "F11", self._toggle_fullscreen)

    # ------------------------------------------------------------- config
    def _load_cfg(self):
        try:
            return json.load(open(CONFIG, encoding="utf-8"))
        except Exception:
            return {}

    def _save_cfg(self):
        self.cfg["plotted"] = self.plotted
        if not self._focus:
            self.cfg["splitter_open"] = self.split.sizes()
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            json.dump(self.cfg, open(CONFIG, "w", encoding="utf-8"))
        except Exception:
            pass

    # ------------------------------------------------------------ folders
    def show_laps(self):
        self.laps_win.set_root(self.root)
        self.laps_win.show()
        self.laps_win.raise_()
        self.laps_win.activateWindow()

    def focus_channels(self):
        if self.split.sizes()[0] == 0:
            self.toggle_focus()
        self.chan_panel.filter.setFocus()
        self.chan_panel.filter.selectAll()

    def _start(self, job):
        self._jobs = [j for j in self._jobs if not j.finished]
        self._jobs.append(job)
        self.pool.start(job)

    def open_folder_dialog(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select a WinTAX data / session / car folder", self.cfg.get("last_folder", ""))
        if d:
            self.open_folder(d)

    def open_folder(self, root):
        self.root = root
        self.cfg["last_folder"] = root
        self.status.setText("Scanning folders…")
        self._walk_gen += 1
        w = Walk(root, self._walk_gen)
        w.sig.result.connect(self._on_cars)
        self._start(w)

    def _on_cars(self, gen, cars):
        if gen != self._walk_gen:
            return
        self.cars = cars
        self.laps_win.set_root(self.root)
        if cars:
            self.select_car(next(iter(cars)))
        else:
            self.status.setText("No laps found under that folder.")

    def select_car(self, car):
        if self.scan:
            self.scan.stop()
            try:
                self.scan.sig.lap.disconnect()
                self.scan.sig.done.disconnect()
            except TypeError:
                pass
        self._gen += 1
        self._auto_done = False
        laps = self.cars.get(car, [])
        self.lap_rows = [{"dir": d, "lt": None} for d in laps]
        self.status.setText(f"{len(laps)} laps — reading times…")
        self.laps_win.show_laps(laps)
        self.scan = Scan(laps, self.hdr_cache, self._gen)
        self.scan.sig.lap.connect(self._on_lap_meta)
        self.scan.sig.done.connect(self._on_scan_done)
        self._start(self.scan)

    def _on_lap_meta(self, gen, i, meta):
        if gen == self._gen and i < len(self.lap_rows):
            self.lap_rows[i]["lt"] = meta[0]

    def _on_scan_done(self, gen):
        if gen != self._gen or self._auto_done:
            return
        self._auto_done = True
        ts = [r["lt"] for r in self.lap_rows if r["lt"] and r["lt"] > 0]
        med = float(np.median(ts)) if ts else None
        cand = sorted((r["lt"], r["dir"]) for r in self.lap_rows
                      if r["lt"] and r["lt"] > 0 and (med is None or r["lt"] >= 0.5 * med))
        self.status.setText(f"{len(self.lap_rows)} laps")
        if cand:
            self._assign("A", cand[0][1])
        if len(cand) > 1:
            self._assign("B", cand[1][1])

    # --------------------------------------------------------------- laps
    def _assign(self, slot, lap_dir):
        cur = self.lapA if slot == "A" else self.lapB
        if cur and os.path.dirname(cur.ztx_path) == lap_dir:
            return
        ztx = discovery.ztx_in(lap_dir)
        if not ztx:
            self.status.setText("No .ztx in that lap folder.")
            return
        try:
            lt, _ = discovery.lap_meta(lap_dir, self.hdr_cache)
            lap = LapData(ztx, lt, label=slot)
        except Exception as e:
            self.status.setText(f"Load failed: {e}")
            return
        if slot == "A":
            if self.lapA:
                self.lapA.close()
            self.lapA = lap
            self.showA = True
            self.btnA.setChecked(True)
        else:
            if self.lapB:
                self.lapB.close()
            self.lapB = lap
            self.showB = True
            self.btnB.setChecked(True)
        if not self.plotted:
            self.plotted = self._resolve_defaults(lap)
            self.chan_panel.build(lap.channels, self.plotted)
        elif not self.chan_panel.tree.topLevelItemCount():
            self.chan_panel.build(lap.channels, self.plotted)
        self._after_lap_change()

    def _unassign(self, slot):
        if slot == "B":
            self._clear_b()
        elif self.lapA:
            self.lapA.close()
            self.lapA = None
            self._after_lap_change()

    def _toggle_lap(self, slot, flip=False):
        btn = self.btnA if slot == "A" else self.btnB
        if flip:
            btn.setChecked(not btn.isChecked())
        if slot == "A":
            self.showA = btn.isChecked()
        else:
            self.showB = btn.isChecked()
        self.render()

    def _toggle_dt(self):
        self.show_dt = self.delta_btn.isChecked()
        if self.show_dt and self.mode != "dist":
            self.toggle_mode()      # Δt is time gained per metre; only the distance axis shows it
        else:
            self.render()

    def _swap(self):
        self.lapA, self.lapB = self.lapB, self.lapA
        self.showA, self.showB = self.showB, self.showA
        self.btnA.setChecked(self.showA)
        self.btnB.setChecked(self.showB)
        for lap, lbl in ((self.lapA, "A"), (self.lapB, "B")):
            if lap:
                lap.label = lbl
        self._after_lap_change()

    def _clear_b(self):
        if self.lapB:
            self.lapB.close()
            self.lapB = None
        self._after_lap_change()

    def _after_lap_change(self):
        self._sync_origin()
        self._compare_bar()
        self.laps_win.set_slots(self._dir(self.lapA), self._dir(self.lapB))
        self.chan_panel.sync(self.plotted)
        self.render()

    def _dir(self, lap):
        return os.path.dirname(lap.ztx_path) if lap else None

    def _sync_origin(self):
        ref = self.lapA or self.lapB
        o = ref.gps_origin() if ref else None
        if o:
            for lap in (self.lapA, self.lapB):
                if lap:
                    lap.set_origin(*o)

    def _lap_meta_text(self, lap):
        if not lap:
            return ""
        d = os.path.dirname(lap.ztx_path)
        txt = discovery.describe(d, self.meta_cache)
        st = self.meta_cache.get("start:" + d, False)
        if st is False:
            try:
                from .reader import read_lap_header
                st = read_lap_header(d).start
            except Exception:
                st = None
            self.meta_cache["start:" + d] = st
        if st:
            txt += "  ·  " + time.strftime("%d %b %Y", time.localtime(st))
        return txt

    def _compare_bar(self):
        for lap, btn, meta, slot in ((self.lapA, self.btnA, self.metaA, "A"),
                                     (self.lapB, self.btnB, self.metaB, "B")):
            btn.setText(f"{slot}  {fmt_time(lap.lap_time) if lap else '--'}")
            btn.setEnabled(lap is not None)
            meta.setText(self._lap_meta_text(lap))
        both = self.lapA and self.lapB and self.lapA.lap_time and self.lapB.lap_time
        self.delta_btn.setText(f"Δ {self.lapA.lap_time - self.lapB.lap_time:+.3f}"
                               if both else "Δ  --")

    # ----------------------------------------------------------- channels
    def _resolve_defaults(self, lap):
        out = []
        for names in ROLE_CHANNELS.values():
            for n in names:
                if lap.has(n):
                    out.append(n)
                    break
        return out

    def _set_channels(self, channels):
        self.plotted = list(channels)[:MAX_PANELS]
        self.render()

    # -------------------------------------------------------------- plots
    def _vis(self):
        return (self.lapA if self.showA else None), (self.lapB if self.showB else None)

    def _ref(self):
        """The lap the x-axis follows."""
        a, b = self._vis()
        return a or b or self.lapA or self.lapB

    def _plots(self):
        return [pr["plot"] for pr in self.panels.values()] + ([self.dt["plot"]] if self.dt else [])

    def _eff_mode(self):
        if self.mode != "dist":
            return "time"
        for lap in self._vis():
            if lap and not lap.has_distance:
                return "time"
        return "dist"

    def _can_dt(self):
        """Δt needs both laps shown and a distance base for each."""
        a, b = self._vis()
        return bool(a and b and a.has_distance and b.has_distance)

    def toggle_mode(self):
        old = self._eff_mode()
        self.mode = "time" if self.mode == "dist" else "dist"
        new = self._eff_mode()
        ref = self.lapA or self.lapB
        if ref and old != new and self.cursor_x:
            self.cursor_x = (ref.to_dist(self.cursor_x) if new == "dist"
                             else ref.to_time(self.cursor_x))
        self.mode_btn.setText(f"x: {'Distance' if self.mode == 'dist' else 'Time'}")
        self._xset = False
        self.render(autorange=True)

    def render(self, autorange=False):
        self._m = self._eff_mode()
        if self.mode == "dist" and self._m == "time":
            self.status.setText("Distance not available for a selected lap — showing time.")
        can = self._can_dt()
        want = self.show_dt and self._m == "dist" and can
        self.delta_btn.setEnabled(can)       # live only when it can do something,
        self.delta_btn.setChecked(want)      # and checked only when the panel is up
        new = self._sync_panels(want)
        self._refresh()
        if autorange or not self._xset:
            self.autorange()
        else:
            self._limit_x(self._plots())
            for p in new:
                p.enableAutoRange(axis="y")
        self.update_track()
        self._build_readout()
        self.update_cursor(self.cursor_x)

    def _make_panel(self, ch):
        p = pg.PlotItem(viewBox=CursorViewBox(self._cursor_to))
        p.setMenuEnabled(False)
        p.getViewBox().setBorder(pg.mkPen(EDGE, width=1))
        add_grid(p)
        ax = p.getAxis("left")
        ax.setWidth(52)
        ax.setStyle(tickFont=mono(8))
        lbl = pg.TextItem(anchor=(0, 0), color=MUTED)
        lbl.setFont(mono(8))
        val = pg.TextItem(anchor=(1, 0))
        val.setFont(mono(9))
        p.addItem(lbl, ignoreBounds=True)
        p.addItem(val, ignoreBounds=True)
        cA = p.plot()
        cB = p.plot()
        for c in (cA, cB):
            # must be set on the curve: PlotItem.setDownsampling does not reach
            # items added later, and full-resolution repaints make dragging crawl
            c.setDownsampling(auto=True, method="peak")
            c.setClipToView(True)
            cache_curve(c)
        vline = pg.InfiniteLine(angle=90, movable=False,
                                pen=pg.mkPen(CROSS, width=1, style=Qt.PenStyle.DashLine))
        p.addItem(vline, ignoreBounds=True)
        p.getViewBox().sigRangeChanged.connect(lambda *_: self._pin(p, lbl, val))
        return {"ch": ch, "plot": p, "cA": cA, "cB": cB, "vline": vline, "lbl": lbl, "val": val}

    def _make_dt(self):
        p = pg.PlotItem(viewBox=CursorViewBox(self._cursor_to))
        p.setMenuEnabled(False)
        p.getViewBox().setBorder(pg.mkPen(EDGE, width=1))
        add_grid(p)
        p.getAxis("left").setWidth(52)
        p.getAxis("left").setStyle(tickFont=mono(8))
        p.addLine(y=0, pen=pg.mkPen(GRID, width=1))
        lbl = pg.TextItem(anchor=(0, 0), color=MUTED)
        lbl.setFont(mono(8))
        lbl.setText("Δt: B − A [s]   (above 0 = A ahead)")
        val = pg.TextItem(anchor=(1, 0))
        val.setFont(mono(9))
        p.addItem(lbl, ignoreBounds=True)
        p.addItem(val, ignoreBounds=True)
        self.dtcurve = p.plot(pen=pg.mkPen(INK, width=1.6))
        self.dtline = pg.InfiniteLine(angle=90, movable=False,
                                      pen=pg.mkPen(CROSS, width=1, style=Qt.PenStyle.DashLine))
        p.addItem(self.dtline, ignoreBounds=True)
        return {"plot": p, "lbl": lbl, "val": val}

    def _pin(self, p, lbl, val):
        (x0, x1), (y0, y1) = p.getViewBox().viewRange()
        lbl.setPos(x0, y1)
        val.setPos(x1, y1)

    def _sync_panels(self, want_dt):
        # keep panels across edits so zoom survives; only add/remove what changed
        for ch in list(self.panels):
            if ch not in self.plotted:
                del self.panels[ch]
        new = []
        for ch in self.plotted:
            if ch not in self.panels:
                self.panels[ch] = self._make_panel(ch)
                new.append(self.panels[ch]["plot"])
        if want_dt and not self.dt:
            self.dt = self._make_dt()
            new.append(self.dt["plot"])
        elif not want_dt and self.dt:
            self.dt = None
        self._reflow()
        return new

    def _reflow(self):
        self.glw.clear()
        items = ([self.dt["plot"]] if self.dt else []) + [self.panels[c]["plot"] for c in self.plotted]
        if items and items[0] is not self.xref:
            self._xset = False           # new x-link master: its range has never been set
        self.xref = items[0] if items else None
        for r, it in enumerate(items):
            self.glw.addItem(it, row=r, col=0)
            if it is not self.xref:
                it.setXLink(self.xref)
            it.showAxis("bottom", it is items[-1])
            self.glw.ci.layout.setRowStretchFactor(r, 2 if (self.dt and it is self.dt["plot"]) else 4)
        if items:
            items[-1].setLabel("bottom", "Distance" if self._m == "dist" else "Time",
                               units="m" if self._m == "dist" else "s")

    def chan_color(self, ch):
        """Per-channel colour when a single lap is shown, else the lap's colour."""
        a, b = self._vis()
        if a and b:
            return None
        i = self.plotted.index(ch) if ch in self.plotted else 0
        return CHAN_COLORS[i % len(CHAN_COLORS)]

    def _refresh(self):
        m = self._m
        a, b = self._vis()
        for ch, pr in self.panels.items():
            solo = self.chan_color(ch)
            u = self.cat.unit(ch)[0]
            pr["lbl"].setText(f"{ch} [{u}]" if u else ch)
            pr["lbl"].setColor(solo or MUTED)
            for lap, curve, base, dash in ((a, pr["cA"], A_COLOR, False),
                                           (b, pr["cB"], B_COLOR, True)):
                if lap and lap.has(ch):
                    col = solo or base
                    style = Qt.PenStyle.DashLine if (dash and not solo) else Qt.PenStyle.SolidLine
                    curve.setPen(pg.mkPen(col, width=1.6 if not dash else 1.3, style=style))
                    curve.setData(*lap.xy(ch, m))
                else:
                    curve.clear()
        if self.dt and a and b:
            dmax = min(a.dist_max(), b.dist_max())
            dg = np.linspace(0, dmax, 600)
            ta, tb = a.t_of_distance(dg), b.t_of_distance(dg)
            if ta is not None and tb is not None:
                self.dtcurve.setData(dg, tb - ta)

    def _limit_x(self, plots):
        """The lap is the whole x world: zoom and pan stop at 0..xmax."""
        ref = self._ref()
        xmax = ref.xmax(self._m) if ref else None
        for p in plots:
            p.getViewBox().setLimits(xMin=0, xMax=xmax)
        return xmax

    def autorange(self):
        self._m = self._eff_mode()
        plots = self._plots()
        for p in plots:
            p.enableAutoRange(axis="y")
        xmax = self._limit_x(plots)      # limits before the range, or a stale xMax clips it
        if self.xref and xmax:
            self.xref.setXRange(0, xmax, padding=0)
            self._xset = True

    def update_track(self):
        # both laps ran the same circuit, so a second outline over it is only noise;
        # the A / B dots carry which lap is where
        g = next((t for lap in self._vis() if lap and (t := lap.gps_track())), None)
        if g:
            self.track.setData(*g)
            self.sf.setData(g[0][:1], g[1][:1])     # a lap starts where it was cut
        else:
            self.track.clear()
            self.sf.clear()

    # ------------------------------------------------------------ cursor
    def _cursor_to(self, x):
        ref = self._ref()
        if not ref:
            return
        x = max(0.0, min(x, ref.xmax(self._m)))
        # coalesce: a drag fires far more often than the stack can repaint
        now = time.perf_counter()
        if now - self._last_move >= FRAME_MS:
            self._last_move = now
            self._pending_x = None
            self.update_cursor(x)
        else:
            self._pending_x = x
            if not self._tick.isActive():
                self._tick.start(8)

    def _flush_cursor(self):
        if self._pending_x is not None:
            x, self._pending_x = self._pending_x, None
            self._last_move = time.perf_counter()
            self.update_cursor(x)

    def _map_cursor_to(self, px, py):
        ref = self._ref()
        if ref:
            xq = ref.nearest_x(px, py, self._m)
            if xq is not None:
                self._cursor_to(xq)

    def update_cursor(self, x):
        self.cursor_x = x
        a, b = self._vis()
        for pr in self.panels.values():
            pr["vline"].setPos(x)
        if self.dt:
            self.dtline.setPos(x)
        self.cursor_lbl.setText(f"cursor  {x:.0f} m" if self._m == "dist" else f"cursor  {x:.2f} s")
        for ch in self.plotted:
            pr = self.panels.get(ch)
            if not pr:
                continue
            va = a.value_at(ch, x, self._m) if a else None
            vb = b.value_at(ch, x, self._m) if b else None
            solo = self.chan_color(ch)
            if solo:
                v = va if a else vb
                pr["val"].setText(fmt_val(ch, v), color=solo)
            else:
                pr["val"].setHtml(f"<span style='color:{A_COLOR}'>{fmt_val(ch, va)}</span>"
                                  f"  <span style='color:{B_COLOR}'>{fmt_val(ch, vb)}</span>")
            r = self._read_rows.get(ch)
            if r:
                r["a"].setText(fmt_val(ch, va) if a else "")
                r["b"].setText(fmt_val(ch, vb) if b else "")
                r["d"].setText(f"{va - vb:+.1f}" if (va is not None and vb is not None) else "")
        for lap, dot in ((a, self.dotA), (b, self.dotB)):
            p = lap.gps_point(x, self._m) if lap else None
            dot.setData([p[0]], [p[1]]) if p else dot.setData([], [])
        if self.dt and a and b:
            ta = a.t_of_distance(np.array([x], float))
            tb = b.t_of_distance(np.array([x], float))
            if ta is not None and tb is not None:
                d = float((tb - ta)[0])
                self.dt["val"].setHtml(
                    f"<span style='color:{A_COLOR if d >= 0 else B_COLOR}'>{d:+.3f} s</span>")

    def _build_readout(self):
        while self.read_grid.count():
            it = self.read_grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._read_rows = {}
        for r, ch in enumerate(self.plotted):
            cells = {}
            solo = self.chan_color(ch)
            name = QtWidgets.QLabel(ch)
            name.setStyleSheet(f"color:{solo or MUTED};")
            name.setFont(mono(8))
            self.read_grid.addWidget(name, r, 0)
            for col, (key, color) in enumerate((("a", solo or A_COLOR),
                                                ("b", solo or B_COLOR),
                                                ("d", MUTED)), start=1):
                lab = QtWidgets.QLabel("")
                lab.setStyleSheet(f"color:{color};")
                lab.setFont(mono(9))
                lab.setAlignment(Qt.AlignmentFlag.AlignRight)
                self.read_grid.addWidget(lab, r, col)
                cells[key] = lab
            self._read_rows[ch] = cells

    # --------------------------------------------------------- view modes
    def toggle_focus(self):
        if not self._focus:
            self.cfg["splitter_open"] = self.split.sizes()
            self.split.setSizes([0, sum(self.split.sizes()), 0])
            self._focus = True
        else:
            self.split.setSizes(self.cfg.get("splitter_open") or [250, 1050, 300])
            self._focus = False

    def _toggle_max(self):
        self.showNormal() if self.isMaximized() else self.showMaximized()

    def _toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def save_png(self):
        if not self.panels:
            return
        f, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save PNG", "", "PNG (*.png)")
        if not f:
            return
        if not f.lower().endswith(".png"):
            f += ".png"
        overlays = ([pr["vline"] for pr in self.panels.values()]
                    + [pr["val"] for pr in self.panels.values()])
        if self.dt:
            overlays += [self.dtline, self.dt["val"]]
        for o in overlays:
            o.setVisible(False)
        try:
            from pyqtgraph.exporters import ImageExporter
            ex = ImageExporter(self.glw.scene())
            ex.parameters()["width"] = 2000
            ex.export(f)
            self.status.setText(f"Saved {f}")
        finally:
            for o in overlays:
                o.setVisible(True)

    def showEvent(self, e):
        super().showEvent(e)
        dark_frame(int(self.winId()))

    def closeEvent(self, e):
        if self.scan:
            self.scan.stop()
        self.laps_win.close()
        self._save_cfg()
        discovery.save_cache(HEADER_CACHE, dict(self.hdr_cache))
        for lap in (self.lapA, self.lapB):
            if lap:
                lap.close()
        super().closeEvent(e)


def main():
    QtWidgets.QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QtWidgets.QApplication(sys.argv)
    win = MotoData(sys.argv[1] if len(sys.argv) > 1 else "")
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
