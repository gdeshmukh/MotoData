"""pickers.py -- the lap and channel chooser windows.

The lap chooser is the main way in: an expandable Track/Session/Car/Run tree on
the left, the laps of the selected node as rows on the right, each row with an A
and a B checkbox so either slot can be filled in one click.
"""
from __future__ import annotations

import math
import os
import threading
from statistics import median

from PyQt6 import QtWidgets
from PyQt6.QtCore import Qt, QObject, QRunnable, pyqtSignal

from . import discovery
from .catalog import group as chan_group
from .reader import is_flying_marker, lap_moved

COL_LAP, COL_TIME, COL_DELTA, COL_MARK, COL_A, COL_B = range(6)
LAP_CAP = 400


class _ScanSig(QObject):
    lap = pyqtSignal(int, str, object)
    done = pyqtSignal(int, object)


def _known_distance(row):
    value = row.get("dist")
    return value is not None and math.isfinite(value) and value > 0


def lap_candidates(rows):
    timed = [r for r in rows if r["lt"] is not None and math.isfinite(r["lt"])
             and r["lt"] > 0 and is_flying_marker(r.get("mk"))]
    if not timed:
        return []
    measured = [r for r in timed if _known_distance(r)]
    fallback = timed
    measured.sort(key=lambda r: r["dist"])
    while len(measured) >= 2 and measured[-1]["dist"] > 1.25 * measured[-2]["dist"]:
        top_speed = measured[-1]["dist"] / measured[-1]["lt"]
        usual_speed = median(r["dist"] / r["lt"] for r in measured[:-1])
        if 0.7 * usual_speed <= top_speed <= 1.3 * usual_speed:
            break
        if len(measured) == 2:
            fallback = measured + [r for r in timed if not _known_distance(r)]
            measured = []
        else:
            measured.pop()
    if measured:
        distance = measured[-1]["dist"]
        complete = [r for r in measured if 0.8 * distance <= r["dist"] <= 1.2 * distance]
        typical_time = median(r["lt"] for r in complete)
        missing = [r for r in timed if not _known_distance(r)
                   and r["lt"] >= 0.8 * typical_time]
        timed = complete + missing
    else:
        longest = max(r["lt"] for r in fallback)
        timed = [r for r in fallback if r["lt"] >= 0.8 * longest]
    return sorted(timed, key=lambda r: r["lt"])


class Scan(QRunnable):
    """Parse lap headers off the UI thread; gen tags stale results for dropping."""

    def __init__(self, lap_dirs, cache, gen, auto_count=0):
        super().__init__()
        self.lap_dirs, self.cache, self.gen = list(lap_dirs), cache, gen
        self.auto_count = auto_count
        self.sig, self._stop = _ScanSig(), threading.Event()
        self.finished = False
        self.setAutoDelete(False)

    def stop(self):
        self._stop.set()

    def run(self):
        rows = []
        try:
            for ld in self.lap_dirs:
                if self._stop.is_set():
                    return
                try:
                    lt, mk, dist = discovery.lap_header_meta(ld, self.cache)
                except (OSError, TypeError, ValueError):
                    lt, mk, dist = None, None, None
                row = {"dir": ld, "lt": lt, "mk": mk, "dist": dist}
                rows.append(row)
                self.sig.lap.emit(self.gen, ld, (lt, mk, dist))
            selected = []
            if self.auto_count:
                for row in lap_candidates(rows):
                    if self._stop.is_set() or len(selected) >= self.auto_count:
                        break
                    ztx = discovery.ztx_in(row["dir"])
                    if lap_moved(ztx, row["lt"], lap_distance=row["dist"]):
                        selected.append(row["dir"])
            if not self._stop.is_set():
                self.sig.done.emit(self.gen, selected)
        finally:
            self.finished = True


