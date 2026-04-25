"""
ACE Lap Tracker – Windows Tray App
Watches Assetto Corsa Evo log files for completed laps and submits them
to your ACE Lap Tracker backend on your homelab.
"""

import sys
import os
import re
import json
import time
import uuid
import socket
import platform
import logging
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QComboBox, QSystemTrayIcon,
    QMenu, QTableWidget, QTableWidgetItem, QHeaderView, QGroupBox,
    QFormLayout, QSpinBox, QMessageBox, QTabWidget, QTextEdit,
    QStackedWidget, QFrame, QSizePolicy, QFileDialog
)
from PyQt6.QtCore import (
    Qt, QTimer, QThread, pyqtSignal, QSettings, QSize
)
from PyQt6.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QFont, QAction, QPalette,
    QBrush, QLinearGradient
)

import requests

# ─── Constants ───────────────────────────────────────────────────────

APP_NAME = "ACE Lap Tracker"
APP_VERSION = "1.0.0"
ORG_NAME = "ACELaps"

WEATHER_OPTIONS = ["Clear", "Cloudy", "Light Rain", "Heavy Rain", "Fog", "Snow", "Storm", "Dynamic"]

# Default ACE log location.
# ACE 0.5.x: single log.txt — point to the file.
# ACE 0.6+:  per-session files — point to the Logs/ directory and the watcher
#            picks up the newest .txt automatically.
DEFAULT_LOG_PATH = os.path.join(
    os.environ.get("USERPROFILE", os.path.expanduser("~")),
    "Saved Games", "ACE", "Logs"
)

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("ace_tray")

# ─── Data classes ────────────────────────────────────────────────────

@dataclass
class LapRecord:
    track: str
    car: str
    laptime_ms: int
    weather: str = "Clear"
    notes: str = ""
    recorded_at: str = ""

    def formatted_time(self) -> str:
        m = self.laptime_ms // 60000
        s = (self.laptime_ms % 60000) // 1000
        ms = self.laptime_ms % 1000
        return f"{m}:{s:02d}.{ms:03d}"


# ─── API Client ──────────────────────────────────────────────────────

