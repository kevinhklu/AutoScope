"""
gui.py — CustomTkinter + matplotlib front end for AutoScope.

Layout: a waveform plot on the left; a right panel with an always-visible
connection indicator + Connect/Disconnect, tabbed settings (Setup / Measure /
I2C), and a results area. Last-used settings persist to gui_settings.json.
VISA work runs on a worker thread.

Run:  python gui.py
"""

from __future__ import annotations

import json
import os
import queue
import threading
import traceback
import tkinter as tk
from typing import Callable, Optional

import customtkinter as ctk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

from config import DEFAULT_LIMITS, Config
from i2c import I2CAnalysisResult, run_i2c_analysis
from measurements import read_measurement
from results_log import ResultLogger, resolve_log_path
from scope_interface import NoBusActivity, Scope, find_scope_resource


_BASIC_MEAS = {
    "Vpp": "PK2pk",
    "Vmax": "MAXimum",
    "Vmin": "MINImum",
    "Vmean": "MEAN",
    "Frequency": "FREQuency",
    "Period": "PERiod",
    "Duty cycle": "PDUty",
}

_CHANNELS = ("CH1", "CH2", "CH3", "CH4")

# How to combine a measurement across N acquisitions. Peak-type measurements
# take the worst-case extreme (matches the scope's free-running badge, which is
# what you want for broadband/emissions capture); the rest average.
_AGG = {"Vpp": max, "Vmax": max, "Vmin": min}
_AGG_NAME = {"Vpp": "max", "Vmax": "max", "Vmin": "min"}

# limit_key, label, which bound ("min" or "max")
_LIMIT_ROWS = (
    ("tscl_fall", "Tscl fall max (ns)", "max"),
    ("scl_high", "Scl high min (ns)", "min"),
    ("scl_low", "Scl low min (ns)", "min"),
    ("thd_dat", "tHD;DAT min (ns)", "min"),
    ("tsu_dat", "tSU;DAT min (ns)", "min"),
)

_GREEN = "#2ecc71"
_RED = "#e74c3c"
_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "gui_settings.json")


def _aggregate_measurement(label, vals):
    fn = _AGG.get(label)
    return fn(vals) if fn is not None else sum(vals) / len(vals)


class AutoScopeApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("AutoScope")
        self.geometry("1280x800")
        self.minsize(1024, 700)

        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.scope: Optional[Scope] = None
        self._busy = False
        self._closing = False
        self._ui_q: queue.Queue = queue.Queue()
        self._action_btns: list[ctk.CTkButton] = []
        self._limit_entries: dict[str, tuple] = {}

        self._build()
        self._load_settings()
        self._set_status("Enter settings, then Connect.")
        self.after(100, self._drain_ui_queue)
        self.after(300, self._autodetect_if_empty)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # --- layout ----------------------------------------------------------
    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        main.grid_columnconfigure(0, weight=1)
        main.grid_columnconfigure(1, weight=0)
        main.grid_rowconfigure(0, weight=1)

        # --- plot (left) ---
        plot_frame = tk.Frame(main)
        plot_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        plot_frame.grid_rowconfigure(0, weight=1)
        plot_frame.grid_columnconfigure(0, weight=1)

        self.fig = Figure(figsize=(7, 5), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Volts")
        self.ax.set_title("No capture yet")
        self.ax.grid(True, alpha=0.3)
        self.fig.tight_layout()

        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
        self.canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        toolbar_host = tk.Frame(plot_frame)
        toolbar_host.grid(row=1, column=0, sticky="ew")
        self.toolbar = NavigationToolbar2Tk(self.canvas, toolbar_host)
        self.toolbar.update()

        # --- side panel (right) ---
        side = ctk.CTkFrame(main, width=400)
        side.grid(row=0, column=1, sticky="ns")
        side.grid_propagate(False)

        ctk.CTkLabel(side, text="AutoScope",
                     font=ctk.CTkFont(size=18, weight="bold")
                     ).pack(anchor="w", padx=12, pady=(12, 2))

        # connection indicator (always visible)
        conn_row = ctk.CTkFrame(side, fg_color="transparent")
        conn_row.pack(fill="x", padx=12, pady=(0, 2))
        self.conn_dot = ctk.CTkLabel(conn_row, text="●", text_color=_RED,
                                     font=ctk.CTkFont(size=16))
        self.conn_dot.pack(side="left", padx=(0, 6))
        self.conn_label = ctk.CTkLabel(conn_row, text="Disconnected", anchor="w")
        self.conn_label.pack(side="left")

        cbtns = ctk.CTkFrame(side, fg_color="transparent")
        cbtns.pack(fill="x", padx=12, pady=(2, 0))
        self.btn_connect = ctk.CTkButton(cbtns, text="Connect",
                                         command=self._on_connect)
        self.btn_connect.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.btn_disconnect = ctk.CTkButton(cbtns, text="Disconnect",
                                            command=self._on_disconnect,
                                            state="disabled")
        self.btn_disconnect.pack(side="left", expand=True, fill="x", padx=(4, 0))

        self.conn_error = ctk.CTkLabel(side, text="", text_color=_RED,
                                       wraplength=360, anchor="w", justify="left")
        self.conn_error.pack(fill="x", padx=12)

        self.status = ctk.CTkLabel(side, text="", wraplength=360, justify="left",
                                   anchor="w", text_color="gray")
        self.status.pack(fill="x", padx=12, pady=(2, 4))

        # tabbed settings
        self.tabs = ctk.CTkTabview(side, height=360)
        self.tabs.pack(fill="x", padx=8, pady=(0, 4))
        for name in ("Setup", "Measure", "I2C"):
            self.tabs.add(name)
        self._build_setup_tab(self.tabs.tab("Setup"))
        self._build_measure_tab(self.tabs.tab("Measure"))
        self._build_i2c_tab(self.tabs.tab("I2C"))

        # results (expands to fill remaining space)
        ctk.CTkLabel(side, text="Results", anchor="w").pack(
            fill="x", padx=12, pady=(6, 0))
        self.results = ctk.CTkTextbox(side, height=200, wrap="word",
                                      font=ctk.CTkFont(family="Consolas", size=11))
        self.results.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        self.results.insert("1.0", "(no results yet)\n")
        self.results.configure(state="disabled")

    @staticmethod
    def _tab_scroll(tab) -> ctk.CTkScrollableFrame:
        sf = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        sf.pack(fill="both", expand=True)
        return sf

    def _labeled_entry(self, parent, label: str, default: str = "") -> ctk.CTkEntry:
        ctk.CTkLabel(parent, text=label, anchor="w").pack(fill="x", pady=(8, 0))
        entry = ctk.CTkEntry(parent)
        entry.pack(fill="x", pady=(2, 0))
        if default:
            entry.insert(0, default)
        return entry

    def _build_setup_tab(self, tab):
        p = self._tab_scroll(tab)
        ctk.CTkLabel(p, text="Scope", font=ctk.CTkFont(weight="bold")
                     ).pack(anchor="w", pady=(4, 0))
        self.resource_entry = self._labeled_entry(p, "VISA resource", "")
        self.btn_detect = ctk.CTkButton(p, text="Auto-detect scope",
                                        command=self._on_detect)
        self.btn_detect.pack(fill="x", pady=(6, 0))

        ctk.CTkLabel(p, text="Log file", font=ctk.CTkFont(weight="bold")
                     ).pack(anchor="w", pady=(14, 0))
        self.csv_entry = self._labeled_entry(
            p, "File name (.xlsx, saved under results/)", "measurements.xlsx")
        ctk.CTkLabel(p, text="New file -> header row. Existing -> append. "
                     "Close it in Excel before running.",
                     font=ctk.CTkFont(size=11), text_color="gray",
                     anchor="w", justify="left", wraplength=320).pack(fill="x")

    def _build_measure_tab(self, tab):
        p = self._tab_scroll(tab)
        ctk.CTkLabel(p, text="Channel", anchor="w").pack(fill="x", pady=(6, 0))
        self.channel_var = ctk.StringVar(value="CH1")
        ctk.CTkOptionMenu(p, variable=self.channel_var,
                          values=list(_CHANNELS)).pack(fill="x", pady=2)

        self.n_acq_entry = self._labeled_entry(
            p, "Acquisitions (N) — reports peak over N", "16")

        ctk.CTkLabel(p, text="Measurements", anchor="w").pack(fill="x", pady=(10, 0))
        self.meas_vars: dict[str, ctk.BooleanVar] = {}
        for label in _BASIC_MEAS:
            var = ctk.BooleanVar(value=True)
            self.meas_vars[label] = var
            ctk.CTkCheckBox(p, text=label, variable=var).pack(anchor="w", pady=1)

        self.measure_error = ctk.CTkLabel(p, text="", text_color=_RED,
                                          wraplength=320, anchor="w", justify="left")
        self.measure_error.pack(fill="x", pady=(6, 0))
        self.btn_acquire = self._action_button(p, "Acquire & Measure",
                                               self._on_acquire)

    def _build_i2c_tab(self, tab):
        p = self._tab_scroll(tab)
        ctk.CTkLabel(p, text="SCL / SDA", anchor="w").pack(fill="x", pady=(6, 0))
        row = ctk.CTkFrame(p, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ctk.CTkLabel(row, text="SCL").pack(side="left")
        self.scl_var = ctk.StringVar(value="CH1")
        ctk.CTkOptionMenu(row, variable=self.scl_var, values=list(_CHANNELS),
                          width=90).pack(side="left", padx=(6, 12))
        ctk.CTkLabel(row, text="SDA").pack(side="left")
        self.sda_var = ctk.StringVar(value="CH2")
        ctk.CTkOptionMenu(row, variable=self.sda_var, values=list(_CHANNELS),
                          width=90).pack(side="left", padx=(6, 0))

        self.vdd_entry = self._labeled_entry(p, "Vdd (V)", "1.8")
        prow = ctk.CTkFrame(p, fg_color="transparent")
        prow.pack(fill="x", pady=(4, 0))
        ctk.CTkLabel(prow, text="High %").pack(side="left")
        self.high_pct_entry = ctk.CTkEntry(prow, width=60)
        self.high_pct_entry.pack(side="left", padx=(6, 16))
        self.high_pct_entry.insert(0, "70")
        ctk.CTkLabel(prow, text="Low %").pack(side="left")
        self.low_pct_entry = ctk.CTkEntry(prow, width=60)
        self.low_pct_entry.pack(side="left", padx=(6, 0))
        self.low_pct_entry.insert(0, "30")
        self.probe_delay_entry = self._labeled_entry(
            p, "Probe countdown (s, 0 = off)", "5")

        ctk.CTkLabel(p, text="Limits (ns, blank = no check)", anchor="w"
                     ).pack(fill="x", pady=(10, 0))
        for key, label, bound in _LIMIT_ROWS:
            r = ctk.CTkFrame(p, fg_color="transparent")
            r.pack(fill="x", pady=2)
            ctk.CTkLabel(r, text=label, width=150, anchor="w").pack(side="left")
            entry = ctk.CTkEntry(r, width=80)
            entry.pack(side="right")
            default = DEFAULT_LIMITS.get(key, {}).get(f"{bound}_ns")
            if default is not None:
                entry.insert(0, str(int(default) if default == int(default)
                                    else default))
            self._limit_entries[key] = (entry, bound)

        self.i2c_error = ctk.CTkLabel(p, text="", text_color=_RED,
                                      wraplength=320, anchor="w", justify="left")
        self.i2c_error.pack(fill="x", pady=(6, 0))
        self.btn_i2c = self._action_button(p, "I2C Capture", self._on_i2c)

    def _action_button(self, parent, text, command) -> ctk.CTkButton:
        btn = ctk.CTkButton(parent, text=text, command=command, state="disabled")
        btn.pack(fill="x", pady=(3, 8))
        self._action_btns.append(btn)
        return btn

    # --- settings persistence -------------------------------------------
    def _collect_settings(self) -> dict:
        return {
            "resource": self.resource_entry.get(),
            "log": self.csv_entry.get(),
            "channel": self.channel_var.get(),
            "n_acq": self.n_acq_entry.get(),
            "vdd": self.vdd_entry.get(),
            "high_pct": self.high_pct_entry.get(),
            "low_pct": self.low_pct_entry.get(),
            "probe_delay": self.probe_delay_entry.get(),
            "scl": self.scl_var.get(),
            "sda": self.sda_var.get(),
            "limits": {k: e.get() for k, (e, _b) in self._limit_entries.items()},
            "measurements": {k: v.get() for k, v in self.meas_vars.items()},
        }

    def _apply_settings(self, s: dict):
        def set_entry(entry, val):
            if val is None:
                return
            prev = entry.cget("state")
            entry.configure(state="normal")
            entry.delete(0, "end")
            entry.insert(0, str(val))
            entry.configure(state=prev)

        set_entry(self.resource_entry, s.get("resource"))
        set_entry(self.csv_entry, s.get("log"))
        set_entry(self.n_acq_entry, s.get("n_acq"))
        set_entry(self.vdd_entry, s.get("vdd"))
        set_entry(self.high_pct_entry, s.get("high_pct"))
        set_entry(self.low_pct_entry, s.get("low_pct"))
        set_entry(self.probe_delay_entry, s.get("probe_delay"))
        if s.get("channel") in _CHANNELS:
            self.channel_var.set(s["channel"])
        if s.get("scl") in _CHANNELS:
            self.scl_var.set(s["scl"])
        if s.get("sda") in _CHANNELS:
            self.sda_var.set(s["sda"])
        for key, val in (s.get("limits") or {}).items():
            if key in self._limit_entries:
                set_entry(self._limit_entries[key][0], val)
        for label, val in (s.get("measurements") or {}).items():
            if label in self.meas_vars:
                self.meas_vars[label].set(bool(val))

    def _load_settings(self):
        try:
            with open(_SETTINGS_PATH) as f:
                s = json.load(f)
        except (OSError, ValueError):
            return
        try:
            self._apply_settings(s)
        except Exception:
            pass   # never let a bad settings file break startup

    def _save_settings(self):
        try:
            with open(_SETTINGS_PATH, "w") as f:
                json.dump(self._collect_settings(), f, indent=2)
        except OSError:
            pass

    # --- settings parsing ------------------------------------------------
    @staticmethod
    def _parse_float(text: str, name: str, default: float | None = None) -> float:
        text = text.strip()
        if not text:
            if default is not None:
                return default
            raise ValueError(f"{name} is required.")
        try:
            return float(text)
        except ValueError as e:
            raise ValueError(f"{name} must be a number.") from e

    @staticmethod
    def _parse_optional_float(text: str) -> float | None:
        text = text.strip()
        if not text:
            return None
        return float(text)

    def _limits_from_gui(self) -> dict:
        limits = {}
        for key, (entry, bound) in self._limit_entries.items():
            try:
                val = self._parse_optional_float(entry.get())
            except ValueError as e:
                raise ValueError(f"{key} limit must be a number or blank.") from e
            if val is not None:
                limits[key] = {f"{bound}_ns": val}
        return limits

    def _settings_from_gui(self) -> Config:
        resource = self.resource_entry.get().strip()
        if not resource:
            raise ValueError("Enter the scope VISA resource string.")
        vdd = self._parse_float(self.vdd_entry.get(), "Vdd")
        if vdd <= 0:
            raise ValueError("Vdd must be positive.")
        high_pct = self._parse_float(self.high_pct_entry.get(), "High %")
        low_pct = self._parse_float(self.low_pct_entry.get(), "Low %")
        probe_delay_s = self._parse_float(
            self.probe_delay_entry.get(), "Probe countdown", default=0)
        if probe_delay_s < 0:
            raise ValueError("Probe countdown cannot be negative.")
        return Config(
            resource=resource,
            scl=self.scl_var.get(),
            sda=self.sda_var.get(),
            vdd=vdd,
            probe_delay_s=probe_delay_s,
            high_pct=high_pct,
            low_pct=low_pct,
            limits=self._limits_from_gui(),
        )

    def _logger_from_gui(self) -> ResultLogger:
        path = resolve_log_path(self.csv_entry.get())
        return ResultLogger(path)

    def _set_resource_locked(self, locked: bool):
        self.resource_entry.configure(state="disabled" if locked else "normal")

    # --- small ui helpers ------------------------------------------------
    def _show_error(self, label, msg):
        label.configure(text=msg)

    def _clear_error(self, label):
        label.configure(text="")

    def _set_connected(self, connected: bool):
        self.conn_dot.configure(text_color=_GREEN if connected else _RED)
        self.conn_label.configure(
            text="Connected" if connected else "Disconnected")

    # --- auto-detect -----------------------------------------------------
    def _autodetect_if_empty(self):
        if not self.resource_entry.get().strip():
            self._on_detect()

    def _on_detect(self):
        """Fill the resource field with the first connected Tektronix USB scope."""
        try:
            res = find_scope_resource()
        except Exception as e:
            self._set_status(f"Auto-detect unavailable: {e}")
            return
        if res:
            self.resource_entry.delete(0, "end")
            self.resource_entry.insert(0, res)
            self._clear_error(self.conn_error)
            self._set_status(f"Detected scope: {res}")
        else:
            self._set_status("No scope found — check USB/power, or type the "
                             "resource string manually.")

    # --- connection ------------------------------------------------------
    def _on_connect(self):
        if self.scope is not None:
            return
        resource = self.resource_entry.get().strip()
        if not resource:
            self._show_error(self.conn_error,
                             "Enter or auto-detect the VISA resource.")
            return
        self._clear_error(self.conn_error)
        self._run_async(lambda: self._connect_worker(resource),
                        on_done=self._after_connect)

    def _connect_worker(self, resource: str):
        self._post_status("Connecting...")
        s = Scope(resource)
        s.__enter__()
        try:
            idn = s.idn()
            return s, idn
        except Exception:
            try:
                s.__exit__(None, None, None)
            except Exception:
                pass
            raise

    def _after_connect(self, result, err):
        if err:
            self._show_error(self.conn_error, f"Connect failed: {err}")
            return
        s, idn = result
        if self._closing:
            try:
                s.__exit__(None, None, None)
            except Exception:
                pass
            return
        self.scope = s
        self._clear_error(self.conn_error)
        self.btn_connect.configure(state="disabled")
        self.btn_disconnect.configure(state="normal")
        self._set_resource_locked(True)
        self._set_action_enabled(True)
        self._set_connected(True)
        self._set_status(f"Connected: {idn}")
        self._save_settings()

    def _on_disconnect(self):
        self._run_async(self._disconnect_worker, on_done=self._after_disconnect)

    def _disconnect_worker(self):
        self._close_scope()
        return None

    def _after_disconnect(self, _result, err):
        if err:
            self._set_status(f"Disconnect error: {err}")
        else:
            self._set_status("Disconnected.")
        self.btn_connect.configure(state="normal")
        self.btn_disconnect.configure(state="disabled")
        self._set_resource_locked(False)
        self._set_action_enabled(False)
        self._set_connected(False)

    def _close_scope(self):
        if self.scope is None:
            return
        try:
            self.scope.__exit__(None, None, None)
        except Exception:
            pass
        self.scope = None

    def _on_close(self):
        self._closing = True
        if self._busy:
            self._set_status("Finishing scope operation before exit...")
            self.after(100, self._on_close)
            return
        self._save_settings()
        self._close_scope()
        self.destroy()

    # --- actions ---------------------------------------------------------
    def _selected_measurements(self) -> list[tuple[str, str]]:
        return [(label, scpi) for label, scpi in _BASIC_MEAS.items()
                if self.meas_vars[label].get()]

    def _on_acquire(self):
        ch = self.channel_var.get()
        selected = self._selected_measurements()
        try:
            n_acq = int(self._parse_float(self.n_acq_entry.get(),
                                          "Acquisitions", default=1))
            if n_acq < 1:
                raise ValueError("Acquisitions (N) must be at least 1.")
            logger = self._logger_from_gui()
        except ValueError as e:
            self._show_error(self.measure_error, str(e))
            return
        self._clear_error(self.measure_error)
        self._run_async(lambda: self._acquire_worker(ch, selected, logger, n_acq),
                        on_done=self._after_acquire)

    def _restore_scope_live(self):
        if self.scope is None:
            return
        try:
            self.scope.restore_live()
        except Exception:
            pass

    def _acquire_worker(self, channel: str, selected: list[tuple[str, str]],
                        logger: ResultLogger, n_acq: int):
        assert self.scope is not None
        try:
            labels = ", ".join(l for l, _ in selected) or "(plot only)"
            self.scope.write("MEASUrement:REFLevel:METHod PERCent")
            self.scope.ensure_channel_on(channel)

            # Take N acquisitions, collecting each measurement's valid values.
            collected = {label: [] for label, _ in selected}
            units = {label: "" for label, _ in selected}
            for i in range(n_acq):
                self._post_status(f"Acquiring {channel} — {labels} "
                                  f"({i + 1}/{n_acq})...")
                self.scope.single_acquisition()
                for label, scpi in selected:
                    m = read_measurement(self.scope, scpi, channel, retries=3)
                    if m.valid:
                        collected[label].append(m.value)
                    units[label] = m.units

            # Aggregate: peak-type measurements keep the worst-case extreme.
            results = []
            log_rows = []
            for label, _scpi in selected:
                vals = collected[label]
                u = units[label]
                if not vals:
                    note = f"no valid reading in {n_acq} acq"
                    results.append((label, None, u, note))
                    log_rows.append({"measurement": f"{label} ({channel})",
                                     "value": None, "units": u,
                                     "status": "INVALID", "note": note})
                    continue
                agg = _aggregate_measurement(label, vals)
                note = "" if n_acq == 1 else \
                    f"{_AGG_NAME.get(label, 'mean')} of {len(vals)}/{n_acq} acq"
                results.append((label, agg, u, note))
                log_rows.append({"measurement": f"{label} ({channel})",
                                 "value": agg, "units": u,
                                 "status": "OK", "note": note})

            # One capture -> one merged group of rows in the workbook.
            if log_rows:
                logger.log_capture(log_rows)

            times, volts = self.scope.read_waveform(channel)
            return {"channel": channel, "times": times, "volts": volts,
                    "results": results, "logger": logger, "n_acq": n_acq}
        finally:
            self._restore_scope_live()

    def _after_acquire(self, result, err):
        if err:
            self._show_error(self.measure_error, f"Acquire failed: {err}")
            return
        ch = result["channel"]
        logger = result["logger"]
        n_acq = result["n_acq"]
        self._plot_single(result["times"], result["volts"], ch,
                          title=f"{ch} acquisition")
        lines = [f"Acquire {ch} — {n_acq} acquisition(s), "
                 f"{len(result['times'])} samples",
                 f"Log: {logger.path} (run {logger.run_id})", ""]
        if not result["results"]:
            lines.append("  (no measurements selected)")
        for label, value, units, note in result["results"]:
            if value is None:
                lines.append(f"  {label}: INVALID — {note}")
            else:
                suffix = f"  ({note})" if note else ""
                lines.append(f"  {label}: {value:.6g} {units}{suffix}")
        self._set_results("\n".join(lines) + "\n")
        self._set_status(f"Acquired {ch} ({n_acq}×) — logged to {logger.path}")
        self._save_settings()

    def _on_i2c(self):
        try:
            cfg = self._settings_from_gui()
            logger = self._logger_from_gui()
        except ValueError as e:
            self._show_error(self.i2c_error, str(e))
            return
        scl, sda = self.scl_var.get(), self.sda_var.get()
        if scl == sda:
            self._show_error(self.i2c_error, f"SCL and SDA must differ (both {scl}).")
            return
        self._clear_error(self.i2c_error)
        cfg.scl, cfg.sda = scl, sda
        self._run_async(lambda: self._i2c_worker(cfg, logger),
                        on_done=self._after_i2c)

    def _i2c_worker(self, cfg: Config, logger: ResultLogger):
        assert self.scope is not None
        try:
            result = run_i2c_analysis(
                self.scope, cfg, logger,
                on_status=self._post_status,
                do_probe_delay=True,
            )
            return result, logger
        finally:
            self._restore_scope_live()

    def _after_i2c(self, payload, err):
        if err:
            if isinstance(err, NoBusActivity):
                self._show_error(self.i2c_error, f"No bus activity: {err}")
            else:
                self._show_error(self.i2c_error, f"I2C capture failed: {err}")
            return
        result, logger = payload
        assert isinstance(result, I2CAnalysisResult)
        self._plot_i2c(result)
        self._show_i2c_results(result, logger)
        self._set_status(f"I2C done — appended to {logger.path}")
        self._save_settings()

    # --- plotting / results ----------------------------------------------
    def _plot_single(self, times, volts, channel, title=None):
        self.ax.clear()
        self.ax.plot(times, volts, color="#1f77b4", linewidth=0.8, label=channel)
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Volts")
        self.ax.set_title(title or f"{channel} waveform")
        self.ax.grid(True, alpha=0.3)
        self.ax.legend(loc="upper right")
        self.fig.tight_layout()
        self.canvas.draw_idle()

    def _plot_i2c(self, result: I2CAnalysisResult):
        self.ax.clear()
        self.ax.plot(result.scl_t, result.scl_v, color="#1f77b4",
                     linewidth=0.8, label="SCL")
        self.ax.plot(result.sda_t, result.sda_v, color="#ff7f0e",
                     linewidth=0.8, label="SDA")
        self.ax.axhline(result.high_v, color="gray", linestyle="--",
                        linewidth=0.8, label=f"high {result.high_v:.2f} V")
        self.ax.axhline(result.low_v, color="gray", linestyle=":",
                        linewidth=0.8, label=f"low {result.low_v:.2f} V")
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Volts")
        self.ax.set_title("I2C capture (SCL / SDA)")
        self.ax.grid(True, alpha=0.3)
        self.ax.legend(loc="upper right", fontsize=8)
        self.fig.tight_layout()
        self.canvas.draw_idle()

    def _show_i2c_results(self, result: I2CAnalysisResult, logger: ResultLogger):
        lines = [f"I2C run {logger.run_id}", f"Log: {logger.path}", ""]
        lines.append("Levels:")
        for name, m in result.levels.items():
            if m.valid:
                lines.append(f"  {name:9s}: {m.value:.3f} {m.units}")
            else:
                lines.append(f"  {name:9s}: INVALID ({m.note})")
        lines.append("")
        lines.append("Timing:")
        for t in result.timings:
            if t.value_s is None:
                lines.append(f"  {t.name:9s}: INVALID [{t.status}] {t.note}")
            else:
                tag = f"[{t.status}]" if t.status != "NO LIMIT" else ""
                lines.append(
                    f"  {t.name:9s}: {t.value_s * 1e9:8.1f} ns {tag}".rstrip())
        self._set_results("\n".join(lines) + "\n")

    def _set_results(self, text: str):
        self.results.configure(state="normal")
        self.results.delete("1.0", "end")
        self.results.insert("1.0", text)
        self.results.configure(state="disabled")

    def _set_status(self, text: str):
        self.status.configure(text=text)

    def _post_status(self, text: str):
        self._ui_q.put(("status", text))

    def _set_action_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for btn in self._action_btns:
            btn.configure(state=state)

    def _run_async(self, work: Callable, on_done: Callable):
        if self._busy:
            self._set_status("Busy — wait for the current action to finish.")
            return
        self._busy = True
        self._set_action_enabled(False)
        self.btn_connect.configure(state="disabled")
        self.btn_disconnect.configure(state="disabled")

        def runner():
            try:
                result = work()
                self._ui_q.put(("done", on_done, result, None))
            except Exception as e:
                self._ui_q.put(("done", on_done, None, e))

        threading.Thread(target=runner, daemon=True).start()

    def _drain_ui_queue(self):
        try:
            while True:
                item = self._ui_q.get_nowait()
                kind = item[0]
                if kind == "status":
                    self._set_status(item[1])
                elif kind == "done":
                    _, on_done, result, err = item
                    self._busy = False
                    if self._closing:
                        if err is not None:
                            traceback.print_exception(
                                type(err), err, err.__traceback__)
                        on_done(result, err)
                        continue
                    connected = self.scope is not None
                    self.btn_connect.configure(
                        state="disabled" if connected else "normal")
                    self.btn_disconnect.configure(
                        state="normal" if connected else "disabled")
                    self._set_resource_locked(connected)
                    self._set_action_enabled(connected)
                    self._set_connected(connected)
                    if err is not None:
                        traceback.print_exception(type(err), err, err.__traceback__)
                    on_done(result, err)
        except queue.Empty:
            pass
        self.after(100, self._drain_ui_queue)


def main():
    app = AutoScopeApp()
    app.mainloop()


if __name__ == "__main__":
    main()