def fmt_time(s):
    if s is None or not math.isfinite(s):
        return "--"
    m = int(s // 60)
    return f"{m}:{s - 60 * m:06.3f}" if m else f"{s:.3f}"


class LapPicker(QtWidgets.QWidget):
    assign = pyqtSignal(str, str)      # slot ("A"/"B"), lap dir
    unassign = pyqtSignal(str)
    laps_requested = pyqtSignal(object, bool)

    def __init__(self, style=""):
        super().__init__()
        self.setWindowTitle("Choose laps")
        self.resize(1000, 640)
        self.setStyleSheet(style)
        self.meta_cache = {}
        self.rows, self._row_index = [], {}
        self._lap_index = []
        self._guard = False
        self.root = ""
        self._slot_a = self._slot_b = None

        split = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Track / Session / Car / Run"])
        self.tree.itemExpanded.connect(self._expand)
        self.tree.currentItemChanged.connect(lambda cur, _p: self._pick_node(cur))
        split.addWidget(self.tree)

        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.table = QtWidgets.QTreeWidget()
        self.table.setHeaderLabels(["Lap", "Time", "vs best", "", "A", "B"])
        self.table.setRootIsDecorated(False)
        self.table.setUniformRowHeights(True)
        for col, wdt in ((COL_LAP, 90), (COL_TIME, 90), (COL_DELTA, 80),
                         (COL_MARK, 60), (COL_A, 40), (COL_B, 40)):
            self.table.setColumnWidth(col, wdt)
        self.table.itemChanged.connect(self._toggled)
        rv.addWidget(self.table, 1)
        self.info = QtWidgets.QLabel("")
        rv.addWidget(self.info)
        split.addWidget(right)
        split.setSizes([330, 670])

        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(split, 1)
        bar = QtWidgets.QHBoxLayout()
        self.status = QtWidgets.QLabel("")
        bar.addWidget(self.status, 1)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.hide)
        bar.addWidget(close)
        lay.addLayout(bar)

    # ---- tree ----
    def set_index(self, lap_dirs):
        self._lap_index = list(lap_dirs)

    def set_root(self, root, refresh=False):
        if root == self.root and not refresh:
            return
        self.root = root
        self.tree.blockSignals(True)         # filling the tree must not pick a node
        self.tree.clear()
        for d in discovery.subdirs(root):
            self.tree.addTopLevelItem(self._node(d))
        self.tree.setCurrentItem(None)
        self.tree.blockSignals(False)

    def _node(self, path):
        it = QtWidgets.QTreeWidgetItem([os.path.basename(path) or path])
        it.setData(0, Qt.ItemDataRole.UserRole, path)
        if not discovery.is_lap_leaf(path) and discovery.subdirs(path):
            it.addChild(QtWidgets.QTreeWidgetItem(["…"]))
        return it

    def _expand(self, item):
        if item.childCount() == 1 and item.child(0).text(0) == "…":
            item.takeChildren()
            for d in discovery.subdirs(item.data(0, Qt.ItemDataRole.UserRole)):
                if not discovery.is_lap_leaf(d):
                    item.addChild(self._node(d))

    def _pick_node(self, item):
        if item is None:
            return
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if not path:
            return
        norm = os.path.normcase(os.path.abspath(path)).rstrip("\\/")
        prefix = norm + os.sep
        laps = [d for d in self._lap_index
                if (key := os.path.normcase(os.path.abspath(d))) == norm or key.startswith(prefix)]
        extra = len(laps) > LAP_CAP
        self.laps_requested.emit(laps[:LAP_CAP], extra)

    # ---- lap table ----
    def set_laps(self, lap_dirs):
        self.rows = [{"dir": d, "lt": None, "mk": None, "dist": None} for d in lap_dirs]
        self._row_index = {d: i for i, d in enumerate(lap_dirs)}
        self._guard = True
        self.table.clear()
        for d in lap_dirs:
            it = QtWidgets.QTreeWidgetItem([os.path.basename(d).replace("Lap_", ""), "--", "", "", "", ""])
            it.setData(0, Qt.ItemDataRole.UserRole, d)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(COL_A, Qt.CheckState.Unchecked)
            it.setCheckState(COL_B, Qt.CheckState.Unchecked)
            self.table.addTopLevelItem(it)
        self._guard = False
        self._apply_slots()
        self.info.setText(discovery.describe(lap_dirs[0], self.meta_cache) if lap_dirs else "")
        self.status.setText(f"{len(lap_dirs)} laps" + (" — reading times…" if lap_dirs else ""))

    def set_meta(self, lap_dir, meta):
        i = self._row_index.get(lap_dir)
        if i is None:
            return
        self.rows[i]["lt"], self.rows[i]["mk"], self.rows[i]["dist"] = meta
        self._relabel(i)

    def _best(self):
        rows = lap_candidates(self.rows)
        return rows[0]["lt"] if rows else None

    def _relabel(self, i, best=None):
        it = self.table.topLevelItem(i)
        if not it:
            return
        r = self.rows[i]
        self._guard = True
        it.setText(COL_TIME, fmt_time(r["lt"]))
        it.setText(COL_DELTA, "")
        if (best and r["lt"] and r["lt"] >= best
                and is_flying_marker(r["mk"])):
            it.setText(COL_DELTA, "best" if r["lt"] <= best else f"+{r['lt'] - best:.2f}")
        it.setText(COL_MARK, r["mk"] or "")
        self._guard = False

    def scan_finished(self, extra=False):
        best = self._best()
        for i in range(len(self.rows)):
            self._relabel(i, best)
        text = f"showing first {LAP_CAP} laps — narrow the selection" if extra else f"{len(self.rows)} laps"
        self.status.setText(text)

    def _toggled(self, item, col):
        if self._guard or col not in (COL_A, COL_B):
            return
        slot = "A" if col == COL_A else "B"
        if item.checkState(col) == Qt.CheckState.Checked:
            self._guard = True
            for i in range(self.table.topLevelItemCount()):
                other = self.table.topLevelItem(i)
                if other is not item:
                    other.setCheckState(col, Qt.CheckState.Unchecked)
            self._guard = False
            d = item.data(0, Qt.ItemDataRole.UserRole)
            self.info.setText(discovery.describe(d, self.meta_cache))
            self.assign.emit(slot, d)
        else:
            self.unassign.emit(slot)

    def set_slots(self, dir_a, dir_b):
        """Reflect slots chosen elsewhere (auto-assign, swap, clear)."""
        self._slot_a, self._slot_b = dir_a, dir_b
        self._apply_slots()

    def _apply_slots(self):
        self._guard = True
        for i in range(self.table.topLevelItemCount()):
            it = self.table.topLevelItem(i)
            d = it.data(0, Qt.ItemDataRole.UserRole)
            it.setCheckState(COL_A, Qt.CheckState.Checked if d == self._slot_a
                             else Qt.CheckState.Unchecked)
            it.setCheckState(COL_B, Qt.CheckState.Checked if d == self._slot_b
                             else Qt.CheckState.Unchecked)
        self._guard = False


