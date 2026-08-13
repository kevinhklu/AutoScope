"""
gui.py — CustomTkinter + matplotlib front end for AutoScope.

All bench settings (scope resource, bus voltage, limits, CSV log name) are
entered in the GUI. VISA work runs on a worker thread.

Run:  python gui.py
"""

from __future__ import annotations

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

# limit_key, label, which bound ("min" or "max")
_LIMIT_ROWS = (
    ("tscl_fall", "Tscl fall max (ns)", "max"),
    ("scl_high", "Scl high min (ns)", "min"),
    ("scl_low", "Scl low min (ns)", "min"),
    ("thd_dat", "tHD;DAT min (ns)", "min"),
    ("tsu_dat", "tSU;DAT min (ns)", "min"),
)


class AutoScopeApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("AutoScope")
        self.geometry("1280x780")
        self.minsize(1024, 680)

        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.scope: Optional[Scope] = None
        self._busy = False
        self._closing = False
        self._ui_q: queue.Queue = queue.Queue()
        self._action_btns: list[ctk.CTkButton] = []
        self._limit_entries: dict[str, ctk.CTkEntry] = {}

        self._build()
        self._set_status("Enter settings, then Connect.")
        self.after(100, self._drain_ui_queue)
        self.after(300, self._on_detect)   # auto-fill the VISA resource on launch
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

        side = ctk.CTkFrame(main, width=360)
        side.grid(row=0, column=1, sticky="ns")
        side.grid_propagate(False)

        ctk.CTkLabel(side, text="AutoScope", font=ctk.CTkFont(size=18, weight="bold")
                     ).pack(anchor="w", padx=12, pady=(12, 4))

        self.status = ctk.CTkLabel(side, text="", wraplength=320, justify="left",
                                   anchor="w")
        self.status.pack(fill="x", padx=12, pady=(0, 6))

        scroll = ctk.CTkScrollableFrame(side, width=336, height=520)
        scroll.pack(fill="both", expand=True, padx=8, pady=4)

        self._build_settings(scroll)

        ctk.CTkLabel(side, text="Results", anchor="w").pack(
            fill="x", padx=12, pady=(4, 0))
        self.results = ctk.CTkTextbox(side, height=140, wrap="word",
                                      font=ctk.CTkFont(family="Consolas", size=11))
        self.results.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        self.results.insert("1.0", "(no results yet)\n")
        self.results.configure(state="disabled")

    def _labeled_entry(self, parent, label: str, default: str = "") -> ctk.CTkEntry:
        ctk.CTkLabel(parent, text=label, anchor="w").pack(fill="x", pady=(8, 0))
        entry = ctk.CTkEntry(parent)
        entry.pack(fill="x", pady=(2, 0))
        if default:
            entry.insert(0, default)
        return entry

    def _build_settings(self, parent):
        ctk.CTkLabel(parent, text="Scope", font=ctk.CTkFont(weight="bold")
                     ).pack(anchor="w", pady=(4, 0))
        self.resource_entry = self._labeled_entry(parent, "VISA resource", "")
        self.btn_detect = ctk.CTkButton(parent, text="Auto-detect scope",
                                        command=self._on_detect)
        self.btn_detect.pack(fill="x", pady=(4, 0))

        conn = ctk.CTkFrame(parent, fg_color="transparent")
        conn.pack(fill="x", pady=(8, 0))
        self.btn_connect = ctk.CTkButton(conn, text="Connect", width=150,
                                         command=self._on_connect)
        self.btn_connect.pack(side="left", padx=(0, 6))
        self.btn_disconnect = ctk.CTkButton(conn, text="Disconnect", width=150,
                                            command=self._on_disconnect,
                                            state="disabled")
        self.btn_disconnect.pack(side="left")

        ctk.CTkLabel(parent, text="CSV log", font=ctk.CTkFont(weight="bold")
                     ).pack(anchor="w", pady=(12, 0))
        self.csv_entry = self._labeled_entry(
            parent, f"File name (saved under results/)", "measurements.csv")
        ctk.CTkLabel(parent, text="New file → header row. Existing → append.",
                     font=ctk.CTkFont(size=11), text_color="gray",
                     anchor="w", wraplength=300).pack(fill="x")

        ctk.CTkLabel(parent, text="Bus / I2C", font=ctk.CTkFont(weight="bold")
                     ).pack(anchor="w", pady=(12, 0))
        self.vdd_entry = self._labeled_entry(parent, "Vdd (V)", "1.8")
        row_pct = ctk.CTkFrame(parent, fg_color="transparent")
        row_pct.pack(fill="x", pady=(4, 0))
        ctk.CTkLabel(row_pct, text="High %").pack(side="left")
        self.high_pct_entry = ctk.CTkEntry(row_pct, width=60)
        self.high_pct_entry.pack(side="left", padx=(6, 16))
        self.high_pct_entry.insert(0, "70")
        ctk.CTkLabel(row_pct, text="Low %").pack(side="left")
        self.low_pct_entry = ctk.CTkEntry(row_pct, width=60)
        self.low_pct_entry.pack(side="left", padx=(6, 0))
        self.low_pct_entry.insert(0, "30")
        self.probe_delay_entry = self._labeled_entry(
            parent, "Probe countdown (s, 0 = off)", "5")

        ctk.CTkLabel(parent, text="I2C limits (ns, blank = no check)",
                     font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(12, 0))
        for key, label, bound in _LIMIT_ROWS:
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=label, width=140, anchor="w").pack(side="left")
            entry = ctk.CTkEntry(row, width=80)
            entry.pack(side="right")
            default = DEFAULT_LIMITS.get(key, {}).get(f"{bound}_ns")
            if default is not None:
                entry.insert(0, str(int(default) if default == int(default) else default))
            self._limit_entries[key] = (entry, bound)

        ctk.CTkLabel(parent, text="Acquire / measure", font=ctk.CTkFont(weight="bold")
                     ).pack(anchor="w", pady=(12, 0))
        ctk.CTkLabel(parent, text="Channel", anchor="w").pack(fill="x", pady=(4, 0))
        self.channel_var = ctk.StringVar(value="CH1")
        ctk.CTkOptionMenu(parent, variable=self.channel_var,
                          values=list(_CHANNELS)).pack(fill="x", pady=2)

        ctk.CTkLabel(parent, text="Measurements (one acquisition)",
                     anchor="w").pack(fill="x", pady=(8, 0))
        self.meas_vars: dict[str, ctk.BooleanVar] = {}
        for label in _BASIC_MEAS:
            var = ctk.BooleanVar(value=True)
            self.meas_vars[label] = var
            ctk.CTkCheckBox(parent, text=label, variable=var).pack(anchor="w", pady=1)

        ctk.CTkLabel(parent, text="I2C SCL / SDA", anchor="w").pack(
            fill="x", pady=(8, 0))
        i2c_row = ctk.CTkFrame(parent, fg_color="transparent")
        i2c_row.pack(fill="x", pady=2)
        ctk.CTkLabel(i2c_row, text="SCL").pack(side="left")
        self.scl_var = ctk.StringVar(value="CH1")
        ctk.CTkOptionMenu(i2c_row, variable=self.scl_var, values=list(_CHANNELS),
                          width=90).pack(side="left", padx=(6, 12))
        ctk.CTkLabel(i2c_row, text="SDA").pack(side="left")
        self.sda_var = ctk.StringVar(value="CH2")
        ctk.CTkOptionMenu(i2c_row, variable=self.sda_var, values=list(_CHANNELS),
                          width=90).pack(side="left", padx=(6, 0))

        ctk.CTkLabel(parent, text="Run", font=ctk.CTkFont(weight="bold")
                     ).pack(anchor="w", pady=(12, 0))
        self.btn_acquire = self._action_button(
            parent, "Acquire & Measure", self._on_acquire)
        self.btn_i2c = self._action_button(
            parent, "I2C Capture", self._on_i2c)

    def _action_button(self, parent, text, command) -> ctk.CTkButton:
        btn = ctk.CTkButton(parent, text=text, command=command, state="disabled")
        btn.pack(fill="x", pady=3)
        self._action_btns.append(btn)
        return btn

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
            val = self._parse_optional_float(entry.get())
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
        state = "disabled" if locked else "normal"
        self.resource_entry.configure(state=state)

    # --- auto-detect -----------------------------------------------------
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
            self._set_status(f"Detected scope: {res}")
        else:
            self._set_status("No scope found — check USB/power, or type the "
                             "resource string manually.")

    # --- connection ------------------------------------------------------
    def _on_connect(self):
        if self.scope is not None:
            return
        try:
            cfg = self._settings_from_gui()
        except ValueError as e:
            self._set_status(str(e))
            return
        self._run_async(lambda: self._connect_worker(cfg.resource),
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
            self._set_status(f"Connect failed: {err}")
            return
        s, idn = result
        if self._closing:
            try:
                s.__exit__(None, None, None)
            except Exception:
                pass
            return
        self.scope = s
        self.btn_connect.configure(state="disabled")
        self.btn_disconnect.configure(state="normal")
        self._set_resource_locked(True)
        self._set_action_enabled(True)
        self._set_status(f"Connected: {idn}")

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
            logger = self._logger_from_gui()
        except ValueError as e:
            self._set_status(str(e))
            return
        self._run_async(lambda: self._acquire_worker(ch, selected, logger),
                        on_done=self._after_acquire)

    def _restore_scope_live(self):
        if self.scope is None:
            return
        try:
            self.scope.restore_live()
        except Exception:
            pass

    def _acquire_worker(self, channel: str, selected: list[tuple[str, str]],
                        logger: ResultLogger):
        assert self.scope is not None
        try:
            labels = ", ".join(l for l, _ in selected) or "(plot only)"
            self._post_status(f"Acquiring {channel} — {labels}...")
            self.scope.write("MEASUrement:REFLevel:METHod PERCent")
            self.scope.ensure_channel_on(channel)
            self.scope.single_acquisition()
            results = []
            for label, scpi in selected:
                m = read_measurement(self.scope, scpi, channel)
                results.append((label, m))
                if m.valid:
                    logger.log(f"{label} ({channel})", m.value, units=m.units,
                               status="OK")
                else:
                    logger.log(f"{label} ({channel})", None, units=m.units,
                               status="INVALID", note=m.note)
            times, volts = self.scope.read_waveform(channel)
            return {"channel": channel, "times": times, "volts": volts,
                    "results": results, "logger": logger}
        finally:
            self._restore_scope_live()

    def _after_acquire(self, result, err):
        if err:
            self._set_status(f"Acquire failed: {err}")
            return
        ch = result["channel"]
        logger = result["logger"]
        self._plot_single(result["times"], result["volts"], ch,
                          title=f"{ch} acquisition")
        lines = [f"Acquire {ch} ({len(result['times'])} samples)",
                 f"Log: {logger.path} (run {logger.run_id})", ""]
        if not result["results"]:
            lines.append("  (no measurements selected)")
        for label, m in result["results"]:
            if m.valid:
                lines.append(f"  {label}: {m.value} {m.units}")
            else:
                lines.append(f"  {label}: INVALID — {m.note}")
        self._set_results("\n".join(lines) + "\n")
        self._set_status(
            f"Acquired {ch} — logged to {logger.path}")

    def _on_i2c(self):
        try:
            cfg = self._settings_from_gui()
            logger = self._logger_from_gui()
        except ValueError as e:
            self._set_status(str(e))
            return
        scl, sda = self.scl_var.get(), self.sda_var.get()
        if scl == sda:
            self._set_status(f"SCL and SDA must differ (both {scl}).")
            return
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
                self._set_status(f"No bus activity: {err}")
            else:
                self._set_status(f"I2C capture failed: {err}")
            return
        result, logger = payload
        assert isinstance(result, I2CAnalysisResult)
        self._plot_i2c(result)
        self._show_i2c_results(result, logger)
        self._set_status(f"I2C done — appended to {logger.path}")

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
