"""pickers.py -- the lap and channel chooser windows.

The lap chooser is the main way in: an expandable Track/Session/Car/Run tree on
the left, the laps of the selected node as rows on the right, each row with an A
and a B checkbox so either slot can be filled in one click.
"""
from __future__ import annotations
import os, time

from PyQt6 import QtWidgets
from PyQt6.QtCore import Qt, QObject, QRunnable, pyqtSignal

from . import discovery
from .catalog import group as chan_group

COL_LAP, COL_TIME, COL_DELTA, COL_MARK, COL_A, COL_B = range(6)
LAP_CAP = 400


class _ScanSig(QObject):
    lap = pyqtSignal(int, int, object)
    done = pyqtSignal(int)


class Scan(QRunnable):
    """Parse lap headers off the UI thread; gen tags stale results for dropping."""

    def __init__(self, lap_dirs, cache, gen):
        super().__init__()
        self.lap_dirs, self.cache, self.gen = lap_dirs, cache, gen
        self.sig, self._stop = _ScanSig(), False
        self.finished = False
        self.setAutoDelete(False)

    def stop(self):
        self._stop = True

    def run(self):
        for i, ld in enumerate(self.lap_dirs):
            if self._stop:
                break
            try:
                lt, mk = discovery.lap_meta(ld, self.cache)
            except Exception:
                lt, mk = None, None
            self.sig.lap.emit(self.gen, i, (lt, mk))
        if not self._stop:
            self.sig.done.emit(self.gen)
        self.finished = True


def fmt_time(s):
    if s is None or s != s:
        return "--"
    m = int(s // 60)
    return f"{m}:{s - 60 * m:06.3f}" if m else f"{s:.3f}"


class LapPicker(QtWidgets.QWidget):
    assign = pyqtSignal(str, str)      # slot ("A"/"B"), lap dir
    unassign = pyqtSignal(str)

    def __init__(self, pool, hdr_cache, style=""):
        super().__init__()
        self.setWindowTitle("Choose laps")
        self.resize(1000, 640)
        self.setStyleSheet(style)
        self.pool, self.hdr_cache = pool, hdr_cache
        self.meta_cache = {}
        self.rows, self.scan, self._gen = [], None, 0
        self._jobs = []
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
    def set_root(self, root):
        if root == self.root:
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
        laps = discovery.find_lap_dirs(path, cap=LAP_CAP + 1)
        extra = len(laps) > LAP_CAP
        self.show_laps(laps[:LAP_CAP])
        if extra:
            self.status.setText(f"showing first {LAP_CAP} laps — open a car or run to narrow")

    # ---- lap table ----
    def show_laps(self, lap_dirs):
        if self.scan:
            self.scan.stop()
            try:
                self.scan.sig.lap.disconnect()
                self.scan.sig.done.disconnect()
            except TypeError:
                pass
        self._gen += 1
        self.rows = [{"dir": d, "lt": None, "mk": None} for d in lap_dirs]
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
        self.status.setText(f"{len(lap_dirs)} laps — reading times…")
        self.scan = Scan(lap_dirs, self.hdr_cache, self._gen)
        self.scan.sig.lap.connect(self._meta)
        self.scan.sig.done.connect(self._scan_done)
        self._jobs = [j for j in self._jobs if not j.finished] + [self.scan]
        self.pool.start(self.scan)

    def _meta(self, gen, i, meta):
        if gen != self._gen or i >= len(self.rows):
            return
        self.rows[i]["lt"], self.rows[i]["mk"] = meta
        self._relabel(i)

    def _best(self):
        ts = [r["lt"] for r in self.rows if r["lt"] and r["lt"] > 0]
        if not ts:
            return None
        med = sorted(ts)[len(ts) // 2]
        real = [t for t in ts if t >= 0.5 * med]
        return min(real) if real else None

    def _relabel(self, i):
        it = self.table.topLevelItem(i)
        if not it:
            return
        r = self.rows[i]
        best = self._best()
        self._guard = True
        it.setText(COL_TIME, fmt_time(r["lt"]))
        if best and r["lt"] and r["lt"] > 0:
            it.setText(COL_DELTA, "best" if r["lt"] <= best else f"+{r['lt'] - best:.2f}")
        it.setText(COL_MARK, r["mk"] or "")
        self._guard = False

    def _scan_done(self, gen):
        if gen != self._gen:
            return
        for i in range(len(self.rows)):
            self._relabel(i)
        self.status.setText(f"{len(self.rows)} laps")

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

    def __init__(self, catalog, style=""):
        super().__init__()
        self.setWindowTitle("Select channels")
        self.resize(420, 720)
        self.setStyleSheet(style)
        self.cat = catalog
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

    def _describe(self, item):
        if item is None or item.childCount():
            return
        c = item.text(0)
        u = self.cat.unit(c)[0]
        self.desc.setText(f"{c}  [{u if u else '-'}]\n{self.cat.description(c) or ''}")

    def _toggled(self, item, col):
        if self._guard:
            return
        c = item.text(0)
        on = item.checkState(0) == Qt.CheckState.Checked
        if on and c not in self.plotted:
            self.plotted.append(c)
        elif not on and c in self.plotted:
            self.plotted.remove(c)
        else:
            return
        self.changed.emit(list(self.plotted))

    def sync(self, plotted):
        self.plotted = list(plotted)
        self._guard = True
        for i in range(self.tree.topLevelItemCount()):
            p = self.tree.topLevelItem(i)
            for j in range(p.childCount()):
                ch = p.child(j)
                ch.setCheckState(0, Qt.CheckState.Checked if ch.text(0) in self.plotted
                                 else Qt.CheckState.Unchecked)
        self._guard = False

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