class ChannelPicker(QtWidgets.QWidget):
    changed = pyqtSignal(list)

    def __init__(self, catalog, style="", limit=12):
        super().__init__()
        self.setWindowTitle("Select channels")
        self.resize(420, 720)
        self.setStyleSheet(style)
        self.cat = catalog
        self.limit = limit
        self.plotted = []
        self._guard = False

        v = QtWidgets.QVBoxLayout(self)
        self.filter = QtWidgets.QLineEdit()
        self.filter.setPlaceholderText("filter name or description")
        self.filter.textChanged.connect(self._filter)
        v.addWidget(self.filter)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemChanged.connect(self._toggled)
        self.tree.currentItemChanged.connect(lambda cur, _p: self._describe(cur))
        v.addWidget(self.tree, 1)
        self.desc = QtWidgets.QLabel("")
        self.desc.setWordWrap(True)
        self.desc.setMinimumHeight(46)
        v.addWidget(self.desc)

    def build(self, channels, plotted):
        self.plotted = list(plotted)
        self._guard = True
        self.tree.clear()
        self.desc.clear()
        groups = {}
        for c in sorted(channels):
            groups.setdefault(chan_group(c), []).append(c)
        for g in sorted(groups):
            parent = QtWidgets.QTreeWidgetItem([g])
            parent.setFlags(Qt.ItemFlag.ItemIsEnabled)
            for c in groups[g]:
                it = QtWidgets.QTreeWidgetItem([c])
                it.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
                it.setCheckState(0, Qt.CheckState.Checked if c in self.plotted
                                 else Qt.CheckState.Unchecked)
                parent.addChild(it)
            self.tree.addTopLevelItem(parent)
            if any(c in self.plotted for c in groups[g]):
                parent.setExpanded(True)
        self._guard = False
        self._filter(self.filter.text())

    def _describe(self, item):
        if item is None or item.childCount():
            return
        c = item.text(0)
        u = self.cat.unit(c)[0]
        unit = "unknown" if u is None else (u or "dimensionless")
        self.desc.setText(f"{c}  [{unit}]\n{self.cat.description(c) or ''}")

    def _toggled(self, item, col):
        if self._guard:
            return
        c = item.text(0)
        on = item.checkState(0) == Qt.CheckState.Checked
        if on and c not in self.plotted and len(self.plotted) >= self.limit:
            self._guard = True
            item.setCheckState(0, Qt.CheckState.Unchecked)
            self._guard = False
            self.desc.setText(f"Select up to {self.limit} channels.")
            return
        if on and c not in self.plotted:
            self.plotted.append(c)
        elif not on and c in self.plotted:
            self.plotted.remove(c)
        else:
            return
        self.changed.emit(list(self.plotted))

    def _filter(self, text):
        t = text.lower().strip()
        for i in range(self.tree.topLevelItemCount()):
            p = self.tree.topLevelItem(i)
            shown = 0
            for j in range(p.childCount()):
                ch = p.child(j)
                match = (not t) or t in ch.text(0).lower() or t in self.cat.description(ch.text(0)).lower()
                ch.setHidden(not match)
                shown += match
            p.setHidden(shown == 0)
            if t and shown:
                p.setExpanded(True)
