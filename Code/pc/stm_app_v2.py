"""Panda STM control panel (optimized rewrite of stm_app.py).

Main differences against stm_app.py:
  * Serial port defaults to DEFAULT_PORT and is auto-connected at start-up;
    available ports are listed in a combobox and can be re-scanned.
  * Blocking STM commands (IV sweep, scan, curve download) run on worker
    threads, so the UI never freezes; a TaskRunner marshals results back.
  * Serial/IO failures are reported in the log instead of killing the app.
  * Controls are grouped in labelled sections inside a scrollable side panel,
    so the window fits on a laptop screen; plot size adapts to the screen.
  * DAC widgets show both the requested value and the value the device
    actually reports, converted to volts.
  * Output directory is created on demand before saving.
"""

from __future__ import annotations

import csv
import os
import queue
import threading
import time
from datetime import datetime

import numpy as np
import tkinter as tk
from tkinter import ttk

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import (
    FigureCanvasTkAgg, NavigationToolbar2Tk)
from matplotlib.figure import Figure
from matplotlib import rcParams

import stm_control

try:
    from serial.tools import list_ports
except ImportError:  # pragma: no cover - pyserial is a hard dependency anyway
    list_ports = None

rcParams.update({'figure.autolayout': True})


DEFAULT_PORT = "/dev/cu.usbmodem164952701"
DATA_DIR = "./data"

STATUS_POLL_MS = 100
IMAGE_POLL_MS = 250
TASK_POLL_MS = 50

PLOT_DPI = 100.0
MAX_PLOT_PX = 430
MIN_PLOT_PX = 260
HISTORY_WINDOW_S = 60.0


def timestamp() -> int:
    """Milliseconds since epoch, used as a suffix for saved files."""
    return int(datetime.timestamp(datetime.now()) * 1000)


def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def save_data_to_file(filename_prefix: str, rows) -> str:
    path = f"{filename_prefix}_{timestamp()}.csv"
    ensure_parent_dir(path)
    with open(path, 'w', newline='') as csvfile:
        datawriter = csv.writer(csvfile, delimiter=',',
                                quotechar='|', quoting=csv.QUOTE_MINIMAL)
        for row in rows:
            datawriter.writerow(row)
    return path


class TaskRunner:
    """Runs blocking STM commands off the Tk main thread.

    Callbacks are delivered on the Tk thread from a polled queue, so widget
    updates stay single-threaded. One task per name may run at a time.
    """

    def __init__(self, widget: tk.Misc):
        self._widget = widget
        self._results: queue.Queue = queue.Queue()
        self._running: set = set()
        self._lock = threading.Lock()
        self._widget.after(TASK_POLL_MS, self._pump)

    def is_running(self, name: str) -> bool:
        with self._lock:
            return name in self._running

    def submit(self, name, func, *args, on_done=None, on_error=None) -> bool:
        """Start ``func(*args)`` in a worker thread. Returns False if busy."""
        with self._lock:
            if name in self._running:
                return False
            self._running.add(name)

        def _worker():
            try:
                result = func(*args)
            except Exception as exc:  # surfaced through on_error on the UI thread
                self._results.put((name, None, exc, on_done, on_error))
            else:
                self._results.put((name, result, None, on_done, on_error))

        threading.Thread(target=_worker, name=f"stm-{name}",
                         daemon=True).start()
        return True

    def _pump(self):
        while True:
            try:
                name, result, exc, on_done, on_error = self._results.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                self._running.discard(name)
            if exc is not None:
                if on_error:
                    on_error(exc)
            elif on_done:
                on_done(result)
        self._widget.after(TASK_POLL_MS, self._pump)


class ScrollableFrame(ttk.Frame):
    """Vertically scrollable container; put widgets into ``.body``."""

    def __init__(self, parent, width=360, **kwargs):
        super().__init__(parent, **kwargs)
        self.canvas = tk.Canvas(self, width=width, highlightthickness=0)
        scrollbar = ttk.Scrollbar(
            self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas)

        self._window = self.canvas.create_window(
            (0, 0), window=self.body, anchor=tk.NW)
        self.canvas.configure(yscrollcommand=scrollbar.set)

        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", lambda _e: self._bind_wheel())
        self.canvas.bind("<Leave>", lambda _e: self._unbind_wheel())

    def _on_body_configure(self, _event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)

    def _bind_wheel(self):
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)
        self.canvas.bind_all("<Button-4>", self._on_wheel)
        self.canvas.bind_all("<Button-5>", self._on_wheel)

    def _unbind_wheel(self):
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Button-4>")
        self.canvas.unbind_all("<Button-5>")

    def _on_wheel(self, event):
        if event.num == 4:
            delta = -1
        elif event.num == 5:
            delta = 1
        else:  # macOS / Windows report a signed delta
            delta = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(delta, "units")


