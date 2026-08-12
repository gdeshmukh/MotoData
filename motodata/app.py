"""app.py -- MotoData PyQt6 + pyqtgraph telemetry viewer.

Open a folder (WinTAX Data root, a session, or a single car), scroll the laps,
pick two to compare, and read channels against a synced crosshair and a GPS
track map. Data comes through motodata.reader / .lapdata / .discovery.
"""
from __future__ import annotations
import os, sys, json
import numpy as np

from PyQt6 import QtGui, QtWidgets
from PyQt6.QtCore import Qt, QObject, QRunnable, QThreadPool, pyqtSignal
import pyqtgraph as pg

from .catalog import Catalog, group as chan_group
from .lapdata import LapData
from . import discovery

# palette
A_COLOR = "#f0a500"     # lap A (amber)
B_COLOR = "#2bd4d9"     # lap B (cyan)
BG, PANEL, GRID = "#101317", "#161a1f", "#262c34"
INK, MUTED, CROSS = "#c8cdd4", "#7d8894", "#e8ecf1"

# default stack resolved per-lap by role, so it also works on non-Toyota cars
ROLE_CHANNELS = {
    "speed":    ["vCar", "CarSpd_vCar", "GPS_CarSpeed"],
    "throttle": ["rPedal", "rThrottle1"],
    "brake":    ["pBrakeMCF", "pBrakeMCR"],
    "gear":     ["nGear", "GBX_NGearLeverOut"],
    "steer":    ["EPS_aSteering", "aSteer"],
}
MAX_PANELS = 12
STATE_DIR = os.path.join(os.path.expanduser("~"), ".motodata")
CONFIG = os.path.join(STATE_DIR, "config.json")
HEADER_CACHE = os.path.join(STATE_DIR, "headers.json")

pg.setConfigOptions(antialias=False, background=BG, foreground=MUTED)


