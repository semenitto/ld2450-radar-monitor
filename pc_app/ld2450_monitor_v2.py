from __future__ import annotations

import math
import json
import os
import queue
import sqlite3
import sys
import threading
import time
import tkinter as tk
import ctypes
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import ttk

import serial
from serial.tools import list_ports


APP_TITLE = "LD2450 Monitor"
SERIAL_BAUD = 115200
PREFIX = "LD2450_DATA"
RANGE_MM = 6000
HISTORY_DAYS = 30

# Palette selected by the user. Supporting surfaces are darker/lighter shades
# derived from these six source colours.
GRAPHITE = "#213843"
MUTED_TEAL = "#468D8B"
SAGE = "#74B3A8"
CREAM = "#F6DAC0"
APRICOT = "#FEAF76"
CORAL = "#DA6D58"

BG = "#10242C"
PANEL = "#172E36"
PANEL_ALT = "#1C333B"
BORDER = "#38545B"
GRID = "#345059"
MUTED_TEXT = "#A9B8AA"
DIM_TEXT = "#78918D"
ZONE_FILLS = ("#17363B", "#29433F", "#413B33")
TARGET_COLORS = (SAGE, APRICOT, CORAL)
ZONE_NAMES = ("Вход", "Рабочая зона", "Диван")
ZONE_COLORS = (MUTED_TEAL, SAGE, APRICOT)
ZONE_BOUNDS = ((-60.0, -25.0), (-25.0, 25.0), (25.0, 60.0))
RUS_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _windows_colorref(hex_color: str) -> int:
    """Convert #RRGGBB to the COLORREF byte order used by Windows."""
    red = int(hex_color[1:3], 16)
    green = int(hex_color[3:5], 16)
    blue = int(hex_color[5:7], 16)
    return red | (green << 8) | (blue << 16)


