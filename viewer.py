"""
MotoData -- lightweight lap browser + channel plotter for motorsport telemetry.

A desktop viewer that reads logger session files directly (no extra software).

Graph-first, menu/keyboard driven:
  * Menu bar only (no toolbars). Lap tree & channel picker are pop-up windows.
  * Selected channels persist across lap switches.
  * Mouse on the graph:  left-drag = move cursor,  right-drag = zoom to a span,
    hover = value box for that channel at the cursor x.
  * Keys:  z = Time/Distance,  h = reset zoom,  arrows = nudge cursor,
           Ctrl+L = lap picker,  Ctrl+K = channel picker.

Run:  python viewer.py  [optional path to a session-data folder]
Built on motodata.reader (parser) + motodata.catalog (units/descriptions).
"""
from __future__ import annotations
import os, sys, traceback

import numpy as np
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from motodata.reader import Lap, read_lap_header
from motodata.catalog import Catalog

# Default folder to open on launch. Set the MOTODATA_ROOT environment variable to
# your logger's session-data directory, or just use File ▸ Open Data folder… /
# Lap ▸ Choose lap… at runtime.
DEFAULT_DATA_ROOT = os.environ.get("MOTODATA_ROOT", "")
ZTX_NAMES = ("FlashData.ztx", "cableData.ztx")
DIST_CHANNELS = ("sLap", "In_xDistanceLap")
MAX_PANELS = 10
KNOWN_UNITS = ["", "km/h", "rpm", "bar", "C", "deg", "deg/s", "mm", "m", "s",
               "%", "V", "A", "Nm", "N", "G", "m/s2", "lambda", "ratio", "Hz",
               "kg", "L", "kW", "mph"]


def _ztx_in(path):
    for n in ZTX_NAMES:
        p = os.path.join(path, n)
        if os.path.exists(p):
            return p
    return None