class APIClient:
    def __init__(self):
        self.base_url = ""
        self.token = ""
        self.user_agent = f"ace-tray/{APP_VERSION} ({platform.system()} {platform.release()})"

    def configure(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _headers(self):
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": self.user_agent,
        }

    def login(self, server_url: str, username: str, password: str) -> dict:
        url = f"{server_url.rstrip('/')}/api/auth/login"
        resp = requests.post(
            url,
            json={"username": username, "password": password},
            headers={"User-Agent": self.user_agent},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        self.base_url = server_url.rstrip("/")
        self.token = data["token"]
        return data

    def submit_lap(self, lap: LapRecord) -> dict:
        url = f"{self.base_url}/api/laptimes"
        resp = requests.post(url, json=asdict(lap), headers=self._headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_recent_laps(self, limit: int = 10) -> list:
        url = f"{self.base_url}/api/laptimes"
        resp = requests.get(url, headers=self._headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()[:limit]

    def get_meta(self, kind: str) -> list:
        url = f"{self.base_url}/api/meta/{kind}"
        resp = requests.get(url, headers=self._headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()

    def is_connected(self) -> bool:
        if not self.base_url or not self.token:
            return False
        try:
            url = f"{self.base_url}/api/auth/me"
            resp = requests.get(url, headers=self._headers(), timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def send_heartbeat(self, client_id: str) -> bool:
        if not self.base_url or not self.token:
            return False
        url = f"{self.base_url}/api/client/heartbeat"
        payload = {
            "client_id": client_id,
            "hostname": socket.gethostname(),
            "platform": f"{platform.system()} {platform.release()}",
            "app_version": APP_VERSION,
        }
        resp = requests.post(url, json=payload, headers=self._headers(), timeout=5)
        return resp.status_code == 200

    def send_disconnect(self, client_id: str) -> bool:
        if not self.base_url or not self.token:
            return False
        url = f"{self.base_url}/api/client/disconnect"
        resp = requests.post(
            url, json={"client_id": client_id}, headers=self._headers(), timeout=3
        )
        return resp.status_code == 200


# ─── Log Watcher Thread ─────────────────────────────────────────────

class LogWatcher(QThread):
    """
    Watches the ACE log.txt for completed laps.

    Supports two distinct log formats:

    RACE MODE (GameModeType_INSTANT_RACE):
      - Session: Game Started! ... N laps @... | <car> | WeatherType_XXX
      - Splits:  Split completed for car <uuid>: (<ms> ms, splitindex <N>) lap:<L>
      - Player car identified by UUID; AI cars filtered out.

    PRACTICE MODE (GameModeType_PRACTICE):
      - Session: Game Started! ... N seconds @... | <car> | WeatherType_XXX
      - Splits:  On Split start X end X id <splitindex> splittime <ms>
      - No car UUID (solo session); all splits are the player's.
      - Lap boundary: "Lap test evOnLapCompleted N completed"

    Both modes: only complete laps (all sectors present) are recorded.
    """

    lap_detected = pyqtSignal(dict)
    session_detected = pyqtSignal(dict)
    status_changed = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    # ── Regex patterns ───────────────────────────────────────────

    # Session start: handles both "N laps" (race) and "N seconds" (practice)
    RE_SESSION_START = re.compile(
        r'Game Started!\s+(\S+)\s+\|\s+(.+?)\s+\d+\s+(?:laps?|seconds)\s+@[^|]+\|\s+(\S+)\s+\|\s+GameModeSelectionWeatherType_(\S+)'
    )

    # Player car UUID (from car selection before session)
    RE_PLAYER_CAR = re.compile(
        r'onSetPlayerCurrentCarCommand:\s+Set new car\s+(\S+)\s+content\\+cars\\+(\S+?)\\+'
    )

    # Race split: includes car UUID
    RE_SPLIT_RACE = re.compile(
        r'Split completed for car\s+([0-9a-f-]+):\s+\((\d+)\s+ms,\s+splitindex\s+(\d+)\)\s+lap:(\d+)'
    )

    # Practice split: no car UUID, different format
    RE_SPLIT_PRACTICE = re.compile(
        r'On Split start \d+ end \d+ id (\d+) splittime (\d+)'
    )

    # Lap completion marker (used in practice to know when a lap is done)
    RE_LAP_COMPLETED = re.compile(
        r'Lap test evOnLapCompleted (\d+) completed'
    )

    def __init__(self, log_path: str, parent=None):
        super().__init__(parent)
        self.log_path = log_path
        self._running = False
        self._file_pos = 0
        self._active_file: Optional[str] = None
        self._reset_session()

    def _reset_session(self):
        self._current_track = ""
        self._current_car_id = ""
        self._current_weather = "Clear"
        self._current_game_mode = ""
        self._player_car_uuid = ""
        self._is_practice = False

        # Race tracking
        self._race_splits = {}       # {lap_num: {splitindex: ms}}
        self._race_emitted = set()

        # Practice tracking
        self._practice_splits = {}   # {splitindex: ms}
        self._practice_lap_count = 0

        self._max_splitindex = 2
        self._current_lap = -1

    def _resolve_active_file(self) -> Optional[str]:
        """Return the file to watch.
        - If log_path is a file: return it directly (ACE 0.5.x compat).
        - If log_path is a directory: return the most recently modified .txt
          file in it (ACE 0.6+ per-session logs).
        """
        p = Path(self.log_path)
        if p.is_file():
            return str(p)
        if p.is_dir():
            files = sorted(p.glob("*.txt"), key=lambda f: f.stat().st_mtime, reverse=True)
            return str(files[0]) if files else None
        return None

    def set_log_path(self, path: str):
        self.log_path = path
        self._file_pos = 0
        self._active_file = None
        self._reset_session()

    def run(self):
        self._running = True
        self.status_changed.emit("Watching for laps...")

        try:
            if os.path.exists(self.log_path):
                self._file_pos = os.path.getsize(self.log_path)
        except OSError:
            self._file_pos = 0

        while self._running:
            try:
                self._check_log()
            except Exception as e:
                logger.error(f"Log watcher error: {e}")
                self.error_occurred.emit(str(e))
            self.msleep(2000)

    def stop(self):
        self._running = False
        self.wait(5000)

    def _check_log(self):
        active = self._resolve_active_file()
        if not active:
            return

        # New session file appeared (ACE 0.6+ per-session logs) or first run
        if active != self._active_file:
            self._active_file = active
            self._file_pos = 0
            self._reset_session()
            self.status_changed.emit(f"Watching: {os.path.basename(active)}")

        try:
            file_size = os.path.getsize(active)
        except OSError:
            return

        # File was truncated/replaced (ACE 0.5.x new session in same file)
        if file_size < self._file_pos:
            self._file_pos = 0
            self._reset_session()
            self.status_changed.emit("New game session detected")

        if file_size <= self._file_pos:
            return

        try:
            with open(active, 'r', encoding='utf-8', errors='ignore') as f:
                f.seek(self._file_pos)
                new_content = f.read()
                self._file_pos = f.tell()
        except (OSError, IOError) as e:
            logger.warning(f"Cannot read log: {e}")
            return

        for line in new_content.splitlines():
            line = line.strip()
            if not line:
                continue
            self._parse_line(line)

    def _parse_line(self, line: str):
        # ── 1. Detect player car (before session starts) ─────────
        match = self.RE_PLAYER_CAR.search(line)
        if match:
            self._player_car_uuid = match.group(1).replace('-', '')
            logger.info(f"Player car UUID: {self._player_car_uuid}")
            return

        # ── 2. Detect session start ──────────────────────────────
        match = self.RE_SESSION_START.search(line)
        if match:
            self._current_game_mode = match.group(1)
            raw_track = match.group(2).strip()
            self._current_car_id = match.group(3).strip()
            self._current_weather = match.group(4).strip().replace('_', ' ').title()
            self._is_practice = "PRACTICE" in self._current_game_mode

            # Clean track name: strip mode suffixes
            track = re.sub(
                r'\s+(Race|Practice|Qualifying|Hotlap|Time Attack)\s*$',
                '', raw_track, flags=re.IGNORECASE
            ).strip()
            track = re.sub(
                r'\s+(Race|Practice|Qualifying|Hotlap|Time Attack)\s*$',
                '', track, flags=re.IGNORECASE
            ).strip()
            self._current_track = track

            # Reset tracking for new session
            self._race_splits = {}
            self._race_emitted = set()
            self._practice_splits = {}
            self._practice_lap_count = 0
            self._max_splitindex = 2
            self._current_lap = -1

            mode_label = "Practice" if self._is_practice else "Race"
            car_name = self._format_car_name(self._current_car_id)

            self.session_detected.emit({
                "track": self._current_track,
                "car": car_name,
                "weather": self._current_weather,
                "game_mode": self._current_game_mode,
            })
            self.status_changed.emit(
                f"{mode_label}: {self._current_track} | {car_name} | {self._current_weather}"
            )
            logger.info(f"Session: {mode_label} / {self._current_track} / {self._current_car_id} / {self._current_weather}")
            return

        # ── 3. Parse splits based on mode ────────────────────────
        if self._is_practice:
            self._parse_practice(line)
        else:
            self._parse_race(line)

    def _parse_race(self, line: str):
        match = self.RE_SPLIT_RACE.search(line)
        if not match:
            return

        car_uuid = match.group(1).replace('-', '')
        split_ms = int(match.group(2))
        splitindex = int(match.group(3))
        lap_num = int(match.group(4))

        # Only track the player's car
        if not self._player_car_uuid or car_uuid != self._player_car_uuid:
            return

        if splitindex > self._max_splitindex:
            self._max_splitindex = splitindex

        if lap_num not in self._race_splits:
            self._race_splits[lap_num] = {}
        self._race_splits[lap_num][splitindex] = split_ms

        # Check if all sectors complete for this lap
        if splitindex == self._max_splitindex and lap_num not in self._race_emitted:
            splits = self._race_splits[lap_num]
            expected = set(range(self._max_splitindex + 1))
            if expected.issubset(splits.keys()):
                total_ms = sum(splits[i] for i in range(self._max_splitindex + 1))
                self._race_emitted.add(lap_num)
                self._emit_lap(total_ms, splits, lap_num)

    def _parse_practice(self, line: str):
        # Collect split times
        match = self.RE_SPLIT_PRACTICE.search(line)
        if match:
            splitindex = int(match.group(1))
            split_ms = int(match.group(2))
            self._practice_splits[splitindex] = split_ms
            if splitindex > self._max_splitindex:
                self._max_splitindex = splitindex
            return

        # Detect lap completion
        match = self.RE_LAP_COMPLETED.search(line)
        if match:
            if self._practice_splits:
                expected = set(range(self._max_splitindex + 1))
                if expected.issubset(self._practice_splits.keys()):
                    # Complete lap — all sectors present
                    total_ms = sum(self._practice_splits[i] for i in range(self._max_splitindex + 1))
                    self._emit_lap(total_ms, dict(self._practice_splits), self._practice_lap_count)
                else:
                    # Partial lap (outlap / cut track) — skip it
                    logger.info(
                        f"Skipping partial practice lap {self._practice_lap_count}: "
                        f"only sectors {sorted(self._practice_splits.keys())}"
                    )
                    self.status_changed.emit(
                        f"Partial lap skipped (sectors incomplete)"
                    )
                self._practice_lap_count += 1
                self._practice_splits = {}

    def _emit_lap(self, total_ms: int, splits: dict, lap_num: int):
        # Sanity check: between 20s and 20min
        if total_ms < 20000 or total_ms > 1200000:
            logger.warning(f"Lap {lap_num} time {total_ms}ms outside sane range, skipping")
            self.status_changed.emit(f"Lap {lap_num} skipped (invalid time)")
            return

        # Build sector notes
        sector_strs = []
        for i in range(self._max_splitindex + 1):
            if i in splits:
                sec = splits[i] / 1000
                sector_strs.append(f"S{i+1}: {sec:.3f}s")
        sector_notes = " | ".join(sector_strs)

        mode_label = "Practice" if self._is_practice else "Race"
        car_name = self._format_car_name(self._current_car_id)

        lap_data = {
            "track": self._current_track or "Unknown Track",
            "car": car_name or "Unknown Car",
            "laptime_ms": total_ms,
            "weather": self._current_weather,
            "notes": f"[{mode_label}] {sector_notes}",
        }
        self.lap_detected.emit(lap_data)
        self.status_changed.emit(
            f"{mode_label} Lap {lap_num + 1}: {self._format_laptime(total_ms)} at {self._current_track}"
        )
        logger.info(f"Lap emitted: {lap_data}")

    @staticmethod
    def _format_car_name(car_id: str) -> str:
        if not car_id:
            return ""
        name = re.sub(r'^(ks_|ac_|kunos_)', '', car_id)
        name = name.replace('_', ' ').strip()
        parts = name.split()
        result = []
        for part in parts:
            if part.upper() == part and len(part) <= 5:
                result.append(part.upper())
            else:
                result.append(part.capitalize())
        return ' '.join(result)

    @staticmethod
    def _format_laptime(ms: int) -> str:
        m = ms // 60000
        s = (ms % 60000) // 1000
        mil = ms % 1000
        return f"{m}:{s:02d}.{mil:03d}"


# ─── Create tray icon programmatically ───────────────────────────────

def create_app_icon(size=64) -> QIcon:
    """Create a racing flag-inspired icon."""
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Red circle background
    painter.setBrush(QBrush(QColor("#e63946")))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(2, 2, size - 4, size - 4)

    # White checkered pattern (simplified)
    painter.setBrush(QBrush(QColor(255, 255, 255)))
    block = size // 6
    for row in range(3):
        for col in range(3):
            if (row + col) % 2 == 0:
                x = size // 4 + col * block
                y = size // 4 + row * block
                painter.drawRect(x, y, block - 1, block - 1)

    painter.end()
    return QIcon(pixmap)


# ─── Styles ──────────────────────────────────────────────────────────

STYLESHEET = """
QMainWindow, QWidget {
    background-color: #0f0f17;
    color: #eaeaf0;
    font-family: 'Segoe UI', sans-serif;
}
QGroupBox {
    border: 1px solid #2a2a3a;
    border-radius: 8px;
    margin-top: 12px;
    padding-top: 20px;
    font-weight: bold;
    font-size: 12px;
    color: #8888a0;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
}
QLineEdit, QComboBox, QSpinBox {
    background-color: #1a1a26;
    border: 1px solid #2a2a3a;
    border-radius: 5px;
    padding: 7px 10px;
    color: #eaeaf0;
    font-size: 13px;
    selection-background-color: #e63946;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
    border-color: #e63946;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QComboBox QAbstractItemView {
    background-color: #1a1a26;
    border: 1px solid #2a2a3a;
    color: #eaeaf0;
    selection-background-color: #e63946;
}
QPushButton {
    background-color: #1a1a26;
    border: 1px solid #2a2a3a;
    border-radius: 5px;
    padding: 8px 16px;
    color: #eaeaf0;
    font-weight: 600;
    font-size: 12px;
}
QPushButton:hover {
    background-color: #22222f;
    border-color: #e63946;
}
QPushButton#primaryBtn {
    background-color: #e63946;
    border: none;
    color: white;
}
QPushButton#primaryBtn:hover {
    background-color: #d42f3c;
}
QPushButton#primaryBtn:disabled {
    background-color: #5a2a2e;
    color: #999;
}
QTableWidget {
    background-color: #12121a;
    border: 1px solid #2a2a3a;
    border-radius: 6px;
    gridline-color: #1e1e2a;
    font-size: 12px;
}
QTableWidget::item {
    padding: 6px 8px;
    border-bottom: 1px solid #1e1e2a;
}
QTableWidget::item:selected {
    background-color: rgba(230, 57, 70, 0.2);
}
QHeaderView::section {
    background-color: #16161e;
    color: #55556a;
    border: none;
    border-bottom: 1px solid #2a2a3a;
    padding: 6px 8px;
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
}
QTabWidget::pane {
    border: 1px solid #2a2a3a;
    border-radius: 6px;
    background-color: #0f0f17;
}
QTabBar::tab {
    background-color: #12121a;
    color: #8888a0;
    border: 1px solid #2a2a3a;
    border-bottom: none;
    padding: 8px 20px;
    font-weight: 600;
    font-size: 12px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}
QTabBar::tab:selected {
    background-color: #0f0f17;
    color: #e63946;
    border-bottom: 2px solid #e63946;
}
QTabBar::tab:hover:!selected {
    color: #eaeaf0;
}
QTextEdit {
    background-color: #12121a;
    border: 1px solid #2a2a3a;
    border-radius: 6px;
    color: #8888a0;
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 11px;
    padding: 8px;
}
QLabel#statusLabel {
    color: #55556a;
    font-size: 11px;
}
QLabel#headerLabel {
    font-size: 18px;
    font-weight: 800;
    color: #e63946;
    letter-spacing: 1px;
}
QLabel#laptimeDisplay {
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 28px;
    font-weight: 700;
    color: #2ec866;
}
QFrame#separator {
    background-color: #2a2a3a;
    max-height: 1px;
}
"""


# ─── Main Window ─────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(580, 640)
        self.setStyleSheet(STYLESHEET)

        self.settings = QSettings(ORG_NAME, APP_NAME)
        self.api = APIClient()
        self.watcher = None
        self.auto_submit = True
        self.pending_laps = []

        self._current_track = ""
        self._current_car = ""

        # Persistent client identifier so the backend can distinguish this
        # tray instance from any other (same user can run several).
        self.client_id = self.settings.value("client_id", "")
        if not self.client_id:
            self.client_id = str(uuid.uuid4())
            self.settings.setValue("client_id", self.client_id)

        # Restore saved credentials
        saved_url = self.settings.value("server_url", "")
        saved_token = self.settings.value("token", "")
        saved_user = self.settings.value("display_name", "")
        if saved_url and saved_token:
            self.api.configure(saved_url, saved_token)

        self._build_ui()
        self._setup_tray()
        self._start_watcher()

        # Heartbeat — pings backend every 30s while we have a token, so the
        # admin "Connected Clients" view can tell us apart from a lost one.
        self.heartbeat_timer = QTimer(self)
        self.heartbeat_timer.setInterval(30_000)
        self.heartbeat_timer.timeout.connect(self._send_heartbeat)
        self.heartbeat_timer.start()

        # Check connection on start
        QTimer.singleShot(500, self._check_connection)
        QTimer.singleShot(1500, self._send_heartbeat)

    # ── UI Construction ──────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Header
        header = QLabel("ACE LAP TRACKER")
        header.setObjectName("headerLabel")
        layout.addWidget(header)

        # Status bar
        self.status_label = QLabel("Initializing...")
        self.status_label.setObjectName("statusLabel")
        layout.addWidget(self.status_label)

        sep = QFrame()
        sep.setObjectName("separator")
        sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep)

        # Tabs
        tabs = QTabWidget()
        tabs.addTab(self._build_dashboard_tab(), "Dashboard")
        tabs.addTab(self._build_manual_tab(), "Manual Entry")
        tabs.addTab(self._build_settings_tab(), "Settings")
        tabs.addTab(self._build_log_tab(), "Activity Log")
        layout.addWidget(tabs)

    def _build_dashboard_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(12)

        # Last detected lap
        lap_group = QGroupBox("Last Detected Lap")
        lap_layout = QVBoxLayout(lap_group)

        self.last_lap_label = QLabel("--:--.---")
        self.last_lap_label.setObjectName("laptimeDisplay")
        self.last_lap_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lap_layout.addWidget(self.last_lap_label)

        self.last_lap_info = QLabel("Waiting for lap data...")
        self.last_lap_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.last_lap_info.setStyleSheet("color: #8888a0; font-size: 12px;")
        lap_layout.addWidget(self.last_lap_info)

        layout.addWidget(lap_group)

        # Current session info
        session_group = QGroupBox("Current Session")
        session_layout = QFormLayout(session_group)

        self.session_track_label = QLabel("—")
        self.session_track_label.setStyleSheet("font-weight: 600;")
        session_layout.addRow("Track:", self.session_track_label)

        self.session_car_label = QLabel("—")
        self.session_car_label.setStyleSheet("font-weight: 600;")
        session_layout.addRow("Car:", self.session_car_label)

        self.session_laps_label = QLabel("0")
        self.session_laps_label.setStyleSheet("font-weight: 600; color: #4a9eff;")
        session_layout.addRow("Laps this session:", self.session_laps_label)

        layout.addWidget(session_group)

        # Recent laps table
        recent_group = QGroupBox("Recent Laps (from server)")
        recent_layout = QVBoxLayout(recent_group)

        self.recent_table = QTableWidget(0, 4)
        self.recent_table.setHorizontalHeaderLabels(["Track", "Car", "Time", "Date"])
        self.recent_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.recent_table.verticalHeader().setVisible(False)
        self.recent_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.recent_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.recent_table.setMaximumHeight(200)
        recent_layout.addWidget(self.recent_table)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_recent)
        recent_layout.addWidget(refresh_btn, alignment=Qt.AlignmentFlag.AlignRight)

        layout.addWidget(recent_group)
        layout.addStretch()

        return tab

    def _build_manual_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(12)

        form_group = QGroupBox("Record a Lap Manually")
        form = QFormLayout(form_group)
        form.setSpacing(10)

        self.manual_track = QComboBox()
        self.manual_track.setEditable(True)
        self.manual_track.setPlaceholderText("e.g. Monza")
        form.addRow("Track:", self.manual_track)

        self.manual_car = QComboBox()
        self.manual_car.setEditable(True)
        self.manual_car.setPlaceholderText("e.g. Ferrari 296 GT3")
        form.addRow("Car:", self.manual_car)

        # Lap time inputs
        time_widget = QWidget()
        time_layout = QHBoxLayout(time_widget)
        time_layout.setContentsMargins(0, 0, 0, 0)
        time_layout.setSpacing(4)

        self.manual_min = QSpinBox()
        self.manual_min.setRange(0, 59)
        self.manual_min.setSuffix(" m")
        self.manual_min.setFixedWidth(80)
        time_layout.addWidget(self.manual_min)

        time_layout.addWidget(QLabel(":"))

        self.manual_sec = QSpinBox()
        self.manual_sec.setRange(0, 59)
        self.manual_sec.setSuffix(" s")
        self.manual_sec.setFixedWidth(80)
        time_layout.addWidget(self.manual_sec)

        time_layout.addWidget(QLabel("."))

        self.manual_ms = QSpinBox()
        self.manual_ms.setRange(0, 999)
        self.manual_ms.setSuffix(" ms")
        self.manual_ms.setFixedWidth(90)
        time_layout.addWidget(self.manual_ms)

        time_layout.addStretch()
        form.addRow("Lap Time:", time_widget)

        self.manual_weather = QComboBox()
        self.manual_weather.addItems(WEATHER_OPTIONS)
        form.addRow("Weather:", self.manual_weather)

        self.manual_notes = QLineEdit()
        self.manual_notes.setPlaceholderText("Optional notes...")
        form.addRow("Notes:", self.manual_notes)

        layout.addWidget(form_group)

        submit_btn = QPushButton("Submit Lap")
        submit_btn.setObjectName("primaryBtn")
        submit_btn.setFixedHeight(40)
        submit_btn.clicked.connect(self._manual_submit)
        layout.addWidget(submit_btn)

        self.manual_result = QLabel("")
        self.manual_result.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.manual_result)

        layout.addStretch()
        return tab

    def _build_settings_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(12)

        # Server connection
        server_group = QGroupBox("Server Connection")
        server_form = QFormLayout(server_group)
        server_form.setSpacing(10)

        self.server_url_input = QLineEdit()
        self.server_url_input.setPlaceholderText("http://192.168.1.x:8099")
        self.server_url_input.setText(self.settings.value("server_url", ""))
        server_form.addRow("Server URL:", self.server_url_input)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Your username")
        self.username_input.setText(self.settings.value("username", ""))
        server_form.addRow("Username:", self.username_input)

        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("Your password")
        server_form.addRow("Password:", self.password_input)

        connect_row = QWidget()
        connect_layout = QHBoxLayout(connect_row)
        connect_layout.setContentsMargins(0, 0, 0, 0)

        connect_btn = QPushButton("Connect")
        connect_btn.setObjectName("primaryBtn")
        connect_btn.clicked.connect(self._connect_to_server)
        connect_layout.addWidget(connect_btn)

        self.connection_status = QLabel("Not connected")
        self.connection_status.setStyleSheet("color: #e63946; font-size: 12px; font-weight: 600;")
        connect_layout.addWidget(self.connection_status)
        connect_layout.addStretch()

        server_form.addRow("", connect_row)
        layout.addWidget(server_group)

        # Log file settings
        log_group = QGroupBox("ACE Log File")
        log_form = QFormLayout(log_group)
        log_form.setSpacing(10)

        log_path_row = QWidget()
        log_path_layout = QHBoxLayout(log_path_row)
        log_path_layout.setContentsMargins(0, 0, 0, 0)

        self.log_path_input = QLineEdit()
        self.log_path_input.setText(
            self.settings.value("log_path", DEFAULT_LOG_PATH)
        )
        log_path_layout.addWidget(self.log_path_input)

        browse_btn = QPushButton("Browse")
        browse_btn.setFixedWidth(70)
        browse_btn.clicked.connect(self._browse_log_path)
        log_path_layout.addWidget(browse_btn)

        log_form.addRow("Log path (file or folder):", log_path_row)

        self.log_exists_label = QLabel("")
        log_form.addRow("", self.log_exists_label)

        save_log_btn = QPushButton("Save & Restart Watcher")
        save_log_btn.clicked.connect(self._save_log_settings)
        log_form.addRow("", save_log_btn)

        layout.addWidget(log_group)

        # Auto-submit toggle
        behavior_group = QGroupBox("Behavior")
        behavior_form = QFormLayout(behavior_group)

        self.auto_submit_combo = QComboBox()
        self.auto_submit_combo.addItems(["Auto-submit detected laps", "Ask before submitting"])
        self.auto_submit_combo.setCurrentIndex(
            0 if self.settings.value("auto_submit", "true") == "true" else 1
        )
        self.auto_submit_combo.currentIndexChanged.connect(self._toggle_auto_submit)
        behavior_form.addRow("On lap detect:", self.auto_submit_combo)

        layout.addWidget(behavior_group)
        layout.addStretch()

        return tab

    def _build_log_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        self.activity_log = QTextEdit()
        self.activity_log.setReadOnly(True)
        self.activity_log.setPlaceholderText("Activity will appear here...")
        layout.addWidget(self.activity_log)

        clear_btn = QPushButton("Clear Log")
        clear_btn.clicked.connect(self.activity_log.clear)
        layout.addWidget(clear_btn, alignment=Qt.AlignmentFlag.AlignRight)

        return tab

    # ── System Tray ──────────────────────────────────────────────────

    def _setup_tray(self):
        self.tray_icon = QSystemTrayIcon(create_app_icon(), self)
        self.tray_icon.setToolTip(APP_NAME)

        tray_menu = QMenu()

        show_action = QAction("Show Dashboard", self)
        show_action.triggered.connect(self._show_window)
        tray_menu.addAction(show_action)

        tray_menu.addSeparator()

        self.tray_status = QAction("Not connected", self)
        self.tray_status.setEnabled(False)
        tray_menu.addAction(self.tray_status)

        tray_menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._quit_app)
        tray_menu.addAction(quit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._tray_activated)
        self.tray_icon.show()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_window()

    def _show_window(self):
        self.showNormal()
        self.activateWindow()
        self.raise_()

    # ── Log Watcher ──────────────────────────────────────────────────

    def _start_watcher(self):
        log_path = self.settings.value("log_path", DEFAULT_LOG_PATH)

        if self.watcher and self.watcher.isRunning():
            self.watcher.stop()

        self.watcher = LogWatcher(log_path)
        self.watcher.lap_detected.connect(self._on_lap_detected)
        self.watcher.session_detected.connect(self._on_session_detected)
        self.watcher.status_changed.connect(self._on_watcher_status)
        self.watcher.error_occurred.connect(self._on_watcher_error)
        self.watcher.start()

        self._log(f"Watcher started: {log_path}")
        self._update_log_exists()

    def _on_lap_detected(self, lap_data: dict):
        track = lap_data.get("track", "Unknown Track")
        car = lap_data.get("car", "Unknown Car")
        laptime_ms = lap_data["laptime_ms"]
        weather = lap_data.get("weather", "Clear")
        notes = lap_data.get("notes", "")

        lap = LapRecord(
            track=track,
            car=car,
            laptime_ms=laptime_ms,
            weather=weather,
            notes=notes,
            recorded_at=datetime.now().isoformat()
        )

        # Update dashboard
        self.last_lap_label.setText(lap.formatted_time())
        self.last_lap_info.setText(f"{track}  ·  {car}")

        # Update session counter
        current = int(self.session_laps_label.text())
        self.session_laps_label.setText(str(current + 1))

        self._log(f"Lap detected: {track} / {car} – {lap.formatted_time()}")

        # Tray notification
        self.tray_icon.showMessage(
            "Lap Recorded" if self.auto_submit else "Lap Detected",
            f"{lap.formatted_time()} at {track}",
            QSystemTrayIcon.MessageIcon.Information,
            3000
        )

        if self.auto_submit and self.api.is_connected():
            self._submit_lap(lap)
        else:
            self.pending_laps.append(lap)
            self._log("Lap queued (not connected or manual mode)")

    def _on_session_detected(self, session: dict):
        if session.get("track"):
            self._current_track = session["track"]
            self.session_track_label.setText(session["track"])
            self.manual_track.setCurrentText(session["track"])
        if session.get("car"):
            self._current_car = session["car"]
            self.session_car_label.setText(session["car"])
            self.manual_car.setCurrentText(session["car"])
        if session.get("weather"):
            weather = session["weather"]
            idx = self.manual_weather.findText(weather, Qt.MatchFlag.MatchContains)
            if idx >= 0:
                self.manual_weather.setCurrentIndex(idx)
        self.session_laps_label.setText("0")

    def _on_watcher_status(self, status: str):
        self.status_label.setText(f"Watcher: {status}")

    def _on_watcher_error(self, error: str):
        self._log(f"Watcher error: {error}")

    # ── API Actions ──────────────────────────────────────────────────

    def _connect_to_server(self):
        url = self.server_url_input.text().strip()
        username = self.username_input.text().strip()
        password = self.password_input.text()

        if not url or not username or not password:
            self.connection_status.setText("Fill in all fields")
            self.connection_status.setStyleSheet("color: #e63946; font-size: 12px; font-weight: 600;")
            return

        try:
            data = self.api.login(url, username, password)
            display_name = data["user"]["display_name"]

            # Save credentials
            self.settings.setValue("server_url", url)
            self.settings.setValue("username", username)
            self.settings.setValue("token", self.api.token)
            self.settings.setValue("display_name", display_name)

            self.connection_status.setText(f"Connected as {display_name}")
            self.connection_status.setStyleSheet("color: #2ec866; font-size: 12px; font-weight: 600;")
            self.tray_status.setText(f"Connected: {display_name}")
            self.status_label.setText(f"Connected to {url}")
            self._log(f"Connected to {url} as {display_name}")

            # Load metadata for dropdowns
            self._load_meta()
            self._refresh_recent()

            # Register this tray instance with the server
            self._send_heartbeat()

            # Submit any pending laps
            if self.pending_laps:
                for lap in self.pending_laps:
                    self._submit_lap(lap)
                self.pending_laps.clear()

        except requests.exceptions.ConnectionError:
            self.connection_status.setText("Cannot reach server")
            self.connection_status.setStyleSheet("color: #e63946; font-size: 12px; font-weight: 600;")
            self._log(f"Connection failed: cannot reach {url}")
        except requests.exceptions.HTTPError as e:
            msg = "Invalid credentials" if e.response.status_code == 401 else str(e)
            self.connection_status.setText(msg)
            self.connection_status.setStyleSheet("color: #e63946; font-size: 12px; font-weight: 600;")
            self._log(f"Login failed: {msg}")
        except Exception as e:
            self.connection_status.setText(f"Error: {e}")
            self.connection_status.setStyleSheet("color: #e63946; font-size: 12px; font-weight: 600;")
            self._log(f"Connection error: {e}")

    def _check_connection(self):
        if self.api.is_connected():
            name = self.settings.value("display_name", "User")
            self.connection_status.setText(f"Connected as {name}")
            self.connection_status.setStyleSheet("color: #2ec866; font-size: 12px; font-weight: 600;")
            self.tray_status.setText(f"Connected: {name}")
            self.status_label.setText(f"Connected – watching for laps")
            self._load_meta()
            self._refresh_recent()
        else:
            self.connection_status.setText("Not connected")
            self.connection_status.setStyleSheet("color: #e63946; font-size: 12px; font-weight: 600;")
            self.status_label.setText("Not connected – go to Settings to connect")

    def _send_heartbeat(self):
        if not self.api.token or not self.api.base_url:
            return
        try:
            self.api.send_heartbeat(self.client_id)
        except Exception:
            # Heartbeat failures are expected when the network/server is
            # unreachable; the admin view will surface that as "Lost".
            pass

    def _submit_lap(self, lap: LapRecord):
        try:
            result = self.api.submit_lap(lap)
            self._log(f"Submitted: {lap.track} / {lap.car} – {lap.formatted_time()}")
        except Exception as e:
            self._log(f"Submit failed: {e}")
            self.pending_laps.append(lap)

    def _manual_submit(self):
        track = self.manual_track.currentText().strip()
        car = self.manual_car.currentText().strip()
        m = self.manual_min.value()
        s = self.manual_sec.value()
        ms = self.manual_ms.value()
        laptime_ms = m * 60000 + s * 1000 + ms

        if not track or not car or laptime_ms <= 0:
            self.manual_result.setText("Please fill in track, car, and time")
            self.manual_result.setStyleSheet("color: #e63946;")
            return

        lap = LapRecord(
            track=track,
            car=car,
            laptime_ms=laptime_ms,
            weather=self.manual_weather.currentText(),
            notes=self.manual_notes.text().strip(),
            recorded_at=datetime.now().isoformat()
        )

        if self.api.is_connected():
            self._submit_lap(lap)
            self.manual_result.setText(f"Submitted: {lap.formatted_time()} at {track}")
            self.manual_result.setStyleSheet("color: #2ec866;")
            # Reset time fields
            self.manual_min.setValue(0)
            self.manual_sec.setValue(0)
            self.manual_ms.setValue(0)
            self.manual_notes.clear()
            self._refresh_recent()
        else:
            self.pending_laps.append(lap)
            self.manual_result.setText("Queued (not connected)")
            self.manual_result.setStyleSheet("color: #f4a623;")

    def _load_meta(self):
        try:
            tracks = self.api.get_meta("tracks")
            cars = self.api.get_meta("cars")

            self.manual_track.clear()
            self.manual_track.addItems(tracks)

            self.manual_car.clear()
            self.manual_car.addItems(cars)
        except Exception:
            pass

    def _refresh_recent(self):
        if not self.api.is_connected():
            return
        try:
            laps = self.api.get_recent_laps(10)
            self.recent_table.setRowCount(len(laps))
            for i, lap in enumerate(laps):
                self.recent_table.setItem(i, 0, QTableWidgetItem(lap.get("track", "")))
                self.recent_table.setItem(i, 1, QTableWidgetItem(lap.get("car", "")))

                ms = lap.get("laptime_ms", 0)
                m = ms // 60000
                s = (ms % 60000) // 1000
                mil = ms % 1000
                time_str = f"{m}:{s:02d}.{mil:03d}"
                item = QTableWidgetItem(time_str)
                item.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
                self.recent_table.setItem(i, 2, item)

                recorded = lap.get("recorded_at", "")
                try:
                    dt = datetime.fromisoformat(recorded)
                    date_str = dt.strftime("%d %b %Y %H:%M")
                except (ValueError, TypeError):
                    date_str = recorded
                self.recent_table.setItem(i, 3, QTableWidgetItem(date_str))
        except Exception as e:
            self._log(f"Failed to refresh: {e}")

    # ── Settings Actions ─────────────────────────────────────────────

    def _browse_log_path(self):
        # Try directory first (ACE 0.6+), fall back to file picker (ACE 0.5.x)
        path = QFileDialog.getExistingDirectory(self, "Select ACE Logs folder")
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self, "Select ACE log.txt", "", "Log files (*.txt *.log);;All files (*)"
            )
        if path:
            self.log_path_input.setText(path)

    def _save_log_settings(self):
        path = self.log_path_input.text().strip()
        self.settings.setValue("log_path", path)
        self._start_watcher()
        self._log(f"Log path updated: {path}")

    def _update_log_exists(self):
        path = self.settings.value("log_path", DEFAULT_LOG_PATH)
        p = Path(path)
        if p.is_dir():
            files = list(p.glob("*.txt"))
            if files:
                self.log_exists_label.setText(f"Folder found ({len(files)} log file{'s' if len(files) != 1 else ''})")
            else:
                self.log_exists_label.setText("Folder found (no log files yet)")
            self.log_exists_label.setStyleSheet("color: #2ec866; font-size: 11px;")
        elif p.is_file():
            size = p.stat().st_size / 1024
            self.log_exists_label.setText(f"File found ({size:.0f} KB)")
            self.log_exists_label.setStyleSheet("color: #2ec866; font-size: 11px;")
        else:
            self.log_exists_label.setText("Path not found – will watch when it appears")
            self.log_exists_label.setStyleSheet("color: #f4a623; font-size: 11px;")

    def _toggle_auto_submit(self, index):
        self.auto_submit = index == 0
        self.settings.setValue("auto_submit", "true" if self.auto_submit else "false")

    # ── Utility ──────────────────────────────────────────────────────

    def _log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.activity_log.append(f"[{timestamp}] {message}")
        logger.info(message)

    def closeEvent(self, event):
        """Minimize to tray instead of closing."""
        event.ignore()
        self.hide()
        self.tray_icon.showMessage(
            APP_NAME,
            "Still running in the system tray",
            QSystemTrayIcon.MessageIcon.Information,
            2000
        )

    def _quit_app(self):
        if self.watcher:
            self.watcher.stop()
        if hasattr(self, "heartbeat_timer"):
            self.heartbeat_timer.stop()
        try:
            self.api.send_disconnect(self.client_id)
        except Exception:
            pass
        self.tray_icon.hide()
        QApplication.quit()


# ─── Entry point ─────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # Keep running in tray
    app.setWindowIcon(create_app_icon())

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