class PlotFrame(ttk.Frame):
    """A matplotlib figure embedded in Tk, as a line plot or an image."""

    def __init__(self, parent, with_toolbar=False, dpi=PLOT_DPI,
                 width=400, height=400, **kwargs):
        super().__init__(parent, **kwargs)
        self.figure = Figure(figsize=(width / dpi, height / dpi), dpi=dpi)
        self.plot = None
        self.image = None
        self.colorbar = None

        self.canvas = FigureCanvasTkAgg(self.figure, master=self)
        self.canvas.draw()
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        if with_toolbar:
            self.toolbar = NavigationToolbar2Tk(self.canvas, self)
            self.toolbar.update()

    def add_plot(self, label=None, xlabel=None, ylabel=None):
        ax = self.figure.add_subplot(111)
        self.plot = ax.plot([0, 1], [0, 0], '-', lw=1.0, label=label)[0]
        ax.set_autoscalex_on(True)
        ax.set_autoscaley_on(True)
        ax.grid(True, alpha=0.3, lw=0.5)
        if label:
            ax.legend(loc='upper right', fontsize='small')
        if xlabel:
            ax.set_xlabel(xlabel, fontsize='small')
        if ylabel:
            ax.set_ylabel(ylabel, fontsize='small')
        ax.tick_params(labelsize='small')

    def add_image(self, image, title=None, colorbar=True):
        ax = self.figure.add_subplot(111)
        self.image = ax.imshow(image, interpolation='none',
                               norm='linear', origin='lower')
        if title:
            ax.set_title(title, fontsize='small')
        ax.tick_params(labelsize='small')
        if colorbar:
            self.colorbar = self.figure.colorbar(self.image, ax=ax)
            self.colorbar.ax.tick_params(labelsize='small')

    def update_plot(self, x_data, y_data):
        if self.plot is None or not len(x_data):
            return
        self.plot.set_data(x_data, y_data)
        ax = self.figure.get_axes()[0]
        ax.relim()
        ax.autoscale_view()
        self.canvas.draw_idle()

    def update_image(self, image_data, extent=None):
        if self.image is None:
            return
        self.image.set_data(image_data)
        self.image.autoscale()
        if extent:
            self.image.set_extent(extent)
        self.canvas.draw_idle()

    def save_figure(self, image_path):
        ensure_parent_dir(image_path)
        self.figure.savefig(image_path, dpi=200)


class EntryRow(ttk.Frame):
    """A button plus N labelled entries; the button calls ``command(*values)``."""

    def __init__(self, parent, text, defaults, command,
                 labels=None, entry_width=10, **kwargs):
        super().__init__(parent, **kwargs)
        self.command = command
        self.vars: list[tk.StringVar] = []

        button = ttk.Button(self, text=text, width=14, command=self._invoke)
        button.grid(row=0, column=0, rowspan=2, padx=(0, 6), sticky=tk.W)
        self.button = button

        for i, default in enumerate(defaults):
            if labels and i < len(labels) and labels[i]:
                ttk.Label(self, text=labels[i], font=('TkDefaultFont', 9)).grid(
                    row=0, column=i + 1, sticky=tk.W)
            var = tk.StringVar(value=str(default))
            ttk.Entry(self, textvariable=var, width=entry_width).grid(
                row=1, column=i + 1, padx=1, sticky=tk.W)
            self.vars.append(var)

    def values(self) -> list[str]:
        return [var.get().strip() for var in self.vars]

    def _invoke(self):
        self.command(*self.values())


class DacControl(ttk.Frame):
    """Set-value entry for one DAC channel, with requested/device readouts."""

    STEP = 256

    def __init__(self, parent, text, default_value, cmd_func, convert_func,
                 unit="V", **kwargs):
        super().__init__(parent, **kwargs)
        self.cmd_func = cmd_func
        self.convert_func = convert_func
        self.unit = unit

        self.input_var = tk.StringVar(value=str(default_value))
        self.set_var = tk.StringVar(value="--")
        self.device_var = tk.StringVar(value="dev --")

        ttk.Button(self, text=text, width=8, command=self.set_value).grid(
            row=0, column=0, padx=(0, 4))
        ttk.Entry(self, textvariable=self.input_var, width=9).grid(
            row=0, column=1)
        ttk.Button(self, text="-", width=2,
                   command=lambda: self._nudge(-self.STEP)).grid(row=0, column=2)
        ttk.Button(self, text="+", width=2,
                   command=lambda: self._nudge(self.STEP)).grid(row=0, column=3)
        ttk.Label(self, textvariable=self.set_var, width=10,
                  anchor=tk.E).grid(row=0, column=4, padx=(6, 0))
        ttk.Label(self, textvariable=self.device_var, width=12,
                  anchor=tk.E, foreground="#2a6fb0").grid(row=0, column=5)

        self._refresh_set_display()

    def raw_value(self):
        """Current entry content as int, or None when it is not a number."""
        try:
            return int(float(self.input_var.get()))
        except (TypeError, ValueError):
            return None

    def set_value(self):
        value = self.raw_value()
        if value is None:
            return False
        self.cmd_func(value)
        self._refresh_set_display()
        return True

    def update_device_value(self, dac_value):
        if dac_value is None:
            self.device_var.set("dev --")
        else:
            self.device_var.set(
                f"dev {self.convert_func(int(dac_value)):+.3f}{self.unit}")

    def _nudge(self, delta):
        value = self.raw_value()
        if value is None:
            return
        self.input_var.set(str(value + delta))
        self.set_value()

    def _refresh_set_display(self):
        value = self.raw_value()
        self.set_var.set("--" if value is None
                         else f"{self.convert_func(value):+.3f}{self.unit}")