class MotoDataViewer:
    def __init__(self, root, data_root):
        self.root = root
        self.data_root = data_root
        self.cat = Catalog()
        self.lap = None
        self.all_channels = []
        self.rates = {}
        self.selected = []
        self.node_path = {}
        self.node_ztx = {}

        # plot / cursor / interaction state
        self.xmode = tk.StringVar(value="time")
        self.dist_t = self.dist_v = None
        self.axes = []
        self._plotted = []              # channel per axis (==len(axes))
        self.cursor_lines = []
        self.hover_annots = []
        self.zoom_spans = []
        self.plot_data = {}
        self._cache = {}
        self._bg = None
        self._filter_after = None
        self._drag = None               # 'cursor' | 'zoom' | None
        self._zoom_x0 = None
        self._hover_ax = None
        self._scrub_guard = False
        self.xfull = (0.0, 1.0)         # full data x-range
        self.xmin = 0.0                 # current view x-range
        self.xmax = 1.0
        self.cursor_x = 0.0

        root.title("MotoData")
        root.geometry("1500x950")
        self._build_menu()
        self._build_main()
        self._build_lap_window()
        self._build_chan_window()
        self._populate_root()
        self._bind_keys()

    # ----------------------------------------------------------- menu bar
    def _build_menu(self):
        mb = tk.Menu(self.root)
        m_file = tk.Menu(mb, tearoff=0)
        m_file.add_command(label="Open Data folder…", command=self._choose_root)
        m_file.add_command(label="Save graph as PNG…", command=self._save_graph)
        m_file.add_separator()
        m_file.add_command(label="Exit", command=self.root.destroy)
        mb.add_cascade(label="File", menu=m_file)

        m_lap = tk.Menu(mb, tearoff=0)
        m_lap.add_command(label="Choose lap…", accelerator="Ctrl+L", command=self._show_lap_win)
        mb.add_cascade(label="Lap", menu=m_lap)

        m_ch = tk.Menu(mb, tearoff=0)
        m_ch.add_command(label="Select channels…", accelerator="Ctrl+K", command=self._show_chan_win)
        m_ch.add_command(label="Clear all channels", command=self._clear_selected)
        mb.add_cascade(label="Channels", menu=m_ch)

        m_view = tk.Menu(mb, tearoff=0)
        m_view.add_radiobutton(label="x-axis: Time [s]", value="time",
                               variable=self.xmode, command=self._on_xmode, accelerator="Z")
        m_view.add_radiobutton(label="x-axis: Distance [m]", value="dist",
                               variable=self.xmode, command=self._on_xmode, accelerator="Z")
        m_view.add_separator()
        m_view.add_command(label="Reset zoom", accelerator="H", command=self._reset_zoom)
        mb.add_cascade(label="View", menu=m_view)

        m_help = tk.Menu(mb, tearoff=0)
        m_help.add_command(label="Shortcuts & mouse…", command=self._show_help)
        mb.add_cascade(label="Help", menu=m_help)
        self.root.config(menu=mb)

    def _show_help(self):
        messagebox.showinfo("MotoData — controls",
            "Mouse on graph:\n"
            "  • Left-drag  = move the cursor\n"
            "  • Right-drag = highlight a span, release to zoom in\n"
            "  • Hover      = show that channel's value at the cursor\n\n"
            "Keys:\n"
            "  z = toggle Time / Distance x-axis\n"
            "  h = reset zoom\n"
            "  ← / → = nudge cursor\n"
            "  Ctrl+L = choose lap     Ctrl+K = choose channels")

    # -------------------------------------------------------------- main
    def _build_main(self):
        self.fig = Figure(figsize=(10, 8))
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.root)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self.canvas.mpl_connect("draw_event", self._on_draw)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        self.canvas.mpl_connect("axes_leave_event", self._on_leave)

        sc = ttk.Frame(self.root); sc.pack(side=tk.BOTTOM, fill=tk.X, padx=6, pady=2)
        ttk.Label(sc, text="cursor ◄►").pack(side=tk.LEFT)
        self.scrub = ttk.Scale(sc, orient=tk.HORIZONTAL, from_=0.0, to=1.0, command=self._on_scrub)
        self.scrub.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.readout = ttk.Label(self.root, text="Lap ▸ Choose lap…  (Ctrl+L)",
                                 anchor=tk.W, relief=tk.SUNKEN,
                                 foreground="navy", font=("Consolas", 9))
        self.readout.pack(side=tk.BOTTOM, fill=tk.X)

    def _bind_keys(self):
        def guarded(fn):
            def h(_e=None):
                w = self.root.focus_get()
                if isinstance(w, (tk.Entry, ttk.Entry, ttk.Combobox)):
                    return
                fn()
            return h
        self.root.bind("<KeyPress-z>", guarded(self._toggle_xmode))
        self.root.bind("<KeyPress-h>", guarded(self._reset_zoom))
        self.root.bind("<Left>", guarded(lambda: self._nudge(-1)))
        self.root.bind("<Right>", guarded(lambda: self._nudge(+1)))
        self.root.bind("<Control-l>", lambda e: self._show_lap_win())
        self.root.bind("<Control-k>", lambda e: self._show_chan_win())

    # --------------------------------------------------- lap pop-up window
    def _build_lap_window(self):
        self.lap_win = tk.Toplevel(self.root); self.lap_win.title("Choose lap")
        self.lap_win.geometry("440x600")
        self.lap_win.protocol("WM_DELETE_WINDOW", self.lap_win.withdraw)
        self.lap_win.withdraw()
        ttk.Label(self.lap_win, text="double-click a lap to load").pack(anchor=tk.W, padx=4, pady=2)
        fr = ttk.Frame(self.lap_win); fr.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(fr, columns=("info",), show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Track / Session / Car / Run / Lap"); self.tree.heading("info", text="Lap time")
        self.tree.column("#0", width=300); self.tree.column("info", width=80, anchor=tk.E, stretch=False)
        ysb = ttk.Scrollbar(fr, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True); ysb.pack(side=tk.LEFT, fill=tk.Y)
        self.tree.bind("<<TreeviewOpen>>", self._on_open)
        self.tree.bind("<Double-1>", self._on_lap_activate)

    def _show_lap_win(self):
        self.lap_win.deiconify(); self.lap_win.lift(); self.lap_win.focus_set()

    # ----------------------------------------------- channel pop-up window
    def _build_chan_window(self):
        self.chan_win = tk.Toplevel(self.root); self.chan_win.title("Select channels")
        self.chan_win.geometry("440x720")
        self.chan_win.protocol("WM_DELETE_WINDOW", self.chan_win.withdraw)
        self.chan_win.withdraw()
        w = self.chan_win
        fr = ttk.Frame(w); fr.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(fr, text="Filter:").pack(side=tk.LEFT)
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", self._schedule_filter)
        ttk.Entry(fr, textvariable=self.filter_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(w, text="double-click to move • right-click sets a unit",
                  foreground="gray", font=("", 7)).pack(anchor=tk.W, padx=4)

        split = ttk.PanedWindow(w, orient=tk.VERTICAL); split.pack(fill=tk.BOTH, expand=True)
        av = ttk.Frame(split)
        ttk.Label(av, text="Available", font=("", 9, "bold")).pack(anchor=tk.W)
        self.avail = ttk.Treeview(av, show="tree", selectmode="extended")
        avsb = ttk.Scrollbar(av, orient=tk.VERTICAL, command=self.avail.yview)
        self.avail.configure(yscrollcommand=avsb.set)
        self.avail.pack(side=tk.LEFT, fill=tk.BOTH, expand=True); avsb.pack(side=tk.LEFT, fill=tk.Y)
        self.avail.bind("<Double-1>", lambda e: self._add_selected(self.avail.selection()))
        self.avail.bind("<<TreeviewSelect>>", lambda e: self._show_desc_sel(self.avail))
        self.avail.bind("<Button-3>", lambda e: self._set_unit_dialog(self.avail, e))
        split.add(av, weight=3)

        se = ttk.Frame(split)
        seh = ttk.Frame(se); seh.pack(fill=tk.X)
        ttk.Label(seh, text="Selected (plotted)", font=("", 9, "bold")).pack(side=tk.LEFT)
        ttk.Button(seh, text="remove", command=lambda: self._remove_selected(self.sel.selection())).pack(side=tk.RIGHT)
        ttk.Button(seh, text="clear", command=self._clear_selected).pack(side=tk.RIGHT, padx=3)
        self.sel = ttk.Treeview(se, columns=("val",), show="tree headings", selectmode="extended")
        self.sel.heading("#0", text="channel [unit]"); self.sel.heading("val", text="@cursor")
        self.sel.column("#0", width=200); self.sel.column("val", width=80, anchor=tk.E, stretch=False)
        sesb = ttk.Scrollbar(se, orient=tk.VERTICAL, command=self.sel.yview)
        self.sel.configure(yscrollcommand=sesb.set)
        self.sel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True); sesb.pack(side=tk.LEFT, fill=tk.Y)
        self.sel.bind("<Double-1>", lambda e: self._remove_selected(self.sel.selection()))
        self.sel.bind("<<TreeviewSelect>>", lambda e: self._show_desc_sel(self.sel))
        self.sel.bind("<Button-3>", lambda e: self._set_unit_dialog(self.sel, e))
        split.add(se, weight=3)

        self.desc_box = tk.Text(w, height=4, wrap=tk.WORD, relief=tk.SUNKEN, font=("", 8))
        self.desc_box.pack(side=tk.BOTTOM, fill=tk.X); self.desc_box.configure(state=tk.DISABLED)

    def _show_chan_win(self):
        if not self.all_channels:
            messagebox.showinfo("No lap", "Load a lap first (Lap ▸ Choose lap…).")
            return
        self.chan_win.deiconify(); self.chan_win.lift(); self.chan_win.focus_set()

    # ------------------------------------------------------------- tree
    def _populate_root(self):
        self.tree.delete(*self.tree.get_children())
        self.node_path.clear(); self.node_ztx.clear()
        if not self.data_root:
            return                       # no default set; use File ▸ Open Data folder…
        if not os.path.isdir(self.data_root):
            messagebox.showwarning("Not found", "Data folder not found:\n" + self.data_root)
            return
        self._add_dir_children("", self.data_root)

    def _add_dir_children(self, parent, path):
        try:
            entries = sorted(e for e in os.listdir(path) if os.path.isdir(os.path.join(path, e)))
        except OSError:
            return
        for name in entries:
            full = os.path.join(path, name); ztx = _ztx_in(full)
            iid = self.tree.insert(parent, tk.END, text=name)
            self.node_path[iid] = full
            if ztx:
                self.node_ztx[iid] = ztx
                try:
                    info = read_lap_header(full)
                    lt = f"{info.lap_time:.3f}" if info.lap_time == info.lap_time else "-"
                    self.tree.set(iid, "info", lt)
                    self.tree.item(iid, text=f"{name}  [{info.marker or 'lap'}]")
                except Exception:
                    self.tree.set(iid, "info", "?")
            else:
                self.tree.insert(iid, tk.END, text="…")

    def _on_open(self, _e):
        iid = self.tree.focus(); kids = self.tree.get_children(iid)
        if len(kids) == 1 and self.tree.item(kids[0], "text") == "…":
            self.tree.delete(kids[0]); self._add_dir_children(iid, self.node_path[iid])

    def _on_lap_activate(self, _e):
        iid = self.tree.focus()
        if iid in self.node_ztx:
            self._load_lap(self.node_ztx[iid])
            self.lap_win.withdraw()

    # ---------------------------------------------------------- channels
    def _load_lap(self, ztx):
        try:
            lap = Lap(ztx)
            chans = lap.channels()
            rates = {c: lap.rate_snapped(c) for c in chans}
        except Exception as e:
            messagebox.showerror("Load error", f"{e}\n\n{traceback.format_exc()}"); return
        self.lap, self.all_channels, self.rates = lap, chans, rates
        self._cache.clear()
        self.dist_t = self.dist_v = None
        for cand in DIST_CHANNELS:
            if cand in chans:
                t, v = lap.channel(cand)
                self.dist_t, self.dist_v = t, self._unwrap_distance(v); break
        if self.dist_v is None and self.xmode.get() == "dist":
            self.xmode.set("time")
        self.selected = [c for c in self.selected if c in chans]   # PERSIST channels
        self.root.title(f"MotoData — {os.path.basename(os.path.dirname(ztx))}"
                        f"   {lap.lap_time:.3f}s")
        self._refresh_available(); self._refresh_selected(); self._replot()

    def _avail_text(self, c):
        u, _ = self.cat.unit(c)
        ustr = f"[{u}]" if u else ("[?]" if u is None else "")
        return f"{c}  {ustr}  {self.rates.get(c, 0):g}Hz"

    def _sel_text(self, c):
        u, _ = self.cat.unit(c)
        return f"{c} [{u}]" if u else c

    def _schedule_filter(self, *_):
        if self._filter_after:
            self.root.after_cancel(self._filter_after)
        self._filter_after = self.root.after(160, self._refresh_available)

    def _match(self, c, flt):
        return (not flt) or flt in c.lower() or flt in self.cat.description(c).lower()

    def _refresh_available(self):
        self._filter_after = None
        flt = self.filter_var.get().lower()
        self.avail.delete(*self.avail.get_children())
        for c in self.all_channels:
            if c not in self.selected and self._match(c, flt):
                self.avail.insert("", tk.END, iid=c, text=self._avail_text(c))

    def _refresh_selected(self):
        self.sel.delete(*self.sel.get_children())
        for c in self.selected:
            self.sel.insert("", tk.END, iid=c, text=self._sel_text(c), values=("",))

    def _avail_insert_sorted(self, c):
        idx = self.all_channels.index(c)
        order = {ch: i for i, ch in enumerate(self.all_channels)}
        pos = 0
        for sib in self.avail.get_children():
            if order.get(sib, 1 << 30) < idx:
                pos += 1
            else:
                break
        self.avail.insert("", pos, iid=c, text=self._avail_text(c))

    def _add_selected(self, names):
        changed = False
        for n in names:
            if n in self.all_channels and n not in self.selected:
                self.selected.append(n)
                if self.avail.exists(n):
                    self.avail.delete(n)
                self.sel.insert("", tk.END, iid=n, text=self._sel_text(n), values=("",))
                changed = True
        if changed:
            self._replot()

    def _remove_selected(self, names):
        flt = self.filter_var.get().lower()
        changed = False
        for n in list(names):
            if n in self.selected:
                self.selected.remove(n)
                if self.sel.exists(n):
                    self.sel.delete(n)
                if self._match(n, flt) and not self.avail.exists(n):
                    self._avail_insert_sorted(n)
                changed = True
        if changed:
            self._replot()

    def _clear_selected(self):
        if self.selected:
            self.selected.clear()
            self._refresh_selected(); self._refresh_available(); self._replot()

    def _show_desc_sel(self, tree):
        sel = tree.selection()
        if sel:
            self._show_desc(sel[-1])

    def _show_desc(self, c):
        u, src = self.cat.unit(c)
        ustr = u if u else ("unknown - right-click to set" if u is None else "dimensionless")
        txt = f"{c}    unit: {ustr}  ({src})\n{self.cat.description(c) or '(no description in PDF)'}"
        self.desc_box.configure(state=tk.NORMAL); self.desc_box.delete("1.0", tk.END)
        self.desc_box.insert("1.0", txt); self.desc_box.configure(state=tk.DISABLED)

    def _set_unit_dialog(self, tree, event):
        iid = tree.identify_row(event.y)
        if not iid:
            return
        self._show_desc(iid)
        cur, _ = self.cat.unit(iid)
        win = tk.Toplevel(self.root); win.title(f"Unit for {iid}")
        win.transient(self.chan_win); win.grab_set()
        ttk.Label(win, text=iid, font=("", 9, "bold")).pack(padx=10, pady=(8, 0))
        ttk.Label(win, text=self.cat.description(iid) or "(no description)",
                  wraplength=320, foreground="gray").pack(padx=10)
        var = tk.StringVar(value=(cur if cur else ""))
        cb = ttk.Combobox(win, textvariable=var, values=KNOWN_UNITS, width=18)
        cb.pack(padx=10, pady=8); cb.focus_set()

        def save():
            self.cat.set_unit(iid, var.get().strip())
            self._refresh_available(); self._refresh_selected(); self._replot()
            self._show_desc(iid); win.destroy()
        bf = ttk.Frame(win); bf.pack(pady=(0, 10))
        ttk.Button(bf, text="Save", command=save).pack(side=tk.LEFT, padx=4)
        ttk.Button(bf, text="Cancel", command=win.destroy).pack(side=tk.LEFT)
        win.bind("<Return>", lambda *_: save())

    # -------------------------------------------------------------- plot
    @staticmethod
    def _unwrap_distance(v):
        v = np.asarray(v, float).copy()
        if len(v) < 2:
            return v
        dd = np.diff(v)
        j = int(np.argmin(dd))
        if dd[j] < -0.5 * float(v.max()):
            v[:j + 1] -= v[j]
        return v

    def _toggle_xmode(self):
        if self.dist_v is None:
            return
        self.xmode.set("time" if self.xmode.get() == "dist" else "dist")
        self._on_xmode()

    def _on_xmode(self):
        if self.xmode.get() == "dist" and self.dist_v is None:
            self.xmode.set("time"); return
        if self.dist_v is not None and self.axes:
            if self.xmode.get() == "dist":
                self.cursor_x = float(np.interp(self.cursor_x, self.dist_t, self.dist_v))
            else:
                self.cursor_x = float(np.interp(self.cursor_x, self.dist_v, self.dist_t))
        self._replot()

    def _get_ty(self, ch):
        ty = self._cache.get(ch)
        if ty is None:
            ty = self.lap.channel(ch)
            self._cache[ch] = ty
        return ty

    def _x_for(self, ch, t, y):
        if self.xmode.get() == "dist" and self.dist_v is not None:
            return np.interp(t, self.dist_t, self.dist_v), y
        return t, y

    def _replot(self):
        self.fig.clear()
        self.axes = []; self._plotted = []; self.cursor_lines = []
        self.hover_annots = []; self.zoom_spans = []; self.plot_data = {}
        sel = self.selected[:MAX_PANELS]
        if not sel:
            self.canvas.draw(); self._update_readout(); return
        dist = self.xmode.get() == "dist" and self.dist_v is not None
        axes = self.fig.subplots(len(sel), 1, sharex=True, squeeze=False)[:, 0]
        xmaxs = []
        for ax, ch in zip(axes, sel):
            try:
                t, y = self._get_ty(ch)
                x, y = self._x_for(ch, t, y)
                self.plot_data[ch] = (x, y)
                stepish = ch.lower().startswith("ngear") or self.rates.get(ch, 0) <= 2
                if stepish:
                    ax.step(x, y, where="post", lw=1.0)
                else:
                    ax.plot(x, y, lw=0.8)
                xmaxs.append(x[-1])
            except Exception as e:
                ax.text(0.5, 0.5, f"{ch}: {e}", ha="center", transform=ax.transAxes)
            u, _ = self.cat.unit(ch)
            ax.set_ylabel(f"{ch} [{u}]" if u else ch, fontsize=8)
            ax.grid(True, alpha=0.3); ax.tick_params(labelsize=7)
            d = self.cat.description(ch)
            if d:
                ax.set_title(d, fontsize=7, color="dimgray", loc="left", pad=2)
            ax.text(0.997, 0.92, f"{self.rates.get(ch, 0):g} Hz", transform=ax.transAxes,
                    ha="right", va="top", fontsize=7, color="gray")
            self.axes.append(ax); self._plotted.append(ch)
        self.xfull = (0.0, max(xmaxs) if xmaxs else 1.0)
        self.xmin, self.xmax = self.xfull
        axes[0].set_xlim(self.xmin, self.xmax)
        axes[-1].set_xlabel("Lap distance [m]" if dist else "Lap time [s]")
        # use more of the canvas width (small left margin, single-line ylabels)
        self.fig.subplots_adjust(left=0.062, right=0.992, top=0.955, bottom=0.06, hspace=0.4)
        self._make_overlays()
        self.scrub.configure(from_=self.xmin, to=self.xmax)
        self._bg = None
        self.canvas.draw()
        self.scrub.set(self.cursor_x)
        self._update_readout()

    def _make_overlays(self):
        self.cursor_x = min(max(self.cursor_x, self.xmin), self.xmax)
        for ax in self.axes:
            self.cursor_lines.append(
                ax.axvline(self.cursor_x, color="crimson", lw=1.0, alpha=0.9, animated=True))
            sp = ax.axvspan(self.cursor_x, self.cursor_x, color="steelblue", alpha=0.2,
                            animated=True); sp.set_visible(False)
            self.zoom_spans.append(sp)
            an = ax.annotate("", xy=(self.cursor_x, 0), xycoords="data",
                             xytext=(5, 5), textcoords="offset points", fontsize=8,
                             bbox=dict(boxstyle="round,pad=0.2", fc="lightyellow", ec="gray"),
                             animated=True); an.set_visible(False)
            self.hover_annots.append(an)

    # ------------------------------------------------- blitting / overlay
    def _on_draw(self, _event):
        if not self.axes:
            self._bg = None
            return
        self._bg = self.canvas.copy_from_bbox(self.fig.bbox)
        self._draw_overlay(grab=False)

    def _draw_overlay(self, grab=True):
        if self._bg is None or not self.axes:
            return
        if grab:
            self.canvas.restore_region(self._bg)
        for ln in self.cursor_lines:
            ln.axes.draw_artist(ln)
        for sp in self.zoom_spans:
            if sp.get_visible():
                sp.axes.draw_artist(sp)
        for an in self.hover_annots:
            if an.get_visible():
                an.axes.draw_artist(an)
        self.canvas.blit(self.fig.bbox)

    def _move_cursor(self, x, sync=True):
        self.cursor_x = min(max(x, self.xmin), self.xmax)
        for ln in self.cursor_lines:
            ln.set_xdata([self.cursor_x, self.cursor_x])
        if self._hover_ax is not None:
            self._update_hover(self._hover_ax)
        self._update_readout()
        if sync:
            self._scrub_guard = True
            self.scrub.set(self.cursor_x)
            self._scrub_guard = False
        self._draw_overlay()

    def _update_hover(self, ax):
        if ax not in self.axes:
            return
        i = self.axes.index(ax); ch = self._plotted[i]
        for j, an in enumerate(self.hover_annots):
            an.set_visible(False)
        if ch in self.plot_data:
            x, y = self.plot_data[ch]
            v = float(np.interp(self.cursor_x, x, y))
            an = self.hover_annots[i]
            an.xy = (self.cursor_x, v)
            an.set_text(f"{ch} = {self._fmt(ch, v)}")
            an.set_visible(True)

    def _fmt(self, ch, v):
        u, _ = self.cat.unit(ch)
        return str(int(round(v))) if (u == "" and "gear" in ch.lower()) else f"{v:.1f}"

    def _update_readout(self):
        unit_x = "m" if (self.xmode.get() == "dist" and self.dist_v is not None) else "s"
        parts = [f"x={self.cursor_x:.1f}{unit_x}"]
        for ch in self._plotted:
            if ch in self.plot_data:
                x, y = self.plot_data[ch]
                v = float(np.interp(self.cursor_x, x, y))
                parts.append(f"{ch}={self._fmt(ch, v)}")
                if self.sel.exists(ch):
                    self.sel.set(ch, "val", self._fmt(ch, v))
        self.readout.config(text="   ".join(parts))

    # ----------------------------------------------------- mouse handlers
    def _on_press(self, e):
        if e.inaxes not in self.axes or e.xdata is None:
            return
        if e.button == 1:
            self._drag = "cursor"
            self._hover_ax = e.inaxes
            self._move_cursor(e.xdata)
        elif e.button == 3:
            self._drag = "zoom"; self._zoom_x0 = e.xdata
            self._update_zoom(e.xdata)

    def _on_motion(self, e):
        if self._drag == "cursor":
            if e.xdata is not None:
                if e.inaxes in self.axes:
                    self._hover_ax = e.inaxes
                self._move_cursor(e.xdata)
        elif self._drag == "zoom":
            if e.xdata is not None:
                self._update_zoom(e.xdata)
        else:
            if e.inaxes in self.axes:
                self._hover_ax = e.inaxes
                self._update_hover(e.inaxes)
                self._draw_overlay()
            elif self._hover_ax is not None:
                self._hover_ax = None
                for an in self.hover_annots:
                    an.set_visible(False)
                self._draw_overlay()

    def _on_release(self, e):
        if self._drag == "zoom":
            x1 = e.xdata if e.xdata is not None else self._zoom_x0
            x0 = self._zoom_x0
            self._drag = None; self._zoom_x0 = None
            for sp in self.zoom_spans:
                sp.set_visible(False)
            if x0 is not None and x1 is not None and abs(x1 - x0) > (self.xfull[1] - self.xfull[0]) * 0.01:
                lo, hi = sorted((x0, x1))
                self._apply_xview(lo, hi)
            else:
                self._draw_overlay()
        elif self._drag == "cursor":
            self._drag = None

    def _on_leave(self, _e):
        if self._drag is None and self._hover_ax is not None:
            self._hover_ax = None
            for an in self.hover_annots:
                an.set_visible(False)
            self._draw_overlay()

    def _update_zoom(self, x1):
        lo, hi = sorted((self._zoom_x0, x1))
        for sp in self.zoom_spans:          # axvspan -> Rectangle (mpl >=3.9)
            sp.set_visible(True)
            sp.set_x(lo); sp.set_width(hi - lo)
        self._draw_overlay()

    # ------------------------------------------------------- zoom / view
    def _apply_xview(self, lo, hi):
        self.xmin, self.xmax = lo, hi
        if self.axes:
            self.axes[0].set_xlim(lo, hi)
            self._autoscale_y(lo, hi)
        self.cursor_x = min(max(self.cursor_x, lo), hi)
        self.scrub.configure(from_=lo, to=hi)
        self._bg = None
        self.canvas.draw()
        self._scrub_guard = True; self.scrub.set(self.cursor_x); self._scrub_guard = False

    def _autoscale_y(self, lo, hi):
        for ax, ch in zip(self.axes, self._plotted):
            if ch not in self.plot_data:
                continue
            x, y = self.plot_data[ch]
            m = (x >= lo) & (x <= hi)
            if m.any():
                ylo, yhi = float(y[m].min()), float(y[m].max())
                pad = (yhi - ylo) * 0.08 or 1.0
                ax.set_ylim(ylo - pad, yhi + pad)

    def _reset_zoom(self):
        if not self.axes:
            return
        self.xmin, self.xmax = self.xfull
        self.axes[0].set_xlim(*self.xfull)
        for ax in self.axes:
            ax.relim(); ax.autoscale_view(scalex=False, scaley=True)
        self.scrub.configure(from_=self.xmin, to=self.xmax)
        self._bg = None
        self.canvas.draw()

    def _on_scrub(self, v):
        if self._scrub_guard or not self.axes:
            return
        self._move_cursor(float(v), sync=False)

    def _nudge(self, direction):
        if not self.axes:
            return
        self._move_cursor(self.cursor_x + direction * (self.xmax - self.xmin) / 500.0)

    # ------------------------------------------------------------- misc
    def _save_graph(self):
        if not self.axes:
            messagebox.showinfo("Nothing to save", "Plot some channels first."); return
        f = filedialog.asksaveasfilename(defaultextension=".png",
                                         filetypes=[("PNG image", "*.png")])
        if f:
            self.fig.savefig(f, dpi=130)
            self.readout.config(text=f"Saved {f}")

    def _choose_root(self):
        d = filedialog.askdirectory(initialdir=self.data_root, title="Select Data folder")
        if d:
            self.data_root = d; self._populate_root(); self._show_lap_win()


def main():
    data_root = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DATA_ROOT
    root = tk.Tk()
    MotoDataViewer(root, data_root)
    root.mainloop()


if __name__ == "__main__":
    main()