def fmt_time(s):
    if s is None or s != s:
        return "--"
    m = int(s // 60)
    return f"{m}:{s - 60 * m:06.3f}" if m else f"{s:.3f}"


def fmt_val(name, v):
    if v is None or v != v:
        return "--"
    return str(int(round(v))) if "gear" in name.lower() else f"{v:.1f}"


# ---- background workers (generation-tagged so stale results are dropped) ----
class _WalkSig(QObject):
    result = pyqtSignal(int, object)


class Walk(QRunnable):
    def __init__(self, root, gen):
        super().__init__()
        self.root, self.gen, self.sig = root, gen, _WalkSig()

    def run(self):
        try:
            cars = discovery.group_by_car(discovery.find_lap_dirs(self.root))
        except Exception:
            cars = {}
        self.sig.result.emit(self.gen, cars)


class _ScanSig(QObject):
    lap = pyqtSignal(int, int, object)
    done = pyqtSignal(int)


class Scan(QRunnable):
    def __init__(self, lap_dirs, cache, gen):
        super().__init__()
        self.lap_dirs, self.cache, self.gen = lap_dirs, cache, gen
        self.sig, self._stop = _ScanSig(), False

    def stop(self):
        self._stop = True

    def run(self):
        for i, ld in enumerate(self.lap_dirs):
            if self._stop:
                return
            try:
                lt, mk = discovery.lap_meta(ld, self.cache)
            except Exception:
                lt, mk = None, None
            self.sig.lap.emit(self.gen, i, (lt, mk))
        self.sig.done.emit(self.gen)


class MotoData(QtWidgets.QMainWindow):
    def __init__(self, root=""):
        super().__init__()
        self.setWindowTitle("MotoData")
        self.resize(1600, 950)
        self.cat = Catalog()
        self.pool = QThreadPool.globalInstance()
        self.hdr_cache = discovery.load_cache(HEADER_CACHE)
        self.cfg = self._load_cfg()

        self.cars = {}
        self.lap_rows = []
        self.scan = None
        self.lapA = self.lapB = None
        self.active = "A"
        self.mode = "dist"           # user intent; _m is the effective mode
        self._m = "dist"
        self.plotted = list(self.cfg.get("plotted") or [])
        self.cursor_x = 0.0
        self.panels = {}             # ch -> {"plot","cA","cB","vline","lbl","val"}
        self.dt = None
        self.xref = None
        self._gen = self._walk_gen = 0
        self._chan_built = False
        self._auto_done = False
        self._focus = False
        self._xset = False
        self._read_rows = {}

        self._build_ui()
        self._apply_style()
        self._shortcuts()

        start = root or self.cfg.get("last_folder") or os.environ.get("MOTODATA_ROOT", "")
        if start and os.path.isdir(start):
            self.open_folder(start)

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        self.split = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        self.setCentralWidget(self.split)
        self.split.addWidget(self._build_left())
        self.split.addWidget(self._build_center())
        self.split.addWidget(self._build_right())
        self.split.setStretchFactor(1, 1)
        sizes = self.cfg.get("splitter_open") or [300, 1080, 300]
        if len(sizes) != 3 or (sizes[0] == 0 and sizes[2] == 0):
            sizes = [300, 1080, 300]
        self.split.setSizes(sizes)
        self.status = QtWidgets.QLabel("Open a folder to begin.")
        self.statusBar().addWidget(self.status)

    def _build_left(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(6)
        top = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("Open folder")
        b.clicked.connect(self.open_folder_dialog)
        top.addWidget(b)
        self.car_combo = QtWidgets.QComboBox()
        self.car_combo.currentIndexChanged.connect(self._on_car_pick)
        top.addWidget(self.car_combo, 1)
        v.addLayout(top)
        self.path_lbl = QtWidgets.QLabel("")
        self.path_lbl.setStyleSheet(f"color:{MUTED};")
        v.addWidget(self.path_lbl)

        inner = QtWidgets.QSplitter(Qt.Orientation.Vertical)
        v.addWidget(inner, 1)
        lapbox = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(lapbox)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(4)
        head = QtWidgets.QHBoxLayout()
        head.addWidget(self._h("LAPS"))
        head.addStretch(1)
        self.active_btn = QtWidgets.QPushButton("assign: A")
        self.active_btn.setToolTip("Which slot a lap click/arrow fills (Tab flips it)")
        self.active_btn.clicked.connect(self._flip_active)
        head.addWidget(self.active_btn)
        lv.addLayout(head)
        self.lap_list = QtWidgets.QListWidget()
        self.lap_list.setUniformItemSizes(True)
        self.lap_list.setFont(QtGui.QFont("Cascadia Mono", 9))
        self.lap_list.currentRowChanged.connect(self._on_lap_row)
        lv.addWidget(self.lap_list, 1)
        inner.addWidget(lapbox)

        chbox = QtWidgets.QWidget()
        cv = QtWidgets.QVBoxLayout(chbox)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(4)
        cv.addWidget(self._h("CHANNELS"))
        self.filter = QtWidgets.QLineEdit()
        self.filter.setPlaceholderText("filter name or description")
        self.filter.textChanged.connect(self._filter_channels)
        cv.addWidget(self.filter)
        self.chan_tree = QtWidgets.QTreeWidget()
        self.chan_tree.setHeaderHidden(True)
        self.chan_tree.setFont(QtGui.QFont("Cascadia Mono", 9))
        self.chan_tree.itemChanged.connect(self._on_chan_toggle)
        cv.addWidget(self.chan_tree, 1)
        inner.addWidget(chbox)
        inner.setSizes([560, 360])
        return w

    def _build_center(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        bar = QtWidgets.QWidget()
        bar.setObjectName("compbar")
        h = QtWidgets.QHBoxLayout(bar)
        h.setContentsMargins(10, 6, 10, 6)
        self.slotA = QtWidgets.QLabel("A  --")
        self.slotB = QtWidgets.QLabel("B  --")
        for lab, col in ((self.slotA, A_COLOR), (self.slotB, B_COLOR)):
            lab.setFont(QtGui.QFont("Cascadia Mono", 11, QtGui.QFont.Weight.Bold))
            lab.setStyleSheet(f"color:{col};")
        self.delta_lbl = QtWidgets.QLabel("")
        self.delta_lbl.setFont(QtGui.QFont("Cascadia Mono", 11))
        h.addWidget(self.slotA)
        h.addWidget(QtWidgets.QLabel("vs"))
        h.addWidget(self.slotB)
        h.addWidget(self.delta_lbl)
        for text, fn in (("swap", self._swap), ("clear B", self._clear_b)):
            btn = QtWidgets.QPushButton(text)
            btn.clicked.connect(fn)
            h.addWidget(btn)
        h.addStretch(1)
        self.mode_btn = QtWidgets.QPushButton("x: Distance")
        self.mode_btn.clicked.connect(self.toggle_mode)
        h.addWidget(self.mode_btn)
        for text, fn in (("reset", self.autorange), ("PNG", self.save_png), ("focus", self.toggle_focus)):
            btn = QtWidgets.QPushButton(text)
            btn.clicked.connect(fn)
            h.addWidget(btn)
        v.addWidget(bar)
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.ci.layout.setSpacing(4)
        v.addWidget(self.glw, 1)
        self.mproxy = pg.SignalProxy(self.glw.scene().sigMouseMoved,
                                     rateLimit=60, slot=self._on_graph_move)
        return w

    def _build_right(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(6)
        v.addWidget(self._h("TRACK MAP"))
        self.map = pg.PlotWidget()
        self.map.setAspectLocked(True)
        self.map.hideAxis("left")
        self.map.hideAxis("bottom")
        self.map.setMenuEnabled(False)
        self.map.setMouseEnabled(False, False)
        self.map.setMinimumHeight(240)
        self.mapB = self.map.plot(pen=pg.mkPen(B_COLOR, width=1.5))
        self.mapA = self.map.plot(pen=pg.mkPen(A_COLOR, width=2))     # A drawn over B
        self.dotB = pg.ScatterPlotItem(size=8, brush=B_COLOR, pen=None)
        self.dotA = pg.ScatterPlotItem(size=11, brush=A_COLOR, pen=pg.mkPen(BG))
        self.map.addItem(self.dotB)
        self.map.addItem(self.dotA)
        v.addWidget(self.map, 3)
        v.addWidget(self._h("AT CURSOR"))
        self.cursor_lbl = QtWidgets.QLabel("cursor  --")
        self.cursor_lbl.setFont(QtGui.QFont("Cascadia Mono", 9))
        self.cursor_lbl.setStyleSheet(f"color:{MUTED};")
        v.addWidget(self.cursor_lbl)
        self.readout = QtWidgets.QWidget()
        self.read_grid = QtWidgets.QGridLayout(self.readout)
        self.read_grid.setContentsMargins(0, 0, 0, 0)
        self.read_grid.setVerticalSpacing(2)
        self.read_grid.setHorizontalSpacing(10)
        v.addWidget(self.readout)
        v.addStretch(1)
        self.mapproxy = pg.SignalProxy(self.map.scene().sigMouseMoved,
                                       rateLimit=60, slot=self._on_map_move)
        return w

    def _h(self, text):
        lab = QtWidgets.QLabel(text)
        lab.setStyleSheet(f"color:{MUTED}; letter-spacing:1px; font-size:10px;")
        return lab

    def _apply_style(self):
        self.setStyleSheet(f"""
            QWidget {{ background:{PANEL}; color:{INK}; font-family:'Segoe UI'; font-size:12px; }}
            QMainWindow, QSplitter {{ background:{BG}; }}
            QPushButton {{ background:#22262c; border:1px solid #30353c; padding:3px 9px; border-radius:3px; }}
            QPushButton:hover {{ background:#2b3037; }}
            QLineEdit, QComboBox {{ background:#0e1114; border:1px solid #2a2f36; padding:4px; border-radius:3px; }}
            QListWidget, QTreeWidget {{ background:#0e1114; border:1px solid #23282f; outline:0; }}
            QListWidget::item {{ padding:2px 4px; }}
            QListWidget::item:selected {{ background:#2a2f37; color:{INK}; }}
            #compbar {{ background:{BG}; border-bottom:1px solid {GRID}; }}
            QStatusBar {{ background:{BG}; color:{MUTED}; }}
            QScrollBar:vertical {{ background:{PANEL}; width:11px; margin:0; }}
            QScrollBar::handle:vertical {{ background:#2b323a; border-radius:5px; min-height:24px; }}
            QScrollBar::add-line, QScrollBar::sub-line {{ height:0; }}
        """)

    def _shortcuts(self):
        for key, fn in (("z", self.toggle_mode), ("h", self.autorange),
                        ("f", self.toggle_focus), ("Tab", self._flip_active),
                        ("F11", self._toggle_fullscreen)):
            QtGui.QShortcut(QtGui.QKeySequence(key), self).activated.connect(fn)

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
    def open_folder_dialog(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select a WinTAX data / session / car folder", self.cfg.get("last_folder", ""))
        if d:
            self.open_folder(d)

    def open_folder(self, root):
        self.cfg["last_folder"] = root
        self.path_lbl.setText(os.path.normpath(root))
        self.status.setText("Scanning folders…")
        self._walk_gen += 1
        w = Walk(root, self._walk_gen)
        w.sig.result.connect(self._on_cars)
        self.pool.start(w)

    def _on_cars(self, gen, cars):
        if gen != self._walk_gen:
            return
        self.cars = cars
        self.car_combo.blockSignals(True)
        self.car_combo.clear()
        for car in cars:
            self.car_combo.addItem(os.path.basename(car) or car, car)
        self.car_combo.setCurrentIndex(0 if cars else -1)
        self.car_combo.blockSignals(False)
        if cars:
            self.select_car(next(iter(cars)))
        else:
            self.status.setText("No laps found under that folder.")

    def _on_car_pick(self, idx):
        car = self.car_combo.itemData(idx)
        if car:
            self.select_car(car)

    def select_car(self, car):
        if self.scan:
            self.scan.stop()
            try:
                self.scan.sig.lap.disconnect()
                self.scan.sig.done.disconnect()
            except TypeError:
                pass
        self._gen += 1
        self._chan_built = False
        self._auto_done = False
        laps = self.cars.get(car, [])
        self.lap_rows = [{"dir": d, "lt": None, "mk": None} for d in laps]
        self.lap_list.blockSignals(True)
        self.lap_list.clear()
        for d in laps:
            self.lap_list.addItem(os.path.basename(d).replace("Lap_", ""))
        self.lap_list.blockSignals(False)
        self.status.setText(f"{len(laps)} laps — reading times…")
        self.scan = Scan(laps, self.hdr_cache, self._gen)
        self.scan.sig.lap.connect(self._on_lap_meta)
        self.scan.sig.done.connect(self._on_scan_done)
        self.pool.start(self.scan)

    def _on_lap_meta(self, gen, i, meta):
        if gen != self._gen or i >= len(self.lap_rows):
            return
        self.lap_rows[i]["lt"], self.lap_rows[i]["mk"] = meta
        self._relabel_row(i)

    def _relabel_row(self, i):
        row, item = self.lap_rows[i], self.lap_list.item(i)
        if not item:
            return
        best, d = self._best_time(), ""
        if best and row["lt"] and row["lt"] > 0:
            d = "  best" if row["lt"] <= best else f"  {row['lt'] - best:+.2f}"
        mk = f" {row['mk']}" if row["mk"] and row["mk"] not in ("", "lap") else ""
        item.setText(f"{fmt_time(row['lt']):>8}{d}{mk}")

    def _best_time(self):
        ts = [r["lt"] for r in self.lap_rows if r["lt"] and r["lt"] > 0]
        if ts:                                    # drop pit/out fragments vs the field
            med = float(np.median(ts))
            ts = [t for t in ts if t >= 0.5 * med]
        return min(ts) if ts else None

    def _on_scan_done(self, gen):
        if gen != self._gen:
            return
        for i in range(len(self.lap_rows)):
            self._relabel_row(i)
        self.status.setText(f"{len(self.lap_rows)} laps")
        if not self._auto_done:
            self._auto_done = True
            self._auto_assign()

    def _auto_assign(self):
        ts = [r["lt"] for r in self.lap_rows if r["lt"] and r["lt"] > 0]
        med = float(np.median(ts)) if ts else None
        cand = sorted((r["lt"], r["dir"]) for r in self.lap_rows
                      if r["lt"] and r["lt"] > 0 and (med is None or r["lt"] >= 0.5 * med))
        self.mode = "dist"
        self.mode_btn.setText("x: Distance")
        if cand:
            self._assign("A", cand[0][1])
        if len(cand) > 1:
            self._assign("B", cand[1][1])

    # --------------------------------------------------------------- laps
    def _flip_active(self):
        self.active = "B" if self.active == "A" else "A"
        self.active_btn.setText(f"assign: {self.active}")

    def _on_lap_row(self, i):
        if i < 0 or i >= len(self.lap_rows):
            return
        shift = QtWidgets.QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier
        slot = ("B" if self.active == "A" else "A") if shift else self.active
        self._assign(slot, self.lap_rows[i]["dir"])

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
        else:
            if self.lapB:
                self.lapB.close()
            self.lapB = lap
        if not self._chan_built:
            self._build_channels(lap)
        self._sync_origin()
        self._row_colors()
        self._compare_bar()
        self.render()

    def _swap(self):
        self.lapA, self.lapB = self.lapB, self.lapA
        for lap, lbl in ((self.lapA, "A"), (self.lapB, "B")):
            if lap:
                lap.label = lbl
        self._sync_origin()
        self._row_colors()
        self._compare_bar()
        self.render()

    def _clear_b(self):
        if self.lapB:
            self.lapB.close()
            self.lapB = None
        self._row_colors()
        self._compare_bar()
        self.render()

    def _sync_origin(self):
        ref = self.lapA or self.lapB
        o = ref.gps_origin() if ref else None
        if o:
            for lap in (self.lapA, self.lapB):
                if lap:
                    lap.set_origin(*o)

    def _row_colors(self):
        da = os.path.dirname(self.lapA.ztx_path) if self.lapA else None
        db = os.path.dirname(self.lapB.ztx_path) if self.lapB else None
        for i, row in enumerate(self.lap_rows):
            item = self.lap_list.item(i)
            if not item:
                continue
            if row["dir"] == da:
                item.setBackground(QtGui.QColor(60, 44, 12))
                item.setForeground(QtGui.QColor(A_COLOR))
            elif row["dir"] == db:
                item.setBackground(QtGui.QColor(12, 46, 50))
                item.setForeground(QtGui.QColor(B_COLOR))
            else:
                item.setBackground(QtGui.QColor(14, 17, 20))
                item.setForeground(QtGui.QColor(INK))

    def _compare_bar(self):
        self.slotA.setText(f"A  {fmt_time(self.lapA.lap_time) if self.lapA else '--'}")
        self.slotB.setText(f"B  {fmt_time(self.lapB.lap_time) if self.lapB else '--'}")
        if self.lapA and self.lapB and self.lapA.lap_time and self.lapB.lap_time:
            d = self.lapA.lap_time - self.lapB.lap_time
            self.delta_lbl.setText(f"Δ {d:+.3f}s")
            self.delta_lbl.setStyleSheet(f"color:{A_COLOR if d < 0 else B_COLOR};")
        else:
            self.delta_lbl.setText("")

    # ----------------------------------------------------------- channels
    def _resolve_defaults(self, lap):
        out = []
        for names in ROLE_CHANNELS.values():
            for n in names:
                if lap.has(n):
                    out.append(n)
                    break
        return out

    def _build_channels(self, lap):
        self._chan_built = True
        present = [c for c in self.plotted if lap.has(c)]
        self.plotted = present or self._resolve_defaults(lap) or self.plotted
        self.chan_tree.blockSignals(True)
        self.chan_tree.clear()
        groups = {}
        for c in sorted(lap.channels):
            groups.setdefault(chan_group(c), []).append(c)
        for g in sorted(groups):
            parent = QtWidgets.QTreeWidgetItem([g])
            parent.setForeground(0, QtGui.QColor(MUTED))
            parent.setFlags(Qt.ItemFlag.ItemIsEnabled)
            for c in groups[g]:
                it = QtWidgets.QTreeWidgetItem([c])
                it.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
                it.setCheckState(0, Qt.CheckState.Checked if c in self.plotted else Qt.CheckState.Unchecked)
                if self.cat.description(c):
                    it.setToolTip(0, self.cat.description(c))
                parent.addChild(it)
            self.chan_tree.addTopLevelItem(parent)
            if any(c in self.plotted for c in groups[g]):
                parent.setExpanded(True)
        self.chan_tree.blockSignals(False)

    def _on_chan_toggle(self, item, col):
        c = item.text(0)
        on = item.checkState(0) == Qt.CheckState.Checked
        if on and c not in self.plotted:
            if len(self.plotted) >= MAX_PANELS:
                item.setCheckState(0, Qt.CheckState.Unchecked)
                self.status.setText(f"Max {MAX_PANELS} channels.")
                return
            self.plotted.append(c)
        elif not on and c in self.plotted:
            self.plotted.remove(c)
        self.render()

    def _filter_channels(self, text):
        t = text.lower().strip()
        for i in range(self.chan_tree.topLevelItemCount()):
            p = self.chan_tree.topLevelItem(i)
            shown = 0
            for j in range(p.childCount()):
                ch = p.child(j)
                match = (not t) or t in ch.text(0).lower() or t in self.cat.description(ch.text(0)).lower()
                ch.setHidden(not match)
                shown += match
            p.setHidden(shown == 0)
            if t and shown:
                p.setExpanded(True)

    # -------------------------------------------------------------- plots
    def _eff_mode(self):
        if self.mode != "dist":
            return "time"
        if self.lapA and not self.lapA.has_distance:
            return "time"
        if self.lapB and not self.lapB.has_distance:
            return "time"
        return "dist"

    def _want_dt(self):
        return (self._eff_mode() == "dist" and self.lapA and self.lapB
                and self.lapA.has_distance and self.lapB.has_distance)

    def toggle_mode(self):
        old = self._eff_mode()
        self.mode = "time" if self.mode == "dist" else "dist"
        new = self._eff_mode()
        if self.lapA and old != new and self.cursor_x:
            self.cursor_x = self.lapA.to_dist(self.cursor_x) if new == "dist" else self.lapA.to_time(self.cursor_x)
        self.mode_btn.setText(f"x: {'Distance' if self.mode == 'dist' else 'Time'}")
        self._xset = False
        self.render(autorange=True)

    def render(self, autorange=False):
        self._m = self._eff_mode()
        if self.mode == "dist" and self._m == "time":
            self.status.setText("Distance not available for a selected lap — showing time.")
        new = self._sync_panels(self._want_dt())
        self._refresh()
        if autorange or not self._xset:
            self.autorange()
        else:
            for p in new:
                p.enableAutoRange(axis="y")
        self.update_map_tracks()
        self._build_readout()
        self.update_cursor(self.cursor_x)

    def _make_panel(self, ch):
        p = pg.PlotItem()
        p.setMenuEnabled(False)
        p.showGrid(x=True, y=True, alpha=0.12)
        p.setClipToView(True)
        p.setDownsampling(auto=True, mode="peak")
        ax = p.getAxis("left")
        ax.setWidth(52)
        ax.setStyle(tickFont=QtGui.QFont("Consolas", 8))
        u = self.cat.unit(ch)[0]
        lbl = pg.TextItem(anchor=(0, 0), color=MUTED)
        lbl.setFont(QtGui.QFont("Consolas", 8))
        lbl.setText(f"{ch} [{u}]" if u else ch)
        val = pg.TextItem(anchor=(1, 0))
        val.setFont(QtGui.QFont("Cascadia Mono", 9))
        p.addItem(lbl, ignoreBounds=True)
        p.addItem(val, ignoreBounds=True)
        cA = p.plot(pen=pg.mkPen(A_COLOR, width=1.6))
        cB = p.plot(pen=pg.mkPen(B_COLOR, width=1.2, style=Qt.PenStyle.DashLine))
        vline = pg.InfiniteLine(angle=90, movable=False,
                                pen=pg.mkPen(CROSS, width=1, style=Qt.PenStyle.DashLine))
        p.addItem(vline, ignoreBounds=True)
        p.getViewBox().sigRangeChanged.connect(lambda *_: self._pin(p, lbl, val))
        return {"ch": ch, "plot": p, "cA": cA, "cB": cB, "vline": vline, "lbl": lbl, "val": val}

    def _make_dt(self):
        p = pg.PlotItem()
        p.setMenuEnabled(False)
        p.showGrid(x=True, y=True, alpha=0.12)
        p.getAxis("left").setWidth(52)
        p.getAxis("left").setStyle(tickFont=QtGui.QFont("Consolas", 8))
        p.addLine(y=0, pen=pg.mkPen(GRID, width=1))
        lbl = pg.TextItem(anchor=(0, 0), color=MUTED)
        lbl.setFont(QtGui.QFont("Consolas", 8))
        lbl.setText("Δt: B − A [s]   (above 0 = A ahead)")
        val = pg.TextItem(anchor=(1, 0))
        val.setFont(QtGui.QFont("Cascadia Mono", 9))
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

    def _refresh(self):
        m = self._m
        for ch, pr in self.panels.items():
            if self.lapA and self.lapA.has(ch):
                pr["cA"].setData(*self.lapA.xy(ch, m))
            else:
                pr["cA"].clear()
            if self.lapB and self.lapB.has(ch):
                pr["cB"].setData(*self.lapB.xy(ch, m))
            else:
                pr["cB"].clear()
        if self.dt and self.lapA and self.lapB:
            dmax = min(self.lapA.dist_max(), self.lapB.dist_max())
            dg = np.linspace(0, dmax, 600)
            ta, tb = self.lapA.t_of_distance(dg), self.lapB.t_of_distance(dg)
            if ta is not None and tb is not None:
                self.dtcurve.setData(dg, tb - ta)

    def autorange(self):
        self._m = self._eff_mode()
        for pr in self.panels.values():
            pr["plot"].enableAutoRange(axis="y")
        if self.dt:
            self.dt["plot"].enableAutoRange(axis="y")
        if self.xref and self.lapA:
            self.xref.setXRange(0, self.lapA.xmax(self._m), padding=0)
            self._xset = True

    # -------------------------------------------------------------- map
    def update_map_tracks(self):
        for lap, curve in ((self.lapA, self.mapA), (self.lapB, self.mapB)):
            g = lap.gps_track() if lap else None
            if g:
                curve.setData(g[0], g[1])
            else:
                curve.clear()

    # ------------------------------------------------------------ cursor
    def _on_graph_move(self, evt):
        if not self.xref or not self.lapA:
            return
        pos = evt[0]
        if not self.glw.sceneBoundingRect().contains(pos):
            return
        x = self.xref.getViewBox().mapSceneToView(pos).x()
        self.update_cursor(max(0.0, min(x, self.lapA.xmax(self._m))))

    def _on_map_move(self, evt):
        if not self.lapA:
            return
        pos = evt[0]
        if not self.map.sceneBoundingRect().contains(pos):
            return
        pt = self.map.getViewBox().mapSceneToView(pos)
        xq = self.lapA.nearest_x(pt.x(), pt.y(), self._m)
        if xq is not None:
            self.update_cursor(xq)

    def update_cursor(self, x):
        self.cursor_x = x
        for pr in self.panels.values():
            pr["vline"].setPos(x)
        if self.dt:
            self.dtline.setPos(x)
        self.cursor_lbl.setText(f"cursor  {x:.0f} m" if self._m == "dist" else f"cursor  {x:.2f} s")
        for ch in self.plotted:
            pr = self.panels.get(ch)
            if not pr:
                continue
            va = self.lapA.value_at(ch, x, self._m) if self.lapA else None
            vb = self.lapB.value_at(ch, x, self._m) if self.lapB else None
            txt = f"<span style='color:{A_COLOR}'>{fmt_val(ch, va)}</span>"
            if self.lapB:
                txt += f"  <span style='color:{B_COLOR}'>{fmt_val(ch, vb)}</span>"
            pr["val"].setHtml(txt)
            r = self._read_rows.get(ch)
            if r:
                r["a"].setText(fmt_val(ch, va))
                r["b"].setText(fmt_val(ch, vb) if self.lapB else "")
                r["d"].setText(f"{va - vb:+.1f}" if (va is not None and vb is not None) else "")
        for lap, dot in ((self.lapA, self.dotA), (self.lapB, self.dotB)):
            p = lap.gps_point(x, self._m) if lap else None
            dot.setData([p[0]], [p[1]]) if p else dot.setData([], [])
        if self.dt and self.lapA and self.lapB:
            ta = self.lapA.t_of_distance(np.array([x], float))
            tb = self.lapB.t_of_distance(np.array([x], float))
            if ta is not None and tb is not None:
                d = float((tb - ta)[0])
                self.dt["val"].setHtml(f"<span style='color:{A_COLOR if d >= 0 else B_COLOR}'>{d:+.3f} s</span>")

    def _build_readout(self):
        while self.read_grid.count():
            it = self.read_grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._read_rows = {}
        for r, ch in enumerate(self.plotted):
            cells = {}
            name = QtWidgets.QLabel(ch)
            name.setStyleSheet(f"color:{MUTED};")
            name.setFont(QtGui.QFont("Cascadia Mono", 8))
            self.read_grid.addWidget(name, r, 0)
            for col, (key, color) in enumerate((("a", A_COLOR), ("b", B_COLOR), ("d", MUTED)), start=1):
                lab = QtWidgets.QLabel("")
                lab.setStyleSheet(f"color:{color};")
                lab.setFont(QtGui.QFont("Cascadia Mono", 9))
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
            self.split.setSizes(self.cfg.get("splitter_open") or [300, 1080, 300])
            self._focus = False

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
        overlays = [pr["vline"] for pr in self.panels.values()] + [pr["val"] for pr in self.panels.values()]
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

    def closeEvent(self, e):
        if self.scan:
            self.scan.stop()
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
