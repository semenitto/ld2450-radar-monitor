from __future__ import annotations

import math
import queue
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass
from tkinter import ttk

import serial
from serial.tools import list_ports


APP_TITLE = "LD2450 Monitor"
SERIAL_BAUD = 115200
PREFIX = "LD2450_DATA"
RANGE_MM = 6000
COLORS = ("#35e5ff", "#ff5c93", "#ffc857")


@dataclass(frozen=True)
class Target:
    valid: bool
    x: int
    y: int
    speed: int
    resolution: int

    @property
    def distance(self) -> float:
        return math.hypot(self.x, self.y)


@dataclass(frozen=True)
class RadarFrame:
    uptime_ms: int
    counter: int
    targets: tuple[Target, Target, Target]


def parse_data_line(line: str) -> RadarFrame | None:
    fields = line.strip().split(",")
    if len(fields) != 18 or fields[0] != PREFIX:
        return None
    try:
        values = [int(value) for value in fields[1:]]
    except ValueError:
        return None

    targets = []
    for index in range(3):
        offset = 2 + index * 5
        targets.append(
            Target(
                valid=bool(values[offset]),
                x=values[offset + 1],
                y=values[offset + 2],
                speed=values[offset + 3],
                resolution=values[offset + 4],
            )
        )
    return RadarFrame(values[0], values[1], tuple(targets))


class SerialReader:
    def __init__(self, events: queue.Queue):
        self.events = events
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.serial_port: serial.Serial | None = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, port: str) -> None:
        self.stop()
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, args=(port,), daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.serial_port is not None:
            try:
                self.serial_port.close()
            except serial.SerialException:
                pass
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=0.8)
        self.thread = None
        self.serial_port = None

    def _run(self, port: str) -> None:
        try:
            connection = serial.Serial()
            connection.port = port
            connection.baudrate = SERIAL_BAUD
            connection.timeout = 0.25
            connection.write_timeout = 0.25
            connection.dtr = False
            connection.rts = False
            connection.open()
            self.serial_port = connection
            self.events.put(("status", f"Порт {port} открыт, ожидаю данные радара…"))

            while not self.stop_event.is_set():
                raw = connection.readline()
                if not raw:
                    continue
                frame = parse_data_line(raw.decode("ascii", errors="ignore"))
                if frame is not None:
                    self.events.put(("frame", frame))
        except (serial.SerialException, OSError) as exc:
            if not self.stop_event.is_set():
                self.events.put(("error", f"Ошибка порта: {exc}"))
        finally:
            if self.serial_port is not None:
                try:
                    self.serial_port.close()
                except serial.SerialException:
                    pass
            self.serial_port = None


class MonitorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1080x720")
        self.root.minsize(800, 560)
        self.root.configure(bg="#071014")

        self.events: queue.Queue = queue.Queue()
        self.reader = SerialReader(self.events)
        self.latest_frame: RadarFrame | None = None
        self.last_frame_monotonic = 0.0
        self.last_counter = -1
        self.trails = [deque(maxlen=35) for _ in range(3)]
        self.port_values: dict[str, str] = {}
        self.preferred_port: str | None = None
        self.connected = False
        self.canvas_size = (0, 0)
        self.dynamic_dirty = True

        self._configure_styles()
        self._build_ui()
        self.refresh_ports()
        self.root.after(450, self._auto_connect)
        self._process_events()
        self._render()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_styles(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground="#102129", background="#102129", foreground="#e9f7fb")
        style.configure("Accent.TButton", background="#35e5ff", foreground="#041014", padding=(14, 8), font=("Segoe UI", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#7af0ff"), ("disabled", "#31515d")])
        style.configure("Quiet.TButton", background="#152832", foreground="#dbeef3", padding=(11, 8))

    def _build_ui(self) -> None:
        top = tk.Frame(self.root, bg="#071014")
        top.pack(fill="x", padx=20, pady=(18, 12))
        title_box = tk.Frame(top, bg="#071014")
        title_box.pack(side="left", fill="x", expand=True)
        tk.Label(title_box, text="LD2450 · USB-монитор", bg="#071014", fg="#e9f7fb", font=("Segoe UI", 22, "bold")).pack(anchor="w")
        tk.Label(title_box, text="Карта целей через ESP32 · без переключения Wi‑Fi", bg="#071014", fg="#89a4af", font=("Segoe UI", 10)).pack(anchor="w")

        controls = tk.Frame(top, bg="#071014")
        controls.pack(side="right")
        self.port_box = ttk.Combobox(controls, width=33, state="readonly")
        self.port_box.grid(row=0, column=0, padx=(0, 8))
        self.refresh_button = ttk.Button(controls, text="Обновить", style="Quiet.TButton", command=self.refresh_ports)
        self.refresh_button.grid(row=0, column=1, padx=(0, 8))
        self.connect_button = ttk.Button(controls, text="Подключить", style="Accent.TButton", command=self.toggle_connection)
        self.connect_button.grid(row=0, column=2)

        body = tk.Frame(self.root, bg="#071014")
        body.pack(fill="both", expand=True, padx=20, pady=(0, 14))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)

        canvas_panel = tk.Frame(body, bg="#0d1a20", highlightbackground="#1d343e", highlightthickness=1)
        canvas_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        self.canvas = tk.Canvas(canvas_panel, bg="#0a151a", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        sidebar = tk.Frame(body, bg="#071014", width=285)
        sidebar.grid(row=0, column=1, sticky="ns")
        sidebar.grid_propagate(False)
        self.target_vars = []
        for index, color in enumerate(COLORS):
            card = tk.Frame(sidebar, bg="#0d1a20", height=118,
                            highlightbackground="#1d343e", highlightthickness=1)
            card.pack(fill="x", pady=(0, 10))
            card.pack_propagate(False)
            tk.Frame(card, bg=color, width=4, height=124).pack(side="left", fill="y")
            inner = tk.Frame(card, bg="#0d1a20")
            inner.pack(fill="both", expand=True, padx=13, pady=11)
            tk.Label(inner, text=f"Цель {index + 1}", bg="#0d1a20", fg=color, font=("Segoe UI", 10, "bold")).pack(anchor="w")
            coordinate = tk.StringVar(value="X —   Y —")
            motion = tk.StringVar(value="Скорость —   Дистанция —")
            tk.Label(inner, textvariable=coordinate, width=29, anchor="w",
                     bg="#0d1a20", fg="#e9f7fb",
                     font=("Consolas", 10, "bold")).pack(anchor="w", pady=(9, 2))
            tk.Label(inner, textvariable=motion, width=42, anchor="w",
                     bg="#0d1a20", fg="#89a4af",
                     font=("Consolas", 8)).pack(anchor="w")
            self.target_vars.append((coordinate, motion))

        info = tk.Frame(sidebar, bg="#0d1a20", highlightbackground="#1d343e", highlightthickness=1)
        info.pack(fill="x")
        self.frame_var = tk.StringVar(value="Кадры: —")
        self.active_var = tk.StringVar(value="Активные цели: 0")
        tk.Label(info, textvariable=self.active_var, bg="#0d1a20", fg="#e9f7fb", font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=13, pady=(11, 3))
        tk.Label(info, textvariable=self.frame_var, bg="#0d1a20", fg="#89a4af", font=("Segoe UI", 9)).pack(anchor="w", padx=13, pady=(0, 11))

        status = tk.Frame(self.root, bg="#0d1a20", height=38)
        status.pack(fill="x", side="bottom")
        self.status_dot = tk.Label(status, text="●", bg="#0d1a20", fg="#72838a", font=("Segoe UI", 13))
        self.status_dot.pack(side="left", padx=(20, 7), pady=7)
        self.status_var = tk.StringVar(value="ESP32 не подключена")
        tk.Label(status, textvariable=self.status_var, bg="#0d1a20", fg="#c7dce4", font=("Segoe UI", 9)).pack(side="left", pady=7)

    def refresh_ports(self) -> None:
        previous = self.port_values.get(self.port_box.get())
        ports = sorted(list_ports.comports(), key=lambda item: item.device)
        self.port_values = {}
        self.preferred_port = None
        preferred_label = None
        for port in ports:
            label = f"{port.device} — {port.description}"
            self.port_values[label] = port.device
            if port.vid == 0x10C4 and port.pid == 0xEA60:
                preferred_label = label
                self.preferred_port = port.device
            elif "CP210" in (port.description or "").upper() and preferred_label is None:
                preferred_label = label
                self.preferred_port = port.device
        labels = list(self.port_values)
        self.port_box["values"] = labels
        if previous:
            for label, device in self.port_values.items():
                if device == previous:
                    self.port_box.set(label)
                    break
        elif preferred_label:
            self.port_box.set(preferred_label)
        elif labels:
            self.port_box.set(labels[0])
        else:
            self.port_box.set("")
            self.status_var.set("COM-порты не найдены")

    def _auto_connect(self) -> None:
        if self.preferred_port and not self.connected and not self.reader.running:
            self.toggle_connection()

    def toggle_connection(self) -> None:
        if self.connected or self.reader.running:
            self.reader.stop()
            self.connected = False
            self.connect_button.configure(text="Подключить")
            self.port_box.configure(state="readonly")
            self.status_var.set("Отключено")
            self.status_dot.configure(fg="#72838a")
            return
        port = self.port_values.get(self.port_box.get())
        if not port:
            self.status_var.set("Выберите COM-порт")
            return
        self.reader.start(port)
        self.connected = True
        self.connect_button.configure(text="Отключить")
        self.port_box.configure(state="disabled")
        self.status_var.set(f"Открываю {port}…")
        self.status_dot.configure(fg="#ffc857")

    def _process_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "frame":
                    self._accept_frame(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                    self.status_dot.configure(fg="#ffc857")
                elif kind == "error":
                    self.connected = False
                    self.connect_button.configure(text="Подключить")
                    self.port_box.configure(state="readonly")
                    self.status_var.set(payload)
                    self.status_dot.configure(fg="#ff6b6b")
        except queue.Empty:
            pass

        if self.connected and self.last_frame_monotonic:
            age = time.monotonic() - self.last_frame_monotonic
            if age < 1.5:
                self.status_var.set(f"Радар передаёт данные · кадр {self.last_counter}")
                self.status_dot.configure(fg="#55ef9f")
            else:
                self.status_var.set("COM-порт открыт, но свежих данных радара нет")
                self.status_dot.configure(fg="#ff6b6b")
        self.root.after(50, self._process_events)

    def _accept_frame(self, frame: RadarFrame) -> None:
        self.latest_frame = frame
        self.last_frame_monotonic = time.monotonic()
        if frame.counter != self.last_counter:
            for index, target in enumerate(frame.targets):
                if target.valid:
                    self.trails[index].append((target.x, target.y))
            self.last_counter = frame.counter
        self.dynamic_dirty = True

        active = 0
        for target, variables in zip(frame.targets, self.target_vars):
            coordinate, motion = variables
            if target.valid:
                active += 1
                coordinate.set(f"X {target.x / 1000:+.2f} м   Y {target.y / 1000:.2f} м")
                motion.set(f"Скорость {target.speed:+d} см/с   Дистанция {target.distance / 1000:.2f} м")
            else:
                coordinate.set("X —   Y —")
                motion.set("Скорость —   Дистанция —")
        self.active_var.set(f"Активные цели: {active}")
        self.frame_var.set(f"Кадры: {frame.counter:,}".replace(",", " "))

    def _render(self) -> None:
        canvas = self.canvas
        width = max(canvas.winfo_width(), 300)
        height = max(canvas.winfo_height(), 300)
        origin_x = width / 2
        origin_y = height - 28
        scale = min((width - 46) / (RANGE_MM * 2), (height - 62) / RANGE_MM)

        size = (int(width), int(height))
        if size != self.canvas_size:
            self.canvas_size = size
            self._draw_static_grid(width, height, origin_x, origin_y, scale)
            self.dynamic_dirty = True

        if not self.dynamic_dirty:
            self.root.after(50, self._render)
            return

        canvas.delete("dynamic")

        for index, trail in enumerate(self.trails):
            if len(trail) > 1:
                coords = []
                for x, y in trail:
                    coords.extend((origin_x + x * scale, origin_y - y * scale))
                canvas.create_line(*coords, fill=COLORS[index], width=2,
                                   smooth=True, tags="dynamic")

        if self.latest_frame:
            for index, target in enumerate(self.latest_frame.targets):
                if not target.valid:
                    continue
                x = origin_x + target.x * scale
                y = origin_y - target.y * scale
                if 0 <= x <= width and 0 <= y <= height:
                    canvas.create_oval(x - 13, y - 13, x + 13, y + 13,
                                       fill="", outline=COLORS[index], width=1,
                                       tags="dynamic")
                    canvas.create_oval(x - 6, y - 6, x + 6, y + 6,
                                       fill=COLORS[index], outline="", tags="dynamic")
                    canvas.create_text(x + 12, y - 10, text=str(index + 1),
                                       fill="#e9f7fb", anchor="sw",
                                       font=("Segoe UI", 9, "bold"), tags="dynamic")
        self.dynamic_dirty = False
        self.root.after(50, self._render)

    def _draw_static_grid(self, width: float, height: float,
                          origin_x: float, origin_y: float, scale: float) -> None:
        canvas = self.canvas
        canvas.delete("static")
        points = [(origin_x, origin_y)]
        for angle in range(-60, 61, 3):
            radians = math.radians(angle)
            points.append((origin_x + math.sin(radians) * RANGE_MM * scale,
                           origin_y - math.cos(radians) * RANGE_MM * scale))
        canvas.create_polygon(points, fill="#0d2933", outline="", tags="static")

        grid = "#1d3b46"
        for metres in range(1, 7):
            radius = metres * 1000 * scale
            canvas.create_arc(origin_x - radius, origin_y - radius,
                              origin_x + radius, origin_y + radius,
                              start=30, extent=120, style="arc", outline=grid,
                              tags="static")
            canvas.create_text(origin_x + 7, origin_y - radius + 12,
                               text=f"{metres} м", fill="#6f929e", anchor="w",
                               font=("Segoe UI", 8), tags="static")
        for angle in (-60, -30, 0, 30, 60):
            radians = math.radians(angle)
            canvas.create_line(origin_x, origin_y,
                               origin_x + math.sin(radians) * RANGE_MM * scale,
                               origin_y - math.cos(radians) * RANGE_MM * scale,
                               fill=grid, tags="static")

        canvas.create_polygon(origin_x, origin_y - 11,
                              origin_x - 8, origin_y + 5,
                              origin_x + 8, origin_y + 5,
                              fill="#35e5ff", outline="", tags="static")
        canvas.create_text(14, 14,
                           text="Радар внизу · X поперёк · Y вперёд · сетка 1 м",
                           fill="#89a4af", anchor="nw", font=("Segoe UI", 9),
                           tags="static")

    def _on_close(self) -> None:
        self.reader.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    MonitorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