class App(tk.Tk):
    STATUS_FIELDS = [
        ("Bias", "bias"), ("DAC Z", "dac_z"), ("DAC X", "dac_x"),
        ("DAC Y", "dac_y"), ("ADC", "adc"), ("Steps", "steps"),
        ("Approaching", "is_approaching"), ("ConstCurrent", "is_const_current"),
        ("Scanning", "is_scanning"), ("Time(ms)", "time_millis"),
    ]

    def __init__(self, port=DEFAULT_PORT):
        super().__init__()
        self.wm_title("Panda STM")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.stm = stm_control.STM()
        self.tasks = TaskRunner(self)
        self.dac_controls: dict[str, DacControl] = {}
        self.status_vars: dict[str, tk.StringVar] = {}
        self._image_dirty = True
        self._was_busy = False
        self._trace = []
        self._status_live = False
        self._iv_live = False

        plot_px = max(MIN_PLOT_PX,
                      min(MAX_PLOT_PX, (self.winfo_screenheight() - 200) // 2))

        self._build_control_panel()
        self._build_plot_panel(plot_px)

        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        self._poll_status()
        self._poll_images()

        self.update_idletasks()
        self.geometry(
            f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0")
        if self.tk.call("tk", "windowingsystem") == "win32":
            self.state("zoomed")

        self.after(200, lambda: self.connect(port, quiet=True))

    # ---------------------------------------------------------------- layout

    def _build_control_panel(self):
        container = ScrollableFrame(self, width=430)
        container.grid(row=0, column=0, padx=(8, 4), pady=6, sticky=tk.NSEW)
        panel = container.body

        row = 0
        for _label, key in self.STATUS_FIELDS:
            self.status_vars[key] = tk.StringVar(value="--")
        self.adc_current_var = tk.StringVar(value="-- nA")

        # --- connection -----------------------------------------------------
        conn = ttk.LabelFrame(panel, text="Connection", padding=6)
        conn.grid(row=row, column=0, sticky=tk.EW, pady=4)
        row += 1

        self.port_var = tk.StringVar(value=DEFAULT_PORT)
        self.port_combo = ttk.Combobox(
            conn, textvariable=self.port_var, width=30)
        self.port_combo.grid(row=0, column=0, columnspan=2, sticky=tk.W)
        ttk.Button(conn, text="Rescan", width=8,
                   command=self.refresh_ports).grid(row=0, column=2, padx=2)

        self.connect_button = ttk.Button(
            conn, text="Connect", width=12, command=self._toggle_connection)
        self.connect_button.grid(row=1, column=0, pady=(6, 0), sticky=tk.W)
        self.conn_status_var = tk.StringVar(value="disconnected")
        ttk.Label(conn, textvariable=self.conn_status_var,
                  foreground="#b03a2a").grid(row=1, column=1, columnspan=2,
                                             pady=(6, 0), sticky=tk.W)

        buttons = ttk.Frame(conn)
        buttons.grid(row=2, column=0, columnspan=3, pady=(6, 0), sticky=tk.W)
        for i, (text, func) in enumerate([("STOP", self._stop),
                                          ("Reset", self._reset),
                                          ("Clear", self._clear)]):
            ttk.Button(buttons, text=text, width=8,
                       command=func).grid(row=0, column=i, padx=1)

        readout = ttk.Frame(conn)
        readout.grid(row=3, column=0, columnspan=3, pady=(8, 0), sticky=tk.W)
        ttk.Label(readout, text="ADC").grid(row=0, column=0, sticky=tk.W)
        ttk.Label(readout, textvariable=self.status_vars["adc"],
                  font=('TkFixedFont', 16), width=8).grid(
            row=0, column=1, padx=(4, 16), sticky=tk.W)
        ttk.Label(readout, text="I").grid(row=0, column=2, sticky=tk.W)
        ttk.Label(readout, textvariable=self.adc_current_var,
                  font=('TkFixedFont', 16), width=12).grid(
            row=0, column=3, padx=(4, 16), sticky=tk.W)
        ttk.Label(readout, text="Time").grid(row=0, column=4, sticky=tk.W)
        ttk.Label(readout, textvariable=self.status_vars["time_millis"],
                  font=('TkFixedFont', 16), width=10).grid(
            row=0, column=5, padx=(4, 0), sticky=tk.W)
        self.refresh_ports()

        # --- DAC ------------------------------------------------------------
        dac = ttk.LabelFrame(panel, text="DAC Control", padding=6)
        dac.grid(row=row, column=0, sticky=tk.EW, pady=4)
        row += 1

        specs = [
            ("bias", "Bias", "33314", self.stm.set_bias,
             stm_control.STM_Status.dac_to_bias_volts),
            ("dac_z", "DACZ", "32768", self.stm.set_dacz,
             stm_control.STM_Status.dac_to_dacz_volts),
            ("dac_x", "DACX", "32768", self.stm.set_dacx,
             stm_control.STM_Status.dac_to_dacx_volts),
            ("dac_y", "DACY", "32768", self.stm.set_dacy,
             stm_control.STM_Status.dac_to_dacy_volts),
        ]
        for i, (key, text, default, cmd, conv) in enumerate(specs):
            control = DacControl(dac, text, default, cmd, conv)
            control.grid(row=i, column=0, sticky=tk.W, pady=2)
            self.dac_controls[key] = control
        ttk.Button(dac, text="Set All DAC", width=14,
                   command=self._set_all_dac).grid(row=len(specs), column=0,
                                                   pady=(6, 0), sticky=tk.W)

        # --- approach / feedback -------------------------------------------
        feedback = ttk.LabelFrame(panel, text="Approach & Feedback", padding=6)
        feedback.grid(row=row, column=0, sticky=tk.EW, pady=4)
        row += 1

        EntryRow(feedback, "Move Motor", ["100"], self._move_motor,
                 labels=["steps (+/-)"]).grid(
            row=0, column=0, sticky=tk.W, pady=2)
        EntryRow(feedback, "Approach", ["500", "1"], self._approach,
                 labels=["target ADC", "steps"]).grid(
            row=1, column=0, sticky=tk.W, pady=2)
        EntryRow(feedback, "Set PID", ["0.0001", "0.0001", "0.0"],
                 self._set_pid, labels=["Kp", "Ki", "Kd"]).grid(
            row=2, column=0, sticky=tk.W, pady=2)
        EntryRow(feedback, "ConstCurrent ON", ["1000"],
                 self._const_current_on, labels=["target ADC"]).grid(
            row=3, column=0, sticky=tk.W, pady=2)
        ttk.Button(feedback, text="ConstCurrent OFF", width=18,
                   command=self._const_current_off).grid(
            row=4, column=0, sticky=tk.W, pady=2)

        # --- IV curve -------------------------------------------------------
        iv = ttk.LabelFrame(panel, text="IV Curve", padding=6)
        iv.grid(row=row, column=0, sticky=tk.EW, pady=4)
        row += 1

        self.iv_row = EntryRow(iv, "Plot IV", ["31768", "33768", "10"],
                               self._plot_iv_curve,
                               labels=["start", "end", "step"])
        self.iv_row.grid(row=0, column=0, sticky=tk.W, pady=2)
        self.iv_live_btn = ttk.Button(iv, text="Live IV", width=14,
                                     command=self._toggle_iv_live)
        self.iv_live_btn.grid(row=0, column=1, padx=(8, 0), sticky=tk.W)
        EntryRow(iv, "Save IV", [f"{DATA_DIR}/iv_curve"], self._save_iv_curve,
                 labels=["prefix"], entry_width=24).grid(
            row=1, column=0, sticky=tk.W, pady=2)

        # --- scan -----------------------------------------------------------
        scan = ttk.LabelFrame(panel, text="Scan", padding=6)
        scan.grid(row=row, column=0, sticky=tk.EW, pady=4)
        row += 1

        self.scan_x = EntryRow(scan, "X range", ["31768", "33768", "512"],
                               lambda *_a: None,
                               labels=["start", "end", "resolution"])
        self.scan_x.button.state(["disabled"])
        self.scan_x.grid(row=0, column=0, sticky=tk.W, pady=2)

        self.scan_y = EntryRow(scan, "Y range", ["31768", "33768", "512"],
                               lambda *_a: None,
                               labels=["start", "end", "resolution"])
        self.scan_y.button.state(["disabled"])
        self.scan_y.grid(row=1, column=0, sticky=tk.W, pady=2)

        self.scan_samples = EntryRow(scan, "Start Scan", ["10"],
                                     self._start_scan, labels=["samples"])
        self.scan_samples.grid(row=2, column=0, sticky=tk.W, pady=2)

        EntryRow(scan, "Save Image", [f"{DATA_DIR}/image"],
                 self._save_scan_image, labels=["prefix"],
                 entry_width=24).grid(row=3, column=0, sticky=tk.W, pady=2)

        # --- raw console ----------------------------------------------------
        console = ttk.LabelFrame(panel, text="Raw Command", padding=6)
        console.grid(row=row, column=0, sticky=tk.EW, pady=4)
        row += 1

        self.cmd_var = tk.StringVar()
        cmd_entry = ttk.Entry(console, textvariable=self.cmd_var, width=32)
        cmd_entry.grid(row=0, column=0, sticky=tk.W)
        cmd_entry.bind("<Return>", lambda _e: self._send_cmd())
        ttk.Button(console, text="Send", width=8,
                   command=self._send_cmd).grid(row=0, column=1, padx=4)

        # --- status ---------------------------------------------------------
        status = ttk.LabelFrame(panel, text="Status", padding=6)
        status.grid(row=row, column=0, sticky=tk.EW, pady=4)
        row += 1

        for i, (label, key) in enumerate(self.STATUS_FIELDS):
            var = self.status_vars[key]
            col, line = divmod(i, 5)
            ttk.Label(status, text=f"{label}:").grid(
                row=line, column=col * 2, sticky=tk.W, padx=(0, 4))
            ttk.Label(status, textvariable=var, width=10, anchor=tk.W,
                      font=('TkFixedFont', 10)).grid(
                row=line, column=col * 2 + 1, sticky=tk.W, padx=(0, 12))

        # --- log ------------------------------------------------------------
        log_frame = ttk.LabelFrame(panel, text="Log", padding=6)
        log_frame.grid(row=row, column=0, sticky=tk.EW, pady=4)

        self.log_text = tk.Text(log_frame, height=8, width=46, wrap=tk.WORD,
                                state=tk.DISABLED, font=('TkFixedFont', 10))
        self.log_text.grid(row=0, column=0, sticky=tk.EW)
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL,
                                   command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky=tk.NS)
        self.log_text.configure(yscrollcommand=log_scroll.set)

    def _build_plot_panel(self, plot_px):
        frame = ttk.Frame(self)
        frame.grid(row=0, column=1, padx=(4, 8), pady=6, sticky=tk.NSEW)

        def make(kind, row, col, **kwargs):
            plot = PlotFrame(frame, with_toolbar=True,
                             width=plot_px, height=plot_px)
            if kind == 'plot':
                plot.add_plot(**kwargs)
            else:
                plot.add_image(np.zeros((16, 16), dtype=np.float32), **kwargs)
            plot.grid(row=row, column=col, padx=4, pady=4, sticky=tk.NSEW)
            return plot

        self.current_plot = make('plot', 0, 0, label="current",
                                 xlabel="time (s)", ylabel="current (nA)")
        self.adc_plot = make('plot', 0, 1, label="ADC",
                             xlabel="time (s)", ylabel="ADC")
        self.iv_plot = make('plot', 0, 2, label="IV curve",
                            xlabel="bias (V)", ylabel="current (nA)")
        self.steps_plot = make('plot', 1, 0, label="steps",
                               xlabel="time (s)", ylabel="steps")
        self.z_image = make('image', 1, 1, title="Scan DAC Z")
        self.adc_image = make('image', 1, 2, title="Scan ADC")

        for col in range(3):
            frame.columnconfigure(col, weight=1)
        for r in range(2):
            frame.rowconfigure(r, weight=1)

    # ----------------------------------------------------------------- utils

    def log(self, message: str):
        line = f"[{datetime.now():%H:%M:%S}] {message}\n"
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, line)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _require_connection(self) -> bool:
        if not self.stm.is_opened:
            self.log("not connected - open a serial port first")
            return False
        return True

    def _guarded(self, description, func, *args) -> bool:
        """Run a short serial command, logging failures instead of raising."""
        if not self._require_connection():
            return False
        try:
            func(*args)
        except Exception as exc:
            self.log(f"{description} failed: {exc}")
            return False
        self.log(description)
        return True

    @staticmethod
    def _parse_ints(values):
        return [int(float(value)) for value in values]

    # ------------------------------------------------------------ connection

    def refresh_ports(self):
        ports = []
        if list_ports is not None:
            ports = [p.device for p in list_ports.comports()]
        if DEFAULT_PORT not in ports:
            ports.insert(0, DEFAULT_PORT)
        self.port_combo["values"] = ports
        if not self.port_var.get():
            self.port_var.set(ports[0])
        return ports

    def connect(self, port=None, quiet=False):
        port = port or self.port_var.get().strip()
        if not port:
            self.log("no serial port selected")
            return
        self.stm.close()
        self._status_live = False
        try:
            self.stm.open(port)
        except Exception as exc:
            self.stm.is_opened = False
            self._set_connection_state(False)
            err = str(exc)
            hint = ""
            if any(s in err.lower() for s in ("busy", "denied", "in use", "lock")):
                hint = "；先关掉其他 Panda STM 窗口，终端执行 pkill -f stm_app_v2.py"
            if not quiet:
                self.log(f"failed to open {port}: {exc}{hint}")
            else:
                self.log(f"auto-connect to {port} failed: {exc}{hint}")
            return
        self.port_var.set(port)
        self._set_connection_state(True)
        status = None
        for attempt in range(4):
            status = self.stm.get_status(timeout=2)
            if status is not None:
                break
            if attempt == 1:
                self.stm.reopen()
        if status is None:
            raw = repr(getattr(self.stm, "last_raw_status", ""))
            self.log(f"connected to {port}, GSTS empty raw={raw}")
            self.log("close other serial apps, then Disconnect and Connect")
        else:
            self._status_live = True
            self._render_status(status)
            self.log(f"connected to {port}, ADC={status.adc} Time={status.time_millis}")

    def disconnect(self):
        self._status_live = False
        self.stm.close()
        self._set_connection_state(False)
        self.log("disconnected")

    def _toggle_connection(self):
        if self.stm.is_opened:
            self.disconnect()
        else:
            self.connect()

    def _set_connection_state(self, connected: bool):
        self.connect_button.configure(
            text="Disconnect" if connected else "Connect")
        self.conn_status_var.set(
            f"connected: {self.port_var.get()}" if connected else "disconnected")

    # -------------------------------------------------------------- commands

    def _stop(self):
        self._guarded("STOP", self.stm.stop)

    def _reset(self):
        self._guarded("RESET", self.stm.reset)

    def _clear(self):
        self.stm.clear()
        self._trace = []
        self.log("history cleared")

    def _set_all_dac(self):
        if not self._require_connection():
            return
        for key in ("bias", "dac_z", "dac_x", "dac_y"):
            control = self.dac_controls[key]
            if control.raw_value() is None:
                self.log(f"{key}: invalid value, skipped")
                continue
            try:
                control.set_value()
            except Exception as exc:
                self.log(f"{key} failed: {exc}")
                return
            time.sleep(0.01)
        self.log("all DACs set")

    def _move_motor(self, steps):
        try:
            steps = int(float(steps))
        except ValueError:
            self.log("motor: steps must be an integer")
            return
        if not self._require_connection():
            return
        if self.tasks.is_running("motor"):
            self.log("motor already running")
            return
        self._status_live = False
        self.stm.busy = True
        if self.stm.is_opened:
            self.stm.stm_serial.reset_input_buffer()
            self.stm.stm_serial.reset_output_buffer()
        wait_s = abs(steps) / 2048.0 / 2.0 * 60.0 + 0.4

        def _run():
            self.stm.move_motor(steps)
            time.sleep(wait_s)

        def _done(_result):
            if self.stm.is_opened:
                self.stm.stm_serial.reset_input_buffer()
                self.stm.stm_serial.reset_output_buffer()
                self.stm.stm_serial.timeout = 0.2
            self.stm.busy = False
            status = None
            if self.stm.is_opened:
                for _ in range(5):
                    status = self.stm.get_status(timeout=1)
                    if status is not None:
                        break
            if status is not None:
                self._status_live = True
                self._render_status(status)
                self.log(f"motor moved {steps} steps")
            else:
                self.log(f"motor moved {steps} steps, GSTS lost — Disconnect then Connect")

        def _err(exc):
            self.stm.busy = False
            self._status_live = False
            self.log(f"motor failed: {exc}")

        self.tasks.submit("motor", _run, on_done=_done, on_error=_err)
        self.log(f"motor moving {steps} steps ...")

    def _approach(self, target_dac, steps):
        try:
            target_dac, steps = self._parse_ints([target_dac, steps])
        except ValueError:
            self.log("approach: target and steps must be integers")
            return
        self._guarded(f"approach target={target_dac} steps={steps}",
                      self.stm.approach, target_dac, steps)

    def _set_pid(self, kp, ki, kd):
        try:
            kp, ki, kd = (float(v) for v in (kp, ki, kd))
        except ValueError:
            self.log("PID: values must be numbers")
            return
        self._guarded(f"PID Kp={kp} Ki={ki} Kd={kd}",
                      self.stm.set_pid, kp, ki, kd)

    def _const_current_on(self, target_adc):
        try:
            target_adc = int(float(target_adc))
        except ValueError:
            self.log("const current: target must be an integer")
            return
        self._guarded(f"const current ON target={target_adc}",
                      self.stm.turn_on_const_current, target_adc)

    def _const_current_off(self):
        self._guarded("const current OFF", self.stm.turn_off_const_current)

    def _send_cmd(self):
        cmd = self.cmd_var.get().strip()
        if not cmd:
            return
        if self._guarded(f"sent: {cmd}", self.stm.send_cmd, cmd):
            self.cmd_var.set("")

    # -------------------------------------------------------------- IV curve

    def _toggle_iv_live(self):
        if self._iv_live:
            self._iv_live = False
            self.iv_live_btn.configure(text="Live IV")
            self._status_live = True
            self.log("live IV stopped")
            return
        if not self._require_connection():
            return
        self._iv_live = True
        self.iv_live_btn.configure(text="Stop Live IV")
        self.log("live IV started")
        self._run_iv_once()

    def _run_iv_once(self):
        if not self._iv_live or not self.stm.is_opened:
            return
        start, end, step = self._parse_ints(self.iv_row.values())
        self._status_live = False
        self.stm.busy = True
        started = self.tasks.submit(
            "iv", self.stm.measure_iv_curve, start, end, step,
            on_done=self._on_iv_done,
            on_error=self._on_iv_error)
        if not started:
            self.after(500, self._run_iv_once)

    def _plot_iv_curve(self, start, end, step):
        if not self._require_connection():
            return
        start, end, step = self._parse_ints([start, end, step])
        self._status_live = False
        self.stm.busy = True
        started = self.tasks.submit(
            "iv", self.stm.measure_iv_curve, start, end, step,
            on_done=self._on_iv_done,
            on_error=self._on_iv_error)
        if started:
            self.log(f"IV sweep {start} -> {end} step {step} ...")
        else:
            self.log("IV sweep already running")

    def _on_iv_error(self, exc):
        self.stm.busy = False
        self._status_live = not self._iv_live
        self.log(f"IV sweep failed: {exc}")
        if self._iv_live:
            self.after(800, self._run_iv_once)

    def _on_iv_done(self, values):
        self.stm.busy = False
        if not values or len(values) < 2:
            self.log("IV sweep returned no data")
        else:
            dac_values = values[::2]
            adc_values = [int(x) for x in values[1::2]
                          if str(x).lstrip("-").isdigit()]
            if len(dac_values) > 1 and len(adc_values) >= len(dac_values):
                adc_values = adc_values[:len(dac_values)]
                bias = [stm_control.STM_Status.dac_to_bias_volts(d)
                        for d in dac_values]
                current = [stm_control.STM_Status.adc_to_amp(a) * 1e9
                           for a in adc_values]
                self.iv_plot.update_plot(bias, current)
                self.log(f"IV sweep done ({len(bias)} points)")
        if self._iv_live:
            self.after(400, self._run_iv_once)
        else:
            self._status_live = True

    def _save_iv_curve(self, prefix):
        if not self._require_connection():
            return

        def _fetch_and_save():
            values = self.stm.get_iv_curve()
            return save_data_to_file(prefix, zip(values[::2], values[1::2]))

        started = self.tasks.submit(
            "iv_save", _fetch_and_save,
            on_done=lambda path: self.log(f"IV curve saved to {path}"),
            on_error=lambda exc: self.log(f"saving IV curve failed: {exc}"))
        if not started:
            self.log("IV download already running")

    # ------------------------------------------------------------------ scan

    def _start_scan(self, samples):
        if not self._require_connection():
            return
        x_start, x_end, x_res = self._parse_ints(self.scan_x.values())
        y_start, y_end, y_res = self._parse_ints(self.scan_y.values())
        samples = int(float(samples))
        raw = (x_start, x_end, x_res, y_start, y_end, y_res)
        x_start = stm_control._clamp_dac(x_start)
        x_end = stm_control._clamp_dac(x_end)
        y_start = stm_control._clamp_dac(y_start)
        y_end = stm_control._clamp_dac(y_end)
        x_res = stm_control._clamp_res(x_res)
        y_res = stm_control._clamp_res(y_res)
        samples = max(samples, 1)
        clamped = (x_start, x_end, x_res, y_start, y_end, y_res)
        if clamped != raw:
            self.log("scan range clamped to DAC 0-65535, resolution 1-2048")

        started = self.tasks.submit(
            "scan", self.stm.start_scan,
            x_start, x_end, x_res, y_start, y_end, y_res, samples, DATA_DIR,
            on_done=self._on_scan_done,
            on_error=self._on_scan_error)
        if started:
            self.log(f"scan started: X[{x_start},{x_end}]/{x_res} "
                     f"Y[{y_start},{y_end}]/{y_res} samples={samples}")
        else:
            self.log("scan already running")

    def _on_scan_done(self, paths):
        adc_path, dacz_path = paths if paths else (None, None)
        if adc_path:
            self.log(f"scan finished, saved {adc_path} , {dacz_path}")
        else:
            self.log("scan finished")

    def _on_scan_error(self, exc):
        self.stm.busy = False
        self.log(f"scan failed: {exc}")
        self._dump_scan_txt()

    def _dump_scan_txt(self):
        paths = getattr(self.stm, "scan_save_paths", (None, None))
        if paths[0] and os.path.isfile(paths[0]):
            self.log(f"partial scan kept in {paths[0]} , {paths[1]}")
            return
        if self.stm.scan_adc is None:
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        adc_path = os.path.join(DATA_DIR, f"scan_adc_{stamp}.txt")
        dacz_path = os.path.join(DATA_DIR, f"scan_dacz_{stamp}.txt")
        ensure_parent_dir(adc_path)
        np.savetxt(adc_path, self.stm.scan_adc)
        np.savetxt(dacz_path, self.stm.scan_dacz)
        self.log(f"partial scan saved {adc_path} , {dacz_path}")

    def _save_scan_image(self, prefix):
        if self.stm.scan_adc is None or self.stm.scan_dacz is None:
            self.log("no scan data to save")
            return
        ts = timestamp()
        try:
            ensure_parent_dir(f"{prefix}_adc_{ts}.txt")
            np.savetxt(f"{prefix}_adc_{ts}.txt", self.stm.scan_adc)
            np.savetxt(f"{prefix}_dacz_{ts}.txt", self.stm.scan_dacz)
            self.adc_image.save_figure(f"{prefix}_adc_{ts}.png")
            self.z_image.save_figure(f"{prefix}_dacz_{ts}.png")
        except Exception as exc:
            self.log(f"saving scan image failed: {exc}")
            return
        self.log(f"scan images saved as {prefix}_*_{ts}.*")

    # ----------------------------------------------------------- update loop

    def _poll_status(self):
        try:
            if self._status_live and self.stm.is_opened and not self.stm.busy:
                status = self.stm.get_status(timeout=0.2)
                if status is not None:
                    now = time.time()
                    adc_now = status.adc
                    if -32768 <= adc_now <= 32767:
                        self._trace.append((now, adc_now, status.steps))
                    cutoff = now - HISTORY_WINDOW_S
                    self._trace = [p for p in self._trace if p[0] >= cutoff]
                    self._render_status(status)
                self._render_history()
        except Exception as exc:
            self.log(f"status poll failed: {exc}")
            self.disconnect()
        finally:
            self.after(STATUS_POLL_MS, self._poll_status)

    def _render_status(self, status):
        if status is None:
            return
        for _label, key in self.STATUS_FIELDS:
            self.status_vars[key].set(str(getattr(status, key)))
        self.adc_current_var.set(
            f"{stm_control.STM_Status.adc_to_amp(status.adc) * 1e9:.2f} nA")
        for key, control in self.dac_controls.items():
            control.update_device_value(getattr(status, key, None))

    def _render_history(self):
        if not self._trace:
            return
        t0 = self._trace[0][0]
        times = [p[0] - t0 for p in self._trace]
        current = [stm_control.STM_Status.adc_to_amp(p[1]) * 1e9 for p in self._trace]
        adc = [p[1] for p in self._trace]
        steps = [p[2] for p in self._trace]
        self.current_plot.update_plot(times, current)
        self.adc_plot.update_plot(times, adc)
        self.steps_plot.update_plot(times, steps)
        x0, x1 = times[0], times[-1]
        if x1 > x0:
            self.current_plot.figure.get_axes()[0].set_xlim(x0, x1)
            self.adc_plot.figure.get_axes()[0].set_xlim(x0, x1)

    def _poll_images(self):
        busy = self.stm.busy
        if busy or self._image_dirty or self._was_busy:
            x_start, x_end, _x_res, y_start, y_end, _y_res = self.stm.scan_config
            extent = [y_start, y_end, x_start, x_end]
            self.adc_image.update_image(self.stm.scan_adc, extent=extent)
            self.z_image.update_image(self.stm.scan_dacz, extent=extent)
            self._image_dirty = False
        self._was_busy = busy
        self.after(IMAGE_POLL_MS, self._poll_images)

    # ---------------------------------------------------------------- teardown

    def _on_close(self):
        self._status_live = False
        self.stm.busy = False
        if getattr(self.stm, "is_opened", False):
            self.stm.stop()
        self.stm.close()
        self.quit()
        self.destroy()


if __name__ == "__main__":
    print("starting Panda STM...")
    app = App()
    print("window ready")
    app.mainloop()