def apply_windows_chrome(root: tk.Tk) -> None:
    """Tint the native Windows frame while keeping resize, snap and accessibility."""
    if sys.platform != "win32":
        return
    try:
        root.update_idletasks()
        child_handle = root.winfo_id()
        window_handle = ctypes.windll.user32.GetParent(child_handle) or child_handle
        dwm = ctypes.windll.dwmapi

        enabled = ctypes.c_int(1)
        for attribute in (20, 19):
            result = dwm.DwmSetWindowAttribute(
                window_handle,
                attribute,
                ctypes.byref(enabled),
                ctypes.sizeof(enabled),
            )
            if result == 0:
                break

        for attribute, color in (
            (34, MUTED_TEAL),
            (35, BG),
            (36, CREAM),
        ):
            value = ctypes.c_uint(_windows_colorref(color))
            dwm.DwmSetWindowAttribute(
                window_handle,
                attribute,
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
    except (AttributeError, OSError, ValueError):
        # Unsupported Windows versions simply keep their native title bar.
        pass


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


@dataclass(frozen=True)
class HistorySample:
    timestamp: int
    occupancy: int
    activity: float
    zones: tuple[int, int, int]


@dataclass
class ZoneRect:
    name: str
    x1: float
    y1: float
    x2: float
    y2: float

    def normalized(self) -> "ZoneRect":
        return ZoneRect(
            self.name,
            min(self.x1, self.x2),
            min(self.y1, self.y2),
            max(self.x1, self.x2),
            max(self.y1, self.y2),
        )

    def contains(self, x: float, y: float, margin: float = 0.0) -> bool:
        rect = self.normalized()
        return (
            rect.x1 - margin <= x <= rect.x2 + margin
            and rect.y1 - margin <= y <= rect.y2 + margin
        )


def default_zones() -> list[ZoneRect]:
    return [
        ZoneRect(ZONE_NAMES[0], -2.7, 0.4, -1.4, 1.6),
        ZoneRect(ZONE_NAMES[1], -1.2, 2.8, 1.2, 5.4),
        ZoneRect(ZONE_NAMES[2], 1.5, 0.7, 2.8, 3.2),
    ]


@dataclass
class CalibrationSettings:
    room_width: float = 6.0
    room_height: float = 6.0
    radar_x: float = 0.0
    radar_y: float = 0.0
    rotation_deg: float = 0.0
    mirror_x: bool = False
    smoothing: float = 0.32
    hysteresis: float = 0.15
    zones: list[ZoneRect] = field(default_factory=default_zones)


def load_calibration(path: Path | None) -> CalibrationSettings:
    if path is None or not path.exists():
        return CalibrationSettings()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        zones = [ZoneRect(**zone).normalized() for zone in payload.get("zones", [])]
        if len(zones) != 3:
            zones = default_zones()
        return CalibrationSettings(
            room_width=float(payload.get("room_width", 6.0)),
            room_height=float(payload.get("room_height", 6.0)),
            radar_x=float(payload.get("radar_x", 0.0)),
            radar_y=float(payload.get("radar_y", 0.0)),
            rotation_deg=float(payload.get("rotation_deg", 0.0)),
            mirror_x=bool(payload.get("mirror_x", False)),
            smoothing=float(payload.get("smoothing", 0.32)),
            hysteresis=float(payload.get("hysteresis", 0.15)),
            zones=zones,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return CalibrationSettings()


def save_calibration(path: Path | None, settings: CalibrationSettings) -> None:
    if path is None:
        return
    payload = {
        "room_width": settings.room_width,
        "room_height": settings.room_height,
        "radar_x": settings.radar_x,
        "radar_y": settings.radar_y,
        "rotation_deg": settings.rotation_deg,
        "mirror_x": settings.mirror_x,
        "smoothing": settings.smoothing,
        "hysteresis": settings.hysteresis,
        "zones": [zone.__dict__ for zone in settings.zones],
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def transform_target(target: Target, settings: CalibrationSettings) -> tuple[float, float]:
    x = target.x / 1000.0
    y = target.y / 1000.0
    if settings.mirror_x:
        x = -x
    angle = math.radians(settings.rotation_deg)
    rotated_x = x * math.cos(angle) - y * math.sin(angle)
    rotated_y = x * math.sin(angle) + y * math.cos(angle)
    return settings.radar_x + rotated_x, settings.radar_y + rotated_y


def zone_index_for_point(
    x: float,
    y: float,
    settings: CalibrationSettings,
    previous_zone: int | None = None,
) -> int | None:
    if previous_zone is not None and settings.zones[previous_zone].contains(
        x, y, settings.hysteresis
    ):
        return previous_zone
    for index, zone in enumerate(settings.zones):
        if zone.contains(x, y):
            return index
    return None


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


def person_count_text(count: int) -> str:
    if count == 1:
        return "1 человек"
    if 2 <= count <= 4:
        return f"{count} человека"
    return f"{count} человек"


def duration_text(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours} ч {minutes:02d} мин"
    return f"{minutes} мин"


def local_date_text(timestamp: float | None = None) -> str:
    moment = datetime.fromtimestamp(timestamp or time.time())
    return f"{moment.day} {RUS_MONTHS[moment.month - 1]} {moment.year}"


def zone_index_for_target(target: Target) -> int | None:
    if not target.valid or target.y <= 0:
        return None
    angle = math.degrees(math.atan2(target.x, target.y))
    for index, (low, high) in enumerate(ZONE_BOUNDS):
        if low <= angle <= high:
            return index
    return None


class HistoryStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS samples (
                ts INTEGER PRIMARY KEY,
                occupancy INTEGER NOT NULL,
                activity REAL NOT NULL,
                zone_entrance INTEGER NOT NULL,
                zone_work INTEGER NOT NULL,
                zone_sofa INTEGER NOT NULL
            )
            """
        )
        cutoff = int(time.time()) - HISTORY_DAYS * 86400
        self.connection.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        self.connection.commit()

    def append(self, sample: HistorySample) -> None:
        self.connection.execute(
            """
            INSERT OR REPLACE INTO samples
            (ts, occupancy, activity, zone_entrance, zone_work, zone_sofa)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (sample.timestamp, sample.occupancy, sample.activity, *sample.zones),
        )
        self.connection.commit()

    def append_many(self, samples: list[HistorySample]) -> None:
        self.connection.executemany(
            """
            INSERT OR REPLACE INTO samples
            (ts, occupancy, activity, zone_entrance, zone_work, zone_sofa)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (sample.timestamp, sample.occupancy, sample.activity, *sample.zones)
                for sample in samples
            ],
        )
        self.connection.commit()

    def query(self, start_ts: int, end_ts: int) -> list[HistorySample]:
        rows = self.connection.execute(
            """
            SELECT ts, occupancy, activity, zone_entrance, zone_work, zone_sofa
            FROM samples WHERE ts BETWEEN ? AND ? ORDER BY ts
            """,
            (start_ts, end_ts),
        ).fetchall()
        return [
            HistorySample(row[0], row[1], row[2], (row[3], row[4], row[5]))
            for row in rows
        ]

    def today_stats(self, now_ts: int) -> tuple[list[int], list[int], int]:
        now = datetime.fromtimestamp(now_ts)
        start = int(datetime(now.year, now.month, now.day).timestamp())
        occupancy_seconds = [0, 0, 0, 0]
        for occupancy, count in self.connection.execute(
            "SELECT occupancy, COUNT(*) FROM samples WHERE ts BETWEEN ? AND ? GROUP BY occupancy",
            (start, now_ts),
        ):
            occupancy_seconds[max(0, min(3, int(occupancy)))] = int(count)
        row = self.connection.execute(
            """
            SELECT
                COALESCE(SUM(zone_entrance > 0), 0),
                COALESCE(SUM(zone_work > 0), 0),
                COALESCE(SUM(zone_sofa > 0), 0),
                COUNT(*)
            FROM samples WHERE ts BETWEEN ? AND ?
            """,
            (start, now_ts),
        ).fetchone()
        zone_seconds = [int(row[0]), int(row[1]), int(row[2])]
        return occupancy_seconds, zone_seconds, int(row[3])

    def close(self) -> None:
        self.connection.close()


def migrate_legacy_history(legacy_path: Path, destination: HistoryStore) -> None:
    if not legacy_path.exists():
        return
    legacy: sqlite3.Connection | None = None
    try:
        legacy = sqlite3.connect(str(legacy_path))
        cutoff = int(time.time()) - HISTORY_DAYS * 86400
        rows = legacy.execute(
            "SELECT ts, occupancy, activity FROM samples WHERE ts >= ? ORDER BY ts",
            (cutoff,),
        ).fetchall()
        destination.append_many(
            [HistorySample(int(ts), int(occupancy), float(activity), (0, 0, 0)) for ts, occupancy, activity in rows]
        )
    except sqlite3.Error:
        return
    finally:
        if legacy is not None:
            legacy.close()


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
    def __init__(self, root: tk.Tk, demo: bool = False):
        self.root = root
        self.demo = demo
        self.root.title(f"{APP_TITLE} — прототип" if demo else APP_TITLE)
        self.root.geometry("1120x760")
        self.root.minsize(980, 680)
        self.root.configure(bg=BG)

        self.app_dir: Path | None = None
        self.settings_path: Path | None = None
        if not demo:
            self.app_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LD2450Monitor"
            self.app_dir.mkdir(parents=True, exist_ok=True)
            self.settings_path = self.app_dir / "settings.json"
        self.calibration = load_calibration(self.settings_path)

        self.events: queue.Queue = queue.Queue()
        self.reader = SerialReader(self.events)
        self.latest_frame: RadarFrame | None = None
        self.last_frame_monotonic = 0.0
        self.last_counter = -1
        self.trails = [deque(maxlen=28) for _ in range(3)]
        self.filtered_positions: list[tuple[float, float] | None] = [None, None, None]
        self.current_room_positions: list[tuple[float, float] | None] = [None, None, None]
        self.target_zone_indices: list[int | None] = [None, None, None]
        self.port_values: dict[str, str] = {}
        self.preferred_port: str | None = None
        self.connected = demo
        self.manual_disconnect = False
        self.last_reconnect_attempt = 0.0
        self.canvas_size = (0, 0)
        self.dynamic_dirty = True
        self.period_hours = 6
        self.recording = True
        self.last_history_write = 0
        self.activity_ema = 0.0
        self.previous_occupancy = 0
        self.current_occupancy = 0
        self.current_zone_counts = [0, 0, 0]
        self.timeline_samples: list[HistorySample | None] = []
        self.timeline_start = int(time.time()) - self.period_hours * 3600
        self.timeline_end = int(time.time())
        self.timeline_hover_index: int | None = None
        self.timeline_hover_active = False
        self.timeline_dirty = True
        self.demo_tick = 0
        self.zone_edit_mode = False
        self.selected_zone = 1
        self.zone_drag: dict[str, object] | None = None

        if demo:
            self.history = HistoryStore(":memory:")
        else:
            history_path = self.app_dir / "history_rect.db"
            migrate_history = not history_path.exists()
            self.history = HistoryStore(history_path)
            if migrate_history:
                migrate_legacy_history(self.app_dir / "history.db", self.history)

        self._configure_styles()
        self._build_ui()
        apply_windows_chrome(self.root)
        if demo:
            self._start_demo()
        else:
            self.refresh_ports()
            self.root.after(450, self._auto_connect)
        self._process_events()
        self._render_radar()
        self._refresh_statistics()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_styles(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "TCombobox",
            fieldbackground=PANEL_ALT,
            background=PANEL_ALT,
            foreground=CREAM,
            arrowcolor=CREAM,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=5,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("disabled", PANEL_ALT), ("readonly", PANEL_ALT)],
            foreground=[("disabled", DIM_TEXT), ("readonly", CREAM)],
        )
        style.configure(
            "Accent.TButton",
            background=SAGE,
            foreground=GRAPHITE,
            padding=(14, 8),
            font=("Segoe UI", 9, "bold"),
            bordercolor=CREAM,
        )
        style.map("Accent.TButton", background=[("active", "#91C5B9"), ("disabled", GRAPHITE)])
        style.configure(
            "Quiet.TButton",
            background=PANEL_ALT,
            foreground=CREAM,
            padding=(11, 8),
            bordercolor=BORDER,
        )
        style.map("Quiet.TButton", background=[("active", GRAPHITE)])
        style.configure(
            "Settings.TButton",
            background=PANEL_ALT,
            foreground=CREAM,
            padding=(8, 5),
            font=("Segoe MDL2 Assets", 12),
            bordercolor=PANEL_ALT,
        )

    def _build_ui(self) -> None:
        top = tk.Frame(self.root, bg=BG, height=88)
        top.pack(fill="x", padx=20, pady=(15, 10))
        top.pack_propagate(False)

        title_box = tk.Frame(top, bg=BG)
        title_box.pack(side="left", fill="both", expand=True)
        tk.Label(
            title_box,
            text="LD2450 · USB-монитор",
            bg=BG,
            fg=CREAM,
            font=("Segoe UI", 21, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_box,
            text="Карта целей через ESP32 · без переключения Wi‑Fi",
            bg=BG,
            fg=MUTED_TEXT,
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(4, 0))

        controls = tk.Frame(top, bg=BG)
        controls.pack(side="right", anchor="n", pady=(6, 0))
        self.port_box = ttk.Combobox(controls, width=26, state="readonly")
        self.port_box.grid(row=0, column=0, padx=(0, 8))
        self.refresh_button = ttk.Button(
            controls, text="Обновить", style="Quiet.TButton", command=self.refresh_ports
        )
        self.refresh_button.grid(row=0, column=1, padx=(0, 8))
        self.connect_button = ttk.Button(
            controls, text="Подключить", style="Accent.TButton", command=self.toggle_connection
        )
        self.connect_button.grid(row=0, column=2)

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=7, minsize=300)
        body.grid_rowconfigure(1, weight=4, minsize=188)

        upper = tk.Frame(body, bg=BG)
        upper.grid(row=0, column=0, sticky="nsew")
        upper.grid_columnconfigure(0, weight=1)
        upper.grid_rowconfigure(0, weight=1)

        radar_panel = tk.Frame(
            upper, bg=PANEL, highlightbackground=BORDER, highlightthickness=1
        )
        radar_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        map_header = tk.Frame(radar_panel, bg=PANEL, height=48)
        map_header.pack(fill="x", padx=12, pady=(8, 2))
        map_header.pack_propagate(False)
        map_title = tk.Frame(map_header, bg=PANEL)
        map_title.pack(side="left", fill="both", expand=True)
        tk.Label(
            map_title,
            text="План помещения",
            bg=PANEL,
            fg=CREAM,
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w")
        self.room_size_var = tk.StringVar()
        self._update_room_size_text()
        tk.Label(
            map_title,
            textvariable=self.room_size_var,
            bg=PANEL,
            fg=MUTED_TEXT,
            font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(2, 0))
        self.zone_edit_button = ttk.Button(
            map_header,
            text="Редактировать зоны",
            style="Quiet.TButton",
            command=self.toggle_zone_editing,
        )
        self.zone_edit_button.pack(side="right", pady=3)

        self.canvas = tk.Canvas(radar_panel, bg=PANEL, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._zone_pointer_down)
        self.canvas.bind("<B1-Motion>", self._zone_pointer_move)
        self.canvas.bind("<ButtonRelease-1>", self._zone_pointer_up)

        sidebar = tk.Frame(
            upper,
            bg=PANEL,
            width=315,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        sidebar.grid(row=0, column=1, sticky="ns")
        sidebar.grid_propagate(False)
        self._build_sidebar(sidebar)

        timeline_panel = tk.Frame(
            body, bg=PANEL, highlightbackground=BORDER, highlightthickness=1
        )
        timeline_panel.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self._build_timeline(timeline_panel)

        status = tk.Frame(self.root, bg=PANEL_ALT, height=39)
        status.pack(fill="x", side="bottom")
        status.pack_propagate(False)

        self.status_dot = tk.Label(
            status, text="●", bg=PANEL_ALT, fg=DIM_TEXT, font=("Segoe UI", 12)
        )
        self.status_dot.pack(side="left", padx=(20, 7), pady=7)
        self.status_var = tk.StringVar(value="ESP32 не подключена")
        tk.Label(
            status,
            textvariable=self.status_var,
            bg=PANEL_ALT,
            fg=CREAM,
            font=("Segoe UI", 9),
        ).pack(side="left", pady=7)

        self.settings_button = tk.Button(
            status,
            text="\uE713",
            width=3,
            bg=PANEL_ALT,
            fg=CREAM,
            activebackground=GRAPHITE,
            activeforeground=CREAM,
            relief="flat",
            bd=0,
            font=("Segoe MDL2 Assets", 12),
            cursor="hand2",
            command=self.open_settings,
        )
        self.settings_button.pack(side="right", padx=(4, 12), pady=4)
        self.clock_var = tk.StringVar()
        tk.Label(
            status,
            textvariable=self.clock_var,
            bg=PANEL_ALT,
            fg=MUTED_TEXT,
            font=("Segoe UI", 9),
        ).pack(side="right", padx=10)
        self.recording_button = tk.Button(
            status,
            text="●  Запись включена",
            bg=PANEL_ALT,
            fg=CORAL,
            activebackground=PANEL_ALT,
            activeforeground=APRICOT,
            relief="flat",
            bd=0,
            font=("Segoe UI", 9),
            cursor="hand2",
            command=self.toggle_recording,
        )
        self.recording_button.pack(side="right", padx=10)

    def _build_sidebar(self, parent: tk.Frame) -> None:
        inner = tk.Frame(parent, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=14, pady=12)

        self.occupancy_var = tk.StringVar(value="В зоне сейчас: 0 человек")
        tk.Label(
            inner,
            textvariable=self.occupancy_var,
            bg=PANEL,
            fg=CREAM,
            font=("Segoe UI", 15, "bold"),
        ).pack(anchor="w")
        self.sidebar_time_var = tk.StringVar()
        tk.Label(
            inner,
            textvariable=self.sidebar_time_var,
            bg=PANEL,
            fg=MUTED_TEXT,
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(3, 10))

        header = tk.Frame(inner, bg=PANEL)
        header.pack(fill="x")
        header.grid_columnconfigure(0, weight=1)
        for column, text, anchor in (
            (0, "Зона", "w"), (1, "Сейчас", "center"), (2, "Время сегодня", "e")
        ):
            tk.Label(
                header,
                text=text,
                bg=PANEL,
                fg=MUTED_TEXT,
                font=("Segoe UI", 8),
                anchor=anchor,
            ).grid(row=0, column=column, sticky="ew")

        self.zone_count_vars: list[tk.StringVar] = []
        self.zone_time_vars: list[tk.StringVar] = []
        for index, (name, color) in enumerate(zip(ZONE_NAMES, ZONE_COLORS)):
            row = tk.Frame(inner, bg=PANEL, height=32)
            row.pack(fill="x")
            row.pack_propagate(False)
            tk.Frame(row, width=8, height=8, bg=color).pack(side="left", padx=(1, 8))
            tk.Label(
                row, text=name, bg=PANEL, fg=CREAM, font=("Segoe UI", 9), anchor="w"
            ).pack(side="left", fill="x", expand=True)
            count_var = tk.StringVar(value="0")
            time_var = tk.StringVar(value="0 мин")
            tk.Label(
                row,
                textvariable=count_var,
                width=4,
                bg=PANEL,
                fg=CREAM,
                font=("Consolas", 9),
            ).pack(side="left")
            tk.Label(
                row,
                textvariable=time_var,
                width=11,
                anchor="e",
                bg=PANEL,
                fg=CREAM,
                font=("Segoe UI", 8),
            ).pack(side="right")
            self.zone_count_vars.append(count_var)
            self.zone_time_vars.append(time_var)
            tk.Frame(inner, height=1, bg=BORDER).pack(fill="x")

        tk.Label(
            inner,
            text="Заполненность сегодня (по времени записи)",
            bg=PANEL,
            fg=CREAM,
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w", pady=(12, 4))

        self.occupancy_canvas = tk.Canvas(
            inner, height=112, bg=PANEL, highlightthickness=0
        )
        self.occupancy_canvas.pack(fill="x")
        self.occupancy_canvas.bind("<Configure>", lambda _event: self._draw_occupancy_summary())
        self.today_occupancy_seconds = [0, 0, 0, 0]

    def _build_timeline(self, parent: tk.Frame) -> None:
        controls = tk.Frame(parent, bg=PANEL, height=38)
        controls.pack(fill="x", padx=12, pady=(6, 0))
        controls.pack_propagate(False)
        tk.Label(
            controls, text="Период:", bg=PANEL, fg=CREAM, font=("Segoe UI", 9)
        ).pack(side="left", padx=(0, 7), pady=5)

        self.period_buttons: dict[int, tk.Button] = {}
        for hours, label in ((1, "1 ч"), (6, "6 ч"), (24, "24 ч")):
            button = tk.Button(
                controls,
                text=label,
                width=6,
                bg=PANEL_ALT,
                fg=CREAM,
                activebackground=GRAPHITE,
                activeforeground=CREAM,
                highlightbackground=BORDER,
                highlightthickness=1,
                relief="flat",
                bd=0,
                font=("Segoe UI", 9),
                cursor="hand2",
                command=lambda selected=hours: self.set_period(selected),
            )
            button.pack(side="left", padx=(0, 6), pady=3)
            self.period_buttons[hours] = button

        self.range_var = tk.StringVar()
        tk.Label(
            controls,
            textvariable=self.range_var,
            bg=PANEL,
            fg=MUTED_TEXT,
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(8, 0), pady=6)
        self.timeline_event_var = tk.StringVar()
        self.timeline_event_label = tk.Label(
            controls,
            textvariable=self.timeline_event_var,
            bg=PANEL_ALT,
            fg=CREAM,
            font=("Segoe UI", 8),
            padx=8,
            pady=4,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        self.timeline_event_label.pack(side="right", pady=3)

        self.timeline_canvas = tk.Canvas(
            parent, height=140, bg=PANEL, highlightthickness=0, cursor="crosshair"
        )
        self.timeline_canvas.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.timeline_canvas.bind("<Configure>", lambda _event: self._draw_timeline())
        self.timeline_canvas.bind("<Motion>", self._timeline_motion)
        self.timeline_canvas.bind("<Leave>", self._timeline_leave)
        self.set_period(6)

    def refresh_ports(self) -> None:
        if self.demo:
            return
        previous = self.port_values.get(self.port_box.get())
        ports = sorted(list_ports.comports(), key=lambda item: item.device)
        self.port_values = {}
        self.preferred_port = None
        preferred_label = None
        for port in ports:
            description = port.description or "USB Serial"
            short_description = "CP210x USB" if "CP210" in description.upper() else description[:24]
            label = f"{port.device} — {short_description}"
            self.port_values[label] = port.device
            if port.vid == 0x10C4 and port.pid == 0xEA60:
                preferred_label = label
                self.preferred_port = port.device
            elif "CP210" in description.upper() and preferred_label is None:
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
        if self.demo or self.manual_disconnect:
            return
        if self.preferred_port and not self.connected and not self.reader.running:
            self.toggle_connection()

    def toggle_connection(self) -> None:
        if self.demo:
            return
        if self.connected or self.reader.running:
            self.reader.stop()
            self.connected = False
            self.manual_disconnect = True
            self.connect_button.configure(text="Подключить")
            self.port_box.configure(state="readonly")
            self.status_var.set("Отключено пользователем")
            self.status_dot.configure(fg=DIM_TEXT)
            return
        port = self.port_values.get(self.port_box.get())
        if not port:
            self.status_var.set("Выберите COM-порт")
            return
        self.manual_disconnect = False
        self.last_reconnect_attempt = time.monotonic()
        self.reader.start(port)
        self.connected = True
        self.connect_button.configure(text="Отключить")
        self.port_box.configure(state="disabled")
        self.status_var.set(f"Открываю {port}…")
        self.status_dot.configure(fg=APRICOT)

    def toggle_recording(self) -> None:
        self.recording = not self.recording
        if self.recording:
            self.recording_button.configure(text="●  Запись включена", fg=CORAL)
        else:
            self.recording_button.configure(text="○  Запись на паузе", fg=DIM_TEXT)

    def set_period(self, hours: int) -> None:
        self.period_hours = hours
        for value, button in self.period_buttons.items():
            if value == hours:
                button.configure(bg=SAGE, fg=GRAPHITE, highlightbackground=CREAM)
            else:
                button.configure(bg=PANEL_ALT, fg=CREAM, highlightbackground=BORDER)
        self.timeline_hover_index = None
        self.timeline_dirty = True
        if hasattr(self, "history"):
            self._load_timeline_data()

    def _process_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "frame":
                    self._accept_frame(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                    self.status_dot.configure(fg=APRICOT)
                elif kind == "error":
                    self.connected = False
                    self.connect_button.configure(text="Подключить")
                    self.port_box.configure(state="readonly")
                    self.status_var.set(f"{payload} · повтор через 3 с")
                    self.status_dot.configure(fg=CORAL)
        except queue.Empty:
            pass

        if self.connected and self.last_frame_monotonic:
            age = time.monotonic() - self.last_frame_monotonic
            if age < 1.5:
                self.status_var.set(f"Радар передаёт данные · кадр {self.last_counter}")
                self.status_dot.configure(fg=SAGE)
            else:
                self.status_var.set(f"Нет свежих данных · последний кадр {age:.1f} с назад")
                self.status_dot.configure(fg=CORAL)
        elif (
            not self.demo
            and not self.connected
            and not self.reader.running
            and not self.manual_disconnect
            and self.preferred_port
            and time.monotonic() - self.last_reconnect_attempt > 3.0
        ):
            self.last_reconnect_attempt = time.monotonic()
            self.toggle_connection()
        self.root.after(50, self._process_events)

    def _accept_frame(self, frame: RadarFrame) -> None:
        self.latest_frame = frame
        self.last_frame_monotonic = time.monotonic()
        zone_counts = [0, 0, 0]
        speed_total = 0.0
        occupancy = 0
        room_half_width = self.calibration.room_width / 2

        for index, target in enumerate(frame.targets):
            if not target.valid:
                self.filtered_positions[index] = None
                self.current_room_positions[index] = None
                self.target_zone_indices[index] = None
                continue

            measured_x, measured_y = transform_target(target, self.calibration)
            previous = self.filtered_positions[index]
            alpha = max(0.05, min(1.0, self.calibration.smoothing))
            if previous is None:
                filtered = (measured_x, measured_y)
            else:
                filtered = (
                    previous[0] + (measured_x - previous[0]) * alpha,
                    previous[1] + (measured_y - previous[1]) * alpha,
                )
            self.filtered_positions[index] = filtered

            in_room = (
                -room_half_width <= filtered[0] <= room_half_width
                and 0.0 <= filtered[1] <= self.calibration.room_height
            )
            if not in_room:
                self.current_room_positions[index] = None
                self.target_zone_indices[index] = None
                continue

            self.current_room_positions[index] = filtered
            occupancy += 1
            speed_total += abs(target.speed)
            zone_index = zone_index_for_point(
                filtered[0],
                filtered[1],
                self.calibration,
                self.target_zone_indices[index],
            )
            self.target_zone_indices[index] = zone_index
            if zone_index is not None:
                zone_counts[zone_index] += 1

            if frame.counter != self.last_counter:
                self.trails[index].append(filtered)

        if frame.counter != self.last_counter:
            self.last_counter = frame.counter

        change_bonus = 22.0 if occupancy != self.previous_occupancy else 0.0
        raw_activity = min(100.0, speed_total * 1.35 + change_bonus)
        self.activity_ema = self.activity_ema * 0.72 + raw_activity * 0.28
        self.previous_occupancy = occupancy
        self.current_occupancy = occupancy
        self.current_zone_counts = zone_counts

        now_second = int(time.time())
        if self.recording and now_second != self.last_history_write:
            self.history.append(
                HistorySample(now_second, occupancy, self.activity_ema, tuple(zone_counts))
            )
            self.last_history_write = now_second
            self.timeline_dirty = True

        self.dynamic_dirty = True
        self._update_live_text()

    def _update_live_text(self) -> None:
        self.occupancy_var.set(f"В зоне сейчас: {person_count_text(self.current_occupancy)}")
        for index, value in enumerate(self.current_zone_counts):
            self.zone_count_vars[index].set(str(value))

    def _refresh_statistics(self) -> None:
        now_ts = int(time.time())
        now = datetime.fromtimestamp(now_ts)
        self.clock_var.set(now.strftime("%d.%m.%Y, %H:%M:%S"))
        self.sidebar_time_var.set(f"{local_date_text(now_ts)}, {now:%H:%M}")

        occupancy_seconds, zone_seconds, _recorded_seconds = self.history.today_stats(now_ts)
        self.today_occupancy_seconds = occupancy_seconds
        for variable, seconds in zip(self.zone_time_vars, zone_seconds):
            variable.set(duration_text(seconds))
        self._draw_occupancy_summary()

        if self.timeline_dirty or now_ts % 5 == 0:
            self._load_timeline_data()
            self.timeline_dirty = False
        self.root.after(1000, self._refresh_statistics)

    def _draw_occupancy_summary(self) -> None:
        canvas = self.occupancy_canvas
        width = max(canvas.winfo_width(), 260)
        canvas.delete("all")
        seconds = self.today_occupancy_seconds
        total = max(1, sum(seconds))
        colors = (MUTED_TEAL, SAGE, APRICOT, CORAL)
        labels = ("0 чел.", "1 чел.", "2 чел.", "3 чел.")
        margin = 2
        segment_width = (width - margin * 2) / 4
        for index, (label, value, color) in enumerate(zip(labels, seconds, colors)):
            x0 = margin + index * segment_width
            x1 = margin + (index + 1) * segment_width
            percent = round(value * 100 / total)
            canvas.create_text(
                (x0 + x1) / 2, 11, text=label, fill=CREAM, font=("Segoe UI", 8)
            )
            canvas.create_rectangle(x0, 26, x1, 48, fill=color, outline="")
            text_color = GRAPHITE if index in (1, 2) else CREAM
            canvas.create_text(
                (x0 + x1) / 2,
                37,
                text=f"{percent}%",
                fill=text_color,
                font=("Segoe UI", 8, "bold"),
            )
            canvas.create_text(
                (x0 + x1) / 2,
                60,
                text=duration_text(value),
                fill=MUTED_TEXT,
                font=("Segoe UI", 7),
            )

        legend = (
            (CORAL, "Высокая активность (3 чел.)", seconds[3]),
            (APRICOT, "2 человека", seconds[2]),
            (SAGE, "1 человек", seconds[1]),
            (MUTED_TEAL, "0 человек", seconds[0]),
        )
        for row, (color, label, value) in enumerate(legend):
            y = 76 + row * 10
            canvas.create_oval(4, y - 3, 10, y + 3, fill=color, outline="")
            canvas.create_text(16, y, text=label, fill=MUTED_TEXT, anchor="w", font=("Segoe UI", 7))
            canvas.create_text(width - 4, y, text=duration_text(value), fill=CREAM, anchor="e", font=("Segoe UI", 7))

    def _load_timeline_data(self) -> None:
        self.timeline_end = int(time.time())
        self.timeline_start = self.timeline_end - self.period_hours * 3600
        raw_samples = self.history.query(self.timeline_start, self.timeline_end)
        width = max(self.timeline_canvas.winfo_width(), 700)
        bucket_count = max(120, min(420, int((width - 120) / 3)))
        buckets: list[dict | None] = [None] * bucket_count
        span = max(1, self.timeline_end - self.timeline_start)
        for sample in raw_samples:
            index = min(bucket_count - 1, int((sample.timestamp - self.timeline_start) * bucket_count / span))
            bucket = buckets[index]
            if bucket is None:
                buckets[index] = {
                    "timestamp": sample.timestamp,
                    "occupancy": sample.occupancy,
                    "activity": sample.activity,
                    "zones": list(sample.zones),
                }
            else:
                bucket["timestamp"] = sample.timestamp
                bucket["occupancy"] = sample.occupancy
                bucket["activity"] = max(bucket["activity"], sample.activity)
                bucket["zones"] = [max(a, b) for a, b in zip(bucket["zones"], sample.zones)]
        self.timeline_samples = [
            None
            if bucket is None
            else HistorySample(
                int(bucket["timestamp"]),
                int(bucket["occupancy"]),
                float(bucket["activity"]),
                tuple(int(value) for value in bucket["zones"]),
            )
            for bucket in buckets
        ]
        start = datetime.fromtimestamp(self.timeline_start)
        end = datetime.fromtimestamp(self.timeline_end)
        self.range_var.set(
            f"Диапазон: {local_date_text(self.timeline_end)}, {start:%H:%M}–{end:%H:%M}"
        )
        if not self.timeline_hover_active:
            valid = [(index, sample.activity) for index, sample in enumerate(self.timeline_samples) if sample]
            self.timeline_hover_index = max(valid, key=lambda item: item[1])[0] if valid else None
        self._update_timeline_event_text()
        self._draw_timeline()

    def _timeline_geometry(self) -> tuple[float, float, float, float]:
        width = max(self.timeline_canvas.winfo_width(), 700)
        height = max(self.timeline_canvas.winfo_height(), 140)
        return 108.0, width - 10.0, 8.0, height - 24.0

    def _draw_timeline(self) -> None:
        if not hasattr(self, "timeline_canvas"):
            return
        canvas = self.timeline_canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 700)
        height = max(canvas.winfo_height(), 140)
        x0, x1, top, bottom = self._timeline_geometry()
        activity_top = top + 4
        activity_bottom = top + (bottom - top) * 0.42
        occupancy_top = activity_bottom + 12
        occupancy_bottom = top + (bottom - top) * 0.78
        zones_y = top + (bottom - top) * 0.91

        canvas.create_text(4, activity_top, text="Активность", fill=SAGE, anchor="nw", font=("Segoe UI", 8, "bold"))
        canvas.create_text(4, activity_top + 18, text="Интенсивность", fill=MUTED_TEXT, anchor="nw", font=("Segoe UI", 7))
        canvas.create_text(96, activity_top, text="100", fill=CREAM, anchor="ne", font=("Consolas", 7))
        canvas.create_text(96, activity_bottom, text="0", fill=CREAM, anchor="se", font=("Consolas", 7))
        canvas.create_text(4, occupancy_top, text="Заполненность", fill=SAGE, anchor="nw", font=("Segoe UI", 8, "bold"))
        canvas.create_text(4, occupancy_top + 18, text="(человек)", fill=MUTED_TEXT, anchor="nw", font=("Segoe UI", 7))
        canvas.create_text(4, zones_y, text="Активные зоны", fill=CREAM, anchor="w", font=("Segoe UI", 8))

        for level in range(4):
            y = occupancy_bottom - level * (occupancy_bottom - occupancy_top) / 3
            canvas.create_line(x0, y, x1, y, fill=GRID, dash=(2, 3))
            canvas.create_text(96, y, text=str(level), fill=CREAM, anchor="e", font=("Consolas", 7))
        for level in (0, 50, 100):
            y = activity_bottom - level * (activity_bottom - activity_top) / 100
            canvas.create_line(x0, y, x1, y, fill=GRID, dash=(2, 3))

        if not self.timeline_samples or not any(self.timeline_samples):
            canvas.create_text(
                (x0 + x1) / 2,
                (top + bottom) / 2,
                text="История появится после начала записи",
                fill=MUTED_TEXT,
                font=("Segoe UI", 10),
            )
            return

        count = len(self.timeline_samples)
        step = (x1 - x0) / max(1, count - 1)

        activity_points: list[float] = []
        previous_index: int | None = None
        previous_sample: HistorySample | None = None
        occupancy_segments: list[float] = []
        last_zone_signature: tuple[int, int, int] | None = None
        last_zone_x = -100.0

        for index, sample in enumerate(self.timeline_samples):
            if sample is None:
                previous_index = None
                previous_sample = None
                if len(activity_points) >= 4:
                    canvas.create_polygon(
                        activity_points + [activity_points[-2], activity_bottom, activity_points[0], activity_bottom],
                        fill="#243D3C",
                        outline="",
                    )
                activity_points = []
                if len(occupancy_segments) >= 4:
                    canvas.create_line(*occupancy_segments, fill=SAGE, width=2)
                occupancy_segments = []
                continue

            x = x0 + index * step
            activity_y = activity_bottom - min(100.0, sample.activity) * (activity_bottom - activity_top) / 100
            occupancy_y = occupancy_bottom - sample.occupancy * (occupancy_bottom - occupancy_top) / 3
            activity_points.extend((x, activity_y))

            if previous_index is not None and previous_sample is not None:
                previous_x = x0 + previous_index * step
                previous_y = activity_bottom - min(100.0, previous_sample.activity) * (activity_bottom - activity_top) / 100
                if max(sample.activity, previous_sample.activity) >= 65:
                    line_color = CORAL
                elif max(sample.activity, previous_sample.activity) >= 30:
                    line_color = APRICOT
                else:
                    line_color = SAGE
                canvas.create_line(previous_x, previous_y, x, activity_y, fill=line_color, width=2)

                previous_occupancy_y = occupancy_bottom - previous_sample.occupancy * (occupancy_bottom - occupancy_top) / 3
                occupancy_segments.extend((previous_x, previous_occupancy_y, x, previous_occupancy_y, x, occupancy_y))
            else:
                occupancy_segments.extend((x, occupancy_y))

            if any(sample.zones):
                signature = sample.zones
                if sample.activity >= 28 or signature != last_zone_signature:
                    if x - last_zone_x >= 8:
                        zone_index = max(range(3), key=lambda zone: sample.zones[zone])
                        canvas.create_oval(x - 3, zones_y - 3, x + 3, zones_y + 3, fill=ZONE_COLORS[zone_index], outline="")
                        last_zone_x = x
                    last_zone_signature = signature
            previous_index = index
            previous_sample = sample

        if len(activity_points) >= 4:
            canvas.create_polygon(
                activity_points + [activity_points[-2], activity_bottom, activity_points[0], activity_bottom],
                fill="#243D3C",
                outline="",
            )
            # Redraw the final waveform over its area fill.
            previous = None
            for index, sample in enumerate(self.timeline_samples):
                if sample is None:
                    previous = None
                    continue
                x = x0 + index * step
                y = activity_bottom - min(100.0, sample.activity) * (activity_bottom - activity_top) / 100
                if previous:
                    px, py, pa = previous
                    line_color = CORAL if max(pa, sample.activity) >= 65 else APRICOT if max(pa, sample.activity) >= 30 else SAGE
                    canvas.create_line(px, py, x, y, fill=line_color, width=2)
                previous = (x, y, sample.activity)
        if len(occupancy_segments) >= 4:
            canvas.create_line(*occupancy_segments, fill=SAGE, width=2)

        for tick in range(7):
            fraction = tick / 6
            x = x0 + fraction * (x1 - x0)
            timestamp = self.timeline_start + fraction * (self.timeline_end - self.timeline_start)
            canvas.create_line(x, zones_y + 7, x, zones_y + 11, fill=GRID)
            tick_anchor = "nw" if tick == 0 else "ne" if tick == 6 else "n"
            canvas.create_text(
                x,
                bottom,
                text=datetime.fromtimestamp(timestamp).strftime("%H:%M"),
                fill=CREAM,
                anchor=tick_anchor,
                font=("Consolas", 7),
            )

        if self.timeline_hover_index is not None:
            index = max(0, min(count - 1, self.timeline_hover_index))
            sample = self.timeline_samples[index]
            if sample:
                x = x0 + index * step
                y = activity_bottom - min(100.0, sample.activity) * (activity_bottom - activity_top) / 100
                canvas.create_line(x, activity_top, x, zones_y + 7, fill=CREAM, width=1)
                canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill=APRICOT, outline=CREAM)

    def _timeline_motion(self, event: tk.Event) -> None:
        if not self.timeline_samples:
            return
        x0, x1, _top, _bottom = self._timeline_geometry()
        if event.x < x0 or event.x > x1:
            return
        fraction = (event.x - x0) / max(1, x1 - x0)
        index = int(round(fraction * (len(self.timeline_samples) - 1)))
        if self.timeline_samples[index] is not None:
            self.timeline_hover_active = True
            self.timeline_hover_index = index
            self._update_timeline_event_text()
            self._draw_timeline()

    def _timeline_leave(self, _event: tk.Event) -> None:
        self.timeline_hover_active = False
        valid = [(index, sample.activity) for index, sample in enumerate(self.timeline_samples) if sample]
        self.timeline_hover_index = max(valid, key=lambda item: item[1])[0] if valid else None
        self._update_timeline_event_text()
        self._draw_timeline()

    def _update_timeline_event_text(self) -> None:
        if self.timeline_hover_index is None or not self.timeline_samples:
            self.timeline_event_var.set("Нет данных за выбранный период")
            return
        sample = self.timeline_samples[self.timeline_hover_index]
        if sample is None:
            self.timeline_event_var.set("Нет данных в этой точке")
            return
        if sample.activity >= 65:
            activity = "высокая активность"
        elif sample.activity >= 30:
            activity = "средняя активность"
        else:
            activity = "низкая активность"
        if any(sample.zones):
            zone_index = max(range(3), key=lambda index: sample.zones[index])
            zone = ZONE_NAMES[zone_index]
        else:
            zone = "зона пуста"
        moment = datetime.fromtimestamp(sample.timestamp).strftime("%H:%M")
        self.timeline_event_var.set(
            f"{moment} · {activity} · {person_count_text(sample.occupancy)} · {zone}"
        )

    def _render_radar(self) -> None:
        canvas = self.canvas
        width = max(canvas.winfo_width(), 300)
        height = max(canvas.winfo_height(), 260)

        size = (int(width), int(height))
        if size != self.canvas_size:
            self.canvas_size = size
            self._draw_static_radar(width, height)
            self.dynamic_dirty = True

        if self.dynamic_dirty:
            canvas.delete("dynamic")
            left, top, room_size = self._room_geometry(width, height)
            for index, trail in enumerate(self.trails):
                if len(trail) > 1:
                    coords = []
                    for x, y in trail:
                        px, py = self._room_to_canvas(x, y, left, top, room_size)
                        coords.extend((px, py))
                    canvas.create_line(
                        *coords,
                        fill=TARGET_COLORS[index],
                        width=1,
                        smooth=True,
                        tags="dynamic",
                    )

            for index, position in enumerate(self.current_room_positions):
                if position is None:
                    continue
                x, y = self._room_to_canvas(*position, left, top, room_size)
                if 0 <= x <= width and 0 <= y <= height:
                    color = TARGET_COLORS[index]
                    canvas.create_oval(
                        x - 12, y - 12, x + 12, y + 12,
                        fill="", outline=color, width=1, tags="dynamic"
                    )
                    canvas.create_oval(
                        x - 6, y - 6, x + 6, y + 6,
                        fill=color, outline="", tags="dynamic"
                    )
                    canvas.create_text(
                        x + 12,
                        y - 9,
                        text=str(index + 1),
                        fill=CREAM,
                        anchor="sw",
                        font=("Segoe UI", 8, "bold"),
                        tags="dynamic",
                    )
            self.dynamic_dirty = False
        self.root.after(50, self._render_radar)

    @staticmethod
    def _room_geometry(width: float, height: float) -> tuple[float, float, float]:
        room_size = max(150.0, min(width - 68.0, height - 42.0))
        left = max(42.0, (width - room_size) / 2)
        if left + room_size > width - 14:
            left = width - room_size - 14
        top = max(8.0, (height - room_size - 22.0) / 2)
        return left, top, room_size

    def _room_to_canvas(
        self, x: float, y: float, left: float, top: float, room_size: float
    ) -> tuple[float, float]:
        px = left + (x + self.calibration.room_width / 2) / self.calibration.room_width * room_size
        py = top + room_size - y / self.calibration.room_height * room_size
        return px, py

    def _canvas_to_room(self, px: float, py: float) -> tuple[float, float]:
        width = max(self.canvas.winfo_width(), 300)
        height = max(self.canvas.winfo_height(), 260)
        left, top, room_size = self._room_geometry(width, height)
        x = (px - left) / room_size * self.calibration.room_width - self.calibration.room_width / 2
        y = (top + room_size - py) / room_size * self.calibration.room_height
        return x, y

    def _fov_room_points(self) -> list[tuple[float, float]]:
        settings = self.calibration
        points = [(settings.radar_x, settings.radar_y)]
        half_width = settings.room_width / 2
        for step in range(31):
            angle = settings.rotation_deg - 60.0 + step * 4.0
            radians = math.radians(angle)
            dx = math.sin(radians)
            dy = math.cos(radians)
            distances = [RANGE_MM / 1000.0]
            if dx > 1e-6:
                distances.append((half_width - settings.radar_x) / dx)
            elif dx < -1e-6:
                distances.append((-half_width - settings.radar_x) / dx)
            if dy > 1e-6:
                distances.append((settings.room_height - settings.radar_y) / dy)
            elif dy < -1e-6:
                distances.append((0.0 - settings.radar_y) / dy)
            distance = min(value for value in distances if value >= 0)
            points.append((settings.radar_x + dx * distance, settings.radar_y + dy * distance))
        return points

    def _draw_static_radar(self, width: float, height: float) -> None:
        canvas = self.canvas
        canvas.delete("static")
        left, top, room_size = self._room_geometry(width, height)
        right = left + room_size
        bottom = top + room_size

        canvas.create_rectangle(
            left, top, right, bottom,
            fill="#132A32", outline=BORDER, width=2, tags="static"
        )

        fov_coords: list[float] = []
        for x, y in self._fov_room_points():
            fov_coords.extend(self._room_to_canvas(x, y, left, top, room_size))
        canvas.create_polygon(
            *fov_coords,
            fill="#1B383B",
            outline=MUTED_TEAL,
            width=1,
            tags="static",
        )

        for metre in range(1, int(self.calibration.room_width)):
            x_value = -self.calibration.room_width / 2 + metre
            x, _ = self._room_to_canvas(x_value, 0, left, top, room_size)
            canvas.create_line(x, top, x, bottom, fill=GRID, dash=(2, 4), tags="static")
        for metre in range(1, int(self.calibration.room_height)):
            _, y = self._room_to_canvas(0, metre, left, top, room_size)
            canvas.create_line(left, y, right, y, fill=GRID, dash=(2, 4), tags="static")

        half_width = self.calibration.room_width / 2
        x_ticks = range(math.ceil(-half_width), math.floor(half_width) + 1)
        for value in x_ticks:
            x, _ = self._room_to_canvas(value, 0, left, top, room_size)
            canvas.create_text(
                x, bottom + 8, text=f"{value:+d}", fill=MUTED_TEXT,
                anchor="n", font=("Consolas", 7), tags="static"
            )
        for value in range(0, math.floor(self.calibration.room_height) + 1):
            _, y = self._room_to_canvas(0, value, left, top, room_size)
            canvas.create_text(
                left - 8, y, text=str(value), fill=MUTED_TEXT,
                anchor="e", font=("Consolas", 7), tags="static"
            )

        for index, (zone, color, fill) in enumerate(
            zip(self.calibration.zones, ZONE_COLORS, ZONE_FILLS)
        ):
            rect = zone.normalized()
            x1, y_bottom = self._room_to_canvas(rect.x1, rect.y1, left, top, room_size)
            x2, y_top = self._room_to_canvas(rect.x2, rect.y2, left, top, room_size)
            selected = self.zone_edit_mode and index == self.selected_zone
            canvas.create_rectangle(
                x1, y_top, x2, y_bottom,
                fill=fill,
                outline=CREAM if selected else color,
                width=2 if selected else 1,
                tags="static",
            )
            canvas.create_text(
                (x1 + x2) / 2,
                y_top + 8,
                text=zone.name,
                fill=color,
                anchor="n",
                font=("Segoe UI", 9, "bold"),
                tags="static",
            )
            if selected:
                canvas.create_text(
                    (x1 + x2) / 2,
                    y_bottom - 7,
                    text=f"{rect.x2 - rect.x1:.1f} × {rect.y2 - rect.y1:.1f} м",
                    fill=CREAM,
                    anchor="s",
                    font=("Consolas", 7),
                    tags="static",
                )
                for hx, hy in ((x1, y_top), (x2, y_top), (x1, y_bottom), (x2, y_bottom)):
                    canvas.create_rectangle(
                        hx - 4, hy - 4, hx + 4, hy + 4,
                        fill=CREAM, outline=GRAPHITE, tags="static"
                    )

        radar_x, radar_y = self._room_to_canvas(
            self.calibration.radar_x,
            self.calibration.radar_y,
            left,
            top,
            room_size,
        )
        radians = math.radians(self.calibration.rotation_deg)
        forward = (math.sin(radians), -math.cos(radians))
        side = (-forward[1], forward[0])
        canvas.create_polygon(
            radar_x + forward[0] * 11,
            radar_y + forward[1] * 11,
            radar_x - forward[0] * 6 + side[0] * 7,
            radar_y - forward[1] * 6 + side[1] * 7,
            radar_x - forward[0] * 6 - side[0] * 7,
            radar_y - forward[1] * 6 - side[1] * 7,
            fill=SAGE,
            outline=CREAM,
            tags="static",
        )
        canvas.create_text(
            right,
            bottom + 8,
            text="X, м",
            fill=MUTED_TEXT,
            anchor="ne",
            font=("Segoe UI", 7),
            tags="static",
        )
        canvas.create_text(
            left - 8,
            top,
            text="Y, м",
            fill=MUTED_TEXT,
            anchor="se",
            font=("Segoe UI", 7),
            tags="static",
        )

    def _update_room_size_text(self) -> None:
        if hasattr(self, "room_size_var"):
            self.room_size_var.set(
                f"{self.calibration.room_width:.1f} × {self.calibration.room_height:.1f} м · сетка 1 м"
            )

    def _invalidate_map(self) -> None:
        self.canvas_size = (0, 0)
        self.dynamic_dirty = True

    def toggle_zone_editing(self) -> None:
        self.zone_edit_mode = not self.zone_edit_mode
        self.zone_drag = None
        self.zone_edit_button.configure(
            text="Готово" if self.zone_edit_mode else "Редактировать зоны",
            style="Accent.TButton" if self.zone_edit_mode else "Quiet.TButton",
        )
        if not self.zone_edit_mode:
            save_calibration(self.settings_path, self.calibration)
            self.status_var.set("Прямоугольные зоны сохранены")
        else:
            self.status_var.set("Перетащите зону или её угловые маркеры")
        self._invalidate_map()

    def _zone_handles(self, zone: ZoneRect) -> dict[str, tuple[float, float]]:
        width = max(self.canvas.winfo_width(), 300)
        height = max(self.canvas.winfo_height(), 260)
        left, top, room_size = self._room_geometry(width, height)
        rect = zone.normalized()
        x1, y_bottom = self._room_to_canvas(rect.x1, rect.y1, left, top, room_size)
        x2, y_top = self._room_to_canvas(rect.x2, rect.y2, left, top, room_size)
        return {
            "nw": (x1, y_top), "ne": (x2, y_top),
            "sw": (x1, y_bottom), "se": (x2, y_bottom),
        }

    def _zone_pointer_down(self, event: tk.Event) -> None:
        if not self.zone_edit_mode:
            return
        selected = self.calibration.zones[self.selected_zone]
        for handle, (hx, hy) in self._zone_handles(selected).items():
            if math.hypot(event.x - hx, event.y - hy) <= 11:
                self.zone_drag = {
                    "mode": handle,
                    "start": self._canvas_to_room(event.x, event.y),
                    "rect": selected.normalized(),
                }
                return

        x, y = self._canvas_to_room(event.x, event.y)
        for index in reversed(range(len(self.calibration.zones))):
            if self.calibration.zones[index].contains(x, y):
                self.selected_zone = index
                rect = self.calibration.zones[index].normalized()
                self.zone_drag = {"mode": "move", "start": (x, y), "rect": rect}
                self._invalidate_map()
                return

    def _zone_pointer_move(self, event: tk.Event) -> None:
        if not self.zone_edit_mode or self.zone_drag is None:
            return
        current_x, current_y = self._canvas_to_room(event.x, event.y)
        start_x, start_y = self.zone_drag["start"]
        original: ZoneRect = self.zone_drag["rect"]
        dx, dy = current_x - start_x, current_y - start_y
        mode = self.zone_drag["mode"]
        x1, y1, x2, y2 = original.x1, original.y1, original.x2, original.y2

        if mode == "move":
            width, height = x2 - x1, y2 - y1
            x1 = max(-self.calibration.room_width / 2, min(self.calibration.room_width / 2 - width, x1 + dx))
            y1 = max(0.0, min(self.calibration.room_height - height, y1 + dy))
            x2, y2 = x1 + width, y1 + height
        else:
            if "w" in mode:
                x1 = min(x2 - 0.3, x1 + dx)
            if "e" in mode:
                x2 = max(x1 + 0.3, x2 + dx)
            if "s" in mode:
                y1 = min(y2 - 0.3, y1 + dy)
            if "n" in mode:
                y2 = max(y1 + 0.3, y2 + dy)
            x1 = max(-self.calibration.room_width / 2, x1)
            x2 = min(self.calibration.room_width / 2, x2)
            y1 = max(0.0, y1)
            y2 = min(self.calibration.room_height, y2)

        self.calibration.zones[self.selected_zone] = ZoneRect(original.name, x1, y1, x2, y2)
        self._invalidate_map()

    def _zone_pointer_up(self, _event: tk.Event) -> None:
        if self.zone_drag is None:
            return
        self.zone_drag = None
        save_calibration(self.settings_path, self.calibration)
        self.status_var.set(f"Зона «{self.calibration.zones[self.selected_zone].name}» сохранена")

    def open_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Калибровка помещения")
        self.root.update_idletasks()
        dialog_width = 500
        dialog_height = 545
        dialog_x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - dialog_width) // 2)
        dialog_y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - dialog_height) // 2)
        dialog.geometry(f"{dialog_width}x{dialog_height}+{dialog_x}+{dialog_y}")
        dialog.resizable(False, False)
        dialog.configure(bg=BG)
        dialog.transient(self.root)
        dialog.grab_set()
        tk.Label(
            dialog,
            text="Калибровка помещения",
            bg=BG,
            fg=CREAM,
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w", padx=20, pady=(18, 6))
        tk.Label(
            dialog,
            text="Достаточно измерить комнату и указать положение радара.\n"
                 "Точная многоточечная калибровка обычно не нужна для LD2450.",
            bg=BG,
            fg=MUTED_TEXT,
            font=("Segoe UI", 9),
            justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 14))

        form = tk.Frame(dialog, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        form.pack(fill="x", padx=20)
        form.grid_columnconfigure(1, weight=1)

        values = {
            "room_width": tk.StringVar(value=f"{self.calibration.room_width:.2f}"),
            "room_height": tk.StringVar(value=f"{self.calibration.room_height:.2f}"),
            "radar_x": tk.StringVar(value=f"{self.calibration.radar_x:.2f}"),
            "radar_y": tk.StringVar(value=f"{self.calibration.radar_y:.2f}"),
            "rotation_deg": tk.StringVar(value=f"{self.calibration.rotation_deg:.1f}"),
            "smoothing": tk.StringVar(value=f"{self.calibration.smoothing:.2f}"),
        }
        rows = (
            ("Ширина помещения, м", "room_width"),
            ("Глубина помещения, м", "room_height"),
            ("Радар X от центра, м", "radar_x"),
            ("Радар Y от нижней стены, м", "radar_y"),
            ("Поворот радара, °", "rotation_deg"),
            ("Сглаживание 0.05–1.00", "smoothing"),
        )
        for row_index, (label, key) in enumerate(rows):
            tk.Label(
                form, text=label, bg=PANEL, fg=CREAM,
                font=("Segoe UI", 9), anchor="w"
            ).grid(row=row_index, column=0, sticky="w", padx=12, pady=7)
            entry = tk.Entry(
                form,
                textvariable=values[key],
                width=12,
                bg=PANEL_ALT,
                fg=CREAM,
                insertbackground=CREAM,
                relief="flat",
                justify="right",
                font=("Consolas", 9),
            )
            entry.grid(row=row_index, column=1, sticky="e", padx=12, pady=7, ipady=4)

        mirror_var = tk.BooleanVar(value=self.calibration.mirror_x)
        tk.Checkbutton(
            form,
            text="Отразить X, если лево и право перепутаны",
            variable=mirror_var,
            bg=PANEL,
            fg=CREAM,
            selectcolor=PANEL_ALT,
            activebackground=PANEL,
            activeforeground=CREAM,
            font=("Segoe UI", 9),
        ).grid(row=len(rows), column=0, columnspan=2, sticky="w", padx=9, pady=(5, 11))

        error_var = tk.StringVar()
        tk.Label(
            dialog,
            textvariable=error_var,
            bg=BG,
            fg=CORAL,
            font=("Segoe UI", 8),
        ).pack(anchor="w", padx=20, pady=(8, 0))

        tk.Label(
            dialog,
            text="Зоны меняются прямо на плане кнопкой «Редактировать зоны».\n"
                 f"История хранится {HISTORY_DAYS} дней: {self.history.path}",
            bg=BG,
            fg=MUTED_TEXT,
            justify="left",
            wraplength=455,
            font=("Segoe UI", 8),
        ).pack(anchor="w", padx=20, pady=(8, 4))

        buttons = tk.Frame(dialog, bg=BG)
        buttons.pack(fill="x", padx=20, pady=(6, 14))

        def apply_settings() -> None:
            try:
                room_width = float(values["room_width"].get().replace(",", "."))
                room_height = float(values["room_height"].get().replace(",", "."))
                radar_x = float(values["radar_x"].get().replace(",", "."))
                radar_y = float(values["radar_y"].get().replace(",", "."))
                rotation_deg = float(values["rotation_deg"].get().replace(",", "."))
                smoothing = float(values["smoothing"].get().replace(",", "."))
            except ValueError:
                error_var.set("Проверьте числа: допустима точка или запятая.")
                return
            if not 2.0 <= room_width <= 12.0 or not 2.0 <= room_height <= 12.0:
                error_var.set("Размер помещения должен быть от 2 до 12 метров.")
                return
            if not -room_width / 2 <= radar_x <= room_width / 2 or not 0 <= radar_y <= room_height:
                error_var.set("Положение радара должно находиться внутри помещения.")
                return
            if not -180 <= rotation_deg <= 180 or not 0.05 <= smoothing <= 1.0:
                error_var.set("Поворот: −180…180°, сглаживание: 0.05…1.00.")
                return

            old_width = self.calibration.room_width
            old_height = self.calibration.room_height
            self.calibration.room_width = room_width
            self.calibration.room_height = room_height
            self.calibration.radar_x = radar_x
            self.calibration.radar_y = radar_y
            self.calibration.rotation_deg = rotation_deg
            self.calibration.mirror_x = mirror_var.get()
            self.calibration.smoothing = smoothing

            if room_width != old_width or room_height != old_height:
                half = room_width / 2
                resized: list[ZoneRect] = []
                for zone in self.calibration.zones:
                    rect = zone.normalized()
                    resized.append(ZoneRect(
                        rect.name,
                        max(-half, min(half - 0.3, rect.x1)),
                        max(0.0, min(room_height - 0.3, rect.y1)),
                        max(-half + 0.3, min(half, rect.x2)),
                        max(0.3, min(room_height, rect.y2)),
                    ).normalized())
                self.calibration.zones = resized

            self.filtered_positions = [None, None, None]
            self.current_room_positions = [None, None, None]
            self.target_zone_indices = [None, None, None]
            self.trails = [deque(maxlen=28) for _ in range(3)]
            save_calibration(self.settings_path, self.calibration)
            self._update_room_size_text()
            self._invalidate_map()
            self.status_var.set("Калибровка помещения сохранена")
            dialog.destroy()

        ttk.Button(
            buttons, text="Отмена", style="Quiet.TButton", command=dialog.destroy
        ).pack(side="right", padx=(8, 0))
        ttk.Button(
            buttons, text="Сохранить", style="Accent.TButton", command=apply_settings
        ).pack(side="right")

    def _start_demo(self) -> None:
        self.port_box["values"] = ["COM9 — CP210x USB"]
        self.port_box.set("COM9 — CP210x USB")
        self.port_box.configure(state="disabled")
        self.refresh_button.configure(state="normal")
        self.connect_button.configure(text="Отключить", state="normal")
        self.status_var.set("Радар передаёт данные · демонстрационный режим")
        self.status_dot.configure(fg=SAGE)
        self._seed_demo_history()
        self._simulate_demo()

    def _seed_demo_history(self) -> None:
        now = int(time.time())
        start = now - 6 * 3600
        samples: list[HistorySample] = []
        pattern = (0, 1, 1, 0, 1, 2, 2, 1, 1, 2, 1, 0, 0, 1, 1, 2, 2, 3, 3, 2, 2, 1, 1, 1)
        for timestamp in range(start, now + 1):
            elapsed = timestamp - start
            occupancy = pattern[(elapsed // 900) % len(pattern)]
            base = 5 + 8 * abs(math.sin(elapsed / 83)) + 5 * abs(math.sin(elapsed / 29))
            spike = 0.0
            for center, height, width in (
                (2700, 45, 65), (5100, 38, 90), (8700, 58, 70),
                (11200, 52, 80), (15600, 92, 55), (18800, 65, 60), (20700, 72, 48),
            ):
                spike += height * math.exp(-((elapsed - center) / width) ** 2)
            activity = min(100.0, base + spike if occupancy else base * 0.18)
            zones = [0, 0, 0]
            if occupancy:
                primary = (elapsed // 1200) % 3
                zones[primary] = 1
                if occupancy >= 2:
                    zones[(primary + 1) % 3] = occupancy - 1
            samples.append(HistorySample(timestamp, occupancy, activity, tuple(zones)))
        self.history.append_many(samples)

    def _simulate_demo(self) -> None:
        self.demo_tick += 1
        phase = self.demo_tick / 20
        first = Target(True, int(-420 + math.sin(phase) * 130), int(3700 + math.cos(phase * 0.7) * 120), 12, 60)
        second = Target(True, int(1950 + math.cos(phase * 0.8) * 120), int(2000 + math.sin(phase * 0.5) * 130), 3, 60)
        third = Target(False, 0, 0, 0, 0)
        frame = RadarFrame(self.demo_tick * 200, 30844 + self.demo_tick, (first, second, third))
        self._accept_frame(frame)
        self.root.after(200, self._simulate_demo)

    def _on_close(self) -> None:
        self.reader.stop()
        self.history.close()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    MonitorApp(root, demo="--demo" in sys.argv)
    root.mainloop()


if __name__ == "__main__":
    main()
