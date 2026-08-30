"""
ACE Lap Tracker – Windows Tray App
Watches Assetto Corsa Evo log files for completed laps and submits them
to your ACE Lap Tracker backend on your homelab.
"""

import sys
import os
import re
import uuid
import socket
import platform
import logging
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QComboBox, QSystemTrayIcon,
    QMenu, QTableWidget, QTableWidgetItem, QHeaderView, QGroupBox,
    QFormLayout, QSpinBox, QTabWidget, QTextEdit,
    QFrame, QFileDialog
)
from PyQt6.QtCore import (
    Qt, QTimer, QThread, pyqtSignal, QSettings
)
from PyQt6.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QFont, QAction, QActionGroup, QBrush
)

import requests

# ─── Constants ───────────────────────────────────────────────────────

APP_NAME = "ACE Lap Tracker"
APP_VERSION = "1.4.0"
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
    # Set only when the lap belongs to someone other than the account the API
    # key was issued to. It lives on the record rather than being applied at
    # send time so that a lap queued while offline keeps whoever was actually
    # in the seat, however many times the driver changes before it goes up.
    user_id: Optional[int] = None

    def formatted_time(self) -> str:
        m = self.laptime_ms // 60000
        s = (self.laptime_ms % 60000) // 1000
        ms = self.laptime_ms % 1000
        return f"{m}:{s:02d}.{ms:03d}"


# ─── API Client ──────────────────────────────────────────────────────

def _is_plaintext_remote(url: str) -> bool:
    """
    True when the URL is unencrypted and points somewhere other than this
    machine. The API key travels on every upload, so it is worth saying so
    once — but not for a loopback address, where there is no network to
    listen on.
    """
    parsed = urlparse(url if "://" in url else f"http://{url}")
    if parsed.scheme == "https":
        return False
    host = (parsed.hostname or "").lower()
    return host not in ("localhost", "127.0.0.1", "::1", "")


class ClientIdConflict(Exception):
    """The server has this client_id registered to a different account."""


class APIClient:
    """
    Talks to the backend with an API key.

    An API key is scoped to lap upload only - it cannot change the account
    or reach admin endpoints - and can be revoked from the website without
    touching the password. A JWT is only ever held transiently, while
    exchanging a username/password for a key in provision_key().
    """

    def __init__(self):
        self.base_url = ""
        self.api_key = ""
        self.user_agent = f"ace-tray/{APP_VERSION} ({platform.system()} {platform.release()})"

    def configure(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _headers(self):
        return {
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
            "User-Agent": self.user_agent,
        }

    def verify_key(self, server_url: str, api_key: str) -> dict:
        """Check a key against /api/auth/me and adopt it on success."""
        base = server_url.rstrip("/")
        resp = requests.get(
            f"{base}/api/auth/me",
            headers={
                "Content-Type": "application/json",
                "X-API-Key": api_key,
                "User-Agent": self.user_agent,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        self.base_url = base
        self.api_key = api_key
        return data

    def provision_key(self, server_url: str, username: str, password: str) -> dict:
        """
        Fallback path: exchange a username/password for an API key.

        The password is never stored and the JWT is discarded as soon as the
        key has been minted, so the only long-lived secret on disk is a
        revocable, upload-only key.
        """
        base = server_url.rstrip("/")
        resp = requests.post(
            f"{base}/api/auth/login",
            json={"username": username, "password": password},
            headers={"User-Agent": self.user_agent},
            timeout=10,
        )
        resp.raise_for_status()
        jwt_token = resp.json()["token"]

        key_name = f"Tray on {socket.gethostname()}"
        resp = requests.post(
            f"{base}/api/keys",
            json={"name": key_name},
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {jwt_token}",
                "User-Agent": self.user_agent,
            },
            timeout=10,
        )
        resp.raise_for_status()
        api_key = resp.json()["key"]
        del jwt_token

        return self.verify_key(base, api_key)

    def submit_lap(self, lap: LapRecord) -> dict:
        url = f"{self.base_url}/api/laptimes"
        payload = asdict(lap)
        # Dropped for your own laps, so the request is byte-for-byte what an
        # older tray sent to a server that has never heard of driver switching.
        if payload.get("user_id") is None:
            payload.pop("user_id", None)
        resp = requests.post(url, json=payload, headers=self._headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_me(self) -> dict:
        url = f"{self.base_url}/api/auth/me"
        resp = requests.get(url, headers=self._headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_assignable_users(self) -> list:
        """
        Drivers this key may file a lap under: its owner, plus the members of
        any group the owner is a group admin of. A plain member's key gets a
        one-entry list, and a server too old to know the endpoint 404s - both
        end up with no driver switch, which is the right answer.
        """
        url = f"{self.base_url}/api/meta/assignable-users"
        resp = requests.get(url, headers=self._headers(), timeout=10)
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
        if not self.base_url or not self.api_key:
            return False
        try:
            url = f"{self.base_url}/api/auth/me"
            resp = requests.get(url, headers=self._headers(), timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def send_heartbeat(self, client_id: str) -> bool:
        if not self.base_url or not self.api_key:
            return False
        url = f"{self.base_url}/api/client/heartbeat"
        payload = {
            "client_id": client_id,
            "hostname": socket.gethostname(),
            "platform": f"{platform.system()} {platform.release()}",
            "app_version": APP_VERSION,
        }
        resp = requests.post(url, json=payload, headers=self._headers(), timeout=5)
        if resp.status_code == 409:
            # The server keeps a client_id to one account. Ours collides with
            # another user's — on a shared machine, or after a registry copy —
            # so the caller needs to mint a fresh one.
            raise ClientIdConflict()
        return resp.status_code == 200

    def send_disconnect(self, client_id: str) -> bool:
        if not self.base_url or not self.api_key:
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

    PRACTICE MODE (GameModeType_PRACTICE, incl. Time Attack):
      - Session: Game Started! ... N seconds @... | <car> | WeatherType_XXX
      - Splits:  On Split start <flag> end <flag> id <splitindex> splittime <ms>
                 (<flag> is an int on ACE 0.5/0.6, a bool on ACE 0.7+)
      - No car UUID (solo session); all splits are the player's.
      - Lap boundary: "Lap test evOnLapCompleted N completed"
      - Valid-lap marker (ACE 0.7+/0.8): "On Split end with all splits, id N"
        is logged only for genuine complete laps. Out/in-laps and interrupted
        laps (where the driver idled in a sector, so the sector time balloons)
        still log every sector and a lap-completed line, but NOT this marker.
        When the marker is present in a file we require it, which filters those
        bogus laps out; when it's absent (older ACE) we fall back to the
        "all sectors present" heuristic.

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

    # Practice split: no car UUID, different format.
    # ACE 0.5/0.6 logged the start/end fields as integers ("start 0 end 0");
    # ACE 0.7 changed them to booleans ("start false end true"). Match either
    # by accepting any non-space token for those two fields.
    RE_SPLIT_PRACTICE = re.compile(
        r'On Split start \S+ end \S+ id (\d+) splittime (\d+)'
    )

    # Lap completion marker (used in practice to know when a lap is done)
    RE_LAP_COMPLETED = re.compile(
        r'Lap test evOnLapCompleted (\d+) completed'
    )

    # Valid-lap marker (ACE 0.7+/0.8): logged only when a genuine complete lap
    # finished. Absent for out/in-laps and interrupted laps. Note this line has
    # no "start"/"splittime" tokens, so it never matches RE_SPLIT_PRACTICE.
    RE_SPLIT_ALL = re.compile(
        r'On Split end with all splits'
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
        # Whether this file's ACE version logs the "all splits" valid-lap
        # marker. Learned once per file (sticky across sessions) so we don't
        # regress older versions that never emit it.
        self._practice_marker_mode = False
        # Whether the valid-lap marker has been seen for the current lap.
        self._practice_lap_marked = False

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

            # Reset tracking for new session. Note: _practice_marker_mode is
            # left untouched — the ACE version doesn't change within a file, so
            # what we learned in an earlier session still applies here.
            self._race_splits = {}
            self._race_emitted = set()
            self._practice_splits = {}
            self._practice_lap_count = 0
            self._practice_lap_marked = False
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
        # Valid-lap marker — ACE 0.7+/0.8 logs this only for genuine complete
        # laps. It arrives with the final sector split, just before the
        # lap-completed line. Seeing it at all tells us this file's ACE version
        # emits the marker, so we can start requiring it.
        if self.RE_SPLIT_ALL.search(line):
            self._practice_marker_mode = True
            self._practice_lap_marked = True
            return

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
                has_all_sectors = expected.issubset(self._practice_splits.keys())
                # On marker-emitting ACE versions, require the marker: it's the
                # game's own "this was a valid complete lap" signal and rejects
                # out/in-laps and interrupted laps (idle time inflating a sector)
                # that still happen to have every sector logged. On older
                # versions that never emit it, fall back to sectors-present only.
                marker_ok = self._practice_lap_marked or not self._practice_marker_mode
                if has_all_sectors and marker_ok:
                    # Complete lap — all sectors present (and marker seen, if used)
                    total_ms = sum(self._practice_splits[i] for i in range(self._max_splitindex + 1))
                    self._emit_lap(total_ms, dict(self._practice_splits), self._practice_lap_count)
                elif has_all_sectors and not marker_ok:
                    # All sectors present but no valid-lap marker — out/in-lap or
                    # an interrupted lap. Skip it.
                    logger.info(
                        f"Skipping unmarked practice lap {self._practice_lap_count} "
                        f"(no 'all splits' marker; likely out/in-lap or interrupted)"
                    )
                    self.status_changed.emit(
                        "Lap skipped (out/in-lap or interrupted)"
                    )
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
                self._practice_lap_marked = False

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

        # Who the next detected lap is filed under. `me_id` is the account the
        # API key belongs to; `active_driver_id` is whoever is actually in the
        # seat, which is the same person until someone switches it. The choice
        # survives a restart, but is dropped the moment the server stops
        # offering that driver - a group change or a different key must not
        # leave laps quietly going to a name that is no longer allowed.
        self.me_id = self.settings.value("user_id", 0, type=int) or None
        self.drivers = []
        self.active_driver_id = self.settings.value("active_driver_id", 0, type=int) or None
        self.driver_actions = []
        self.driver_action_group = None
        self._connected = False

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
        saved_key = self.settings.value("api_key", "")
        if saved_url and saved_key:
            self.api.configure(saved_url, saved_key)
        elif saved_url:
            # Upgrading from a version that stored a JWT: trade it for an API
            # key while it is still valid, so the user notices nothing.
            self._migrate_legacy_token(saved_url)

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

        # Who laps are filed under. Hidden entirely unless the server offers
        # more than one driver, so a solo setup sees no extra control.
        self.driver_group = QGroupBox("Driver")
        driver_layout = QHBoxLayout(self.driver_group)
        self.driver_combo = QComboBox()
        self.driver_combo.currentIndexChanged.connect(self._on_driver_combo_changed)
        driver_layout.addWidget(self.driver_combo, 1)
        self.driver_group.setVisible(False)
        layout.addWidget(self.driver_group)

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

        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.setPlaceholderText("alt_...  (create one on the website)")
        self.api_key_input.setText(self.settings.value("api_key", ""))
        server_form.addRow("API Key:", self.api_key_input)

        key_hint = QLabel(
            "Create a key on the website under My Profile \u2192 API Keys, "
            "then paste it here."
        )
        key_hint.setWordWrap(True)
        key_hint.setStyleSheet("color: #8a8a9a; font-size: 11px;")
        server_form.addRow("", key_hint)

        # Fallback for anyone upgrading from a password-based install: sign in
        # once and the app mints a key for itself, so the password is never
        # written to disk.
        self.pwd_toggle = QPushButton("Or sign in with username & password")
        self.pwd_toggle.setCheckable(True)
        self.pwd_toggle.setFlat(True)
        self.pwd_toggle.setStyleSheet(
            "text-align: left; color: #4a9eff; font-size: 11px; border: none;"
        )
        self.pwd_toggle.toggled.connect(self._toggle_password_fields)
        server_form.addRow("", self.pwd_toggle)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Your username")
        self.username_input.setText(self.settings.value("username", ""))
        self.username_label = QLabel("Username:")
        server_form.addRow(self.username_label, self.username_input)

        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("Your password (never stored)")
        self.password_label = QLabel("Password:")
        server_form.addRow(self.password_label, self.password_input)

        for w in (self.username_label, self.username_input,
                  self.password_label, self.password_input):
            w.setVisible(False)

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

        # Same switch as the dashboard combo, without opening the window -
        # which is the point on a gaming PC when someone else takes the seat.
        self.driver_menu = tray_menu.addMenu("Driving as")
        self.driver_menu.menuAction().setVisible(False)

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
            recorded_at=datetime.now().isoformat(),
            user_id=self._lap_user_id(),
        )

        # Update dashboard
        self.last_lap_label.setText(lap.formatted_time())
        self.last_lap_info.setText(f"{track}  ·  {car}")

        # Update session counter
        current = int(self.session_laps_label.text())
        self.session_laps_label.setText(str(current + 1))

        self._log(f"Lap detected: {track} / {car} – {lap.formatted_time()}")

        # Tray notification. It names the driver when it is not you, since
        # that is the moment a forgotten switch is worth catching.
        driver_note = f" · {self._active_driver_name()}" if lap.user_id else ""
        self.tray_icon.showMessage(
            "Lap Recorded" if self.auto_submit else "Lap Detected",
            f"{lap.formatted_time()} at {track}{driver_note}",
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

    def _toggle_password_fields(self, checked: bool):
        for w in (self.username_label, self.username_input,
                  self.password_label, self.password_input):
            w.setVisible(checked)
        self.pwd_toggle.setText(
            "Use an API key instead" if checked
            else "Or sign in with username & password"
        )

    def _set_status(self, text: str, ok: bool = False):
        colour = "#2ec866" if ok else "#e63946"
        self.connection_status.setText(text)
        self.connection_status.setStyleSheet(
            f"color: {colour}; font-size: 12px; font-weight: 600;"
        )

    def _migrate_legacy_token(self, saved_url: str):
        """
        One-time upgrade path. Older builds stored a 30-day JWT; if one is
        still present and valid, mint an API key with it and drop the JWT.
        Silent on failure - the user just reconnects from Settings.
        """
        legacy = self.settings.value("token", "")
        if not legacy:
            return
        try:
            resp = requests.post(
                f"{saved_url.rstrip('/')}/api/keys",
                json={"name": f"Tray on {socket.gethostname()}"},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {legacy}",
                    "User-Agent": self.api.user_agent,
                },
                timeout=10,
            )
            resp.raise_for_status()
            api_key = resp.json()["key"]
        except Exception as e:
            self._log(f"Could not upgrade saved login to an API key: {e}")
            self.settings.remove("token")
            return

        self.api.configure(saved_url, api_key)
        self.settings.setValue("api_key", api_key)
        self.settings.remove("token")
        self._log("Upgraded saved login to an API key - your password is no longer needed")

    def _connect_to_server(self):
        url = self.server_url_input.text().strip()
        api_key = self.api_key_input.text().strip()
        use_password = self.pwd_toggle.isChecked()
        username = self.username_input.text().strip()
        password = self.password_input.text()

        if not url:
            self._set_status("Enter the server URL")
            return
        insecure = _is_plaintext_remote(url)
        if use_password:
            if not username or not password:
                self._set_status("Enter your username and password")
                return
        elif not api_key:
            self._set_status("Paste your API key, or sign in with a password")
            return

        try:
            if use_password:
                # Mints a key server-side; the password is discarded here.
                data = self.api.provision_key(url, username, password)
                self._log("Signed in and created a new API key for this PC")
            else:
                data = self.api.verify_key(url, api_key)
            display_name = data["display_name"]

            # Only the API key is persisted - never the password.
            self.settings.setValue("server_url", url)
            self.settings.setValue("username", data.get("username", username))
            self.settings.setValue("api_key", self.api.api_key)
            self.settings.setValue("display_name", display_name)
            self.settings.remove("token")  # drop any legacy JWT from an older version

            # A key for a different account invalidates any saved driver
            # choice, so reset before the new list is fetched below.
            if data.get("id") and data["id"] != self.me_id:
                self.me_id = data["id"]
                self.active_driver_id = self.me_id
            self.settings.setValue("user_id", self.me_id or 0)

            # Reflect the provisioned key back into the UI and clear the password.
            self.api_key_input.setText(self.api.api_key)
            self.password_input.clear()
            if use_password:
                self.pwd_toggle.setChecked(False)

            if insecure:
                self._log(
                    "Note: this server uses plain HTTP, so your API key is sent "
                    "unencrypted. Fine on your own network - see docs/TLS.md before "
                    "using it across the internet."
                )
            self._connected = True
            self.status_label.setText(f"Connected to {url}")
            self._log(f"Connected to {url} as {display_name}")

            # Load metadata for dropdowns
            self._load_meta()
            self._load_drivers()   # also refreshes the status lines above
            self._refresh_recent()

            # Register this tray instance with the server
            self._send_heartbeat()

            # Submit any pending laps
            if self.pending_laps:
                for lap in self.pending_laps:
                    self._submit_lap(lap)
                self.pending_laps.clear()

        except requests.exceptions.ConnectionError:
            self._set_status("Cannot reach server")
            self._log(f"Connection failed: cannot reach {url}")
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code == 401:
                msg = ("Invalid username or password" if use_password
                       else "Invalid, revoked or expired API key")
            elif code == 403:
                msg = "This key is not allowed to do that"
            elif code == 404 and use_password:
                msg = "Server too old - it has no API key support yet"
            else:
                msg = str(e)
            self._set_status(msg)
            self._log(f"Connect failed: {msg}")
        except Exception as e:
            self._set_status(f"Error: {e}")
            self._log(f"Connection error: {e}")

    def _check_connection(self):
        if self.api.is_connected():
            self._connected = True
            self.status_label.setText("Connected – watching for laps")
            self._load_meta()
            self._load_drivers()   # sets the connection and tray status lines
            self._refresh_recent()
        else:
            self._connected = False
            self.connection_status.setText("Not connected")
            self.connection_status.setStyleSheet("color: #e63946; font-size: 12px; font-weight: 600;")
            self.status_label.setText("Not connected – go to Settings to connect")

    def _send_heartbeat(self):
        if not self.api.api_key or not self.api.base_url:
            return
        try:
            self.api.send_heartbeat(self.client_id)
        except ClientIdConflict:
            # Take a new identity and let the next tick register it.
            self.client_id = str(uuid.uuid4())
            self.settings.setValue("client_id", self.client_id)
            self._log("This client ID was already registered to another account - issued a new one")
        except Exception:
            # Heartbeat failures are expected when the network/server is
            # unreachable; the admin view will surface that as "Lost".
            pass

    def _submit_lap(self, lap: LapRecord):
        try:
            self.api.submit_lap(lap)
            who = f" for {self._driver_name(lap.user_id)}" if lap.user_id else ""
            self._log(f"Submitted{who}: {lap.track} / {lap.car} – {lap.formatted_time()}")
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
            recorded_at=datetime.now().isoformat(),
            user_id=self._lap_user_id(),
        )

        if self.api.is_connected():
            self._submit_lap(lap)
            who = f" for {self._active_driver_name()}" if lap.user_id else ""
            self.manual_result.setText(f"Submitted{who}: {lap.formatted_time()} at {track}")
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

    # ── Driver switching ─────────────────────────────────────────────
    #
    # The API key stays the one issued to this Windows account. Switching the
    # driver only changes whose name the lap is filed under, and the server
    # allows it exactly as far as the key's owner group-admins - so a plain
    # member's key never gets a switch to offer.

    def _driver_name(self, user_id) -> str:
        match = next((d for d in self.drivers if d["id"] == user_id), None)
        if match:
            return match["display_name"]
        return self.settings.value("display_name", "User")

    def _active_driver_name(self) -> str:
        return self._driver_name(self.active_driver_id)

    def _lap_user_id(self) -> Optional[int]:
        """The id to file the next lap under, or None to use the key's own."""
        if self.active_driver_id and self.me_id and self.active_driver_id != self.me_id:
            return self.active_driver_id
        return None

    def _connection_label(self) -> str:
        owner = self.settings.value("display_name", "User")
        if self._lap_user_id():
            return f"{owner} (driving as {self._active_driver_name()})"
        return owner

    def _load_drivers(self):
        try:
            # Upgrading from a version that never stored it - including the
            # legacy-token migration, which connects without going through
            # Settings - leaves the owning account unknown. Without it a
            # switch cannot tell "me" from "someone else", so establish it
            # before the list is any use.
            if self.me_id is None:
                self.me_id = self.api.get_me().get("id")
                self.settings.setValue("user_id", self.me_id or 0)
            drivers = self.api.get_assignable_users()
        except Exception:
            # No connection, or a server too old to know the endpoint. Either
            # way there is nothing to switch between.
            drivers = []
        self._apply_driver_list(drivers)

    def _apply_driver_list(self, drivers: list):
        self.drivers = drivers
        ids = {d["id"] for d in drivers}
        # A driver the server no longer offers is one we may no longer file
        # under, so the saved choice is dropped rather than kept and refused.
        if self.active_driver_id is not None and self.active_driver_id not in ids:
            if self.active_driver_id != self.me_id:
                self._log("Saved driver is no longer available – laps go under your own name")
            self.active_driver_id = None
        if self.active_driver_id is None:
            self.active_driver_id = self.me_id
        self.settings.setValue("active_driver_id", self.active_driver_id or 0)
        self._rebuild_driver_widgets()

    def _driver_label(self, driver: dict) -> str:
        return driver["display_name"] + (" (you)" if driver["id"] == self.me_id else "")

    def _rebuild_driver_widgets(self):
        # One driver means no choice to make, so neither control appears -
        # and neither does it while we do not know which of them is us.
        show = self.me_id is not None and len(self.drivers) > 1
        self.driver_group.setVisible(show)
        self.driver_menu.menuAction().setVisible(show)

        self.driver_combo.blockSignals(True)
        self.driver_combo.clear()
        for d in self.drivers:
            self.driver_combo.addItem(self._driver_label(d), d["id"])
        index = self.driver_combo.findData(self.active_driver_id)
        if index >= 0:
            self.driver_combo.setCurrentIndex(index)
        self.driver_combo.blockSignals(False)

        self.driver_menu.clear()
        self.driver_actions = []
        # Parented to the group, so replacing the group on the next reconnect
        # takes the old actions with it instead of piling them up on the window.
        self.driver_action_group = QActionGroup(self)
        self.driver_action_group.setExclusive(True)
        for d in self.drivers:
            action = QAction(self._driver_label(d), self.driver_action_group)
            action.setCheckable(True)
            action.setData(d["id"])
            action.setChecked(d["id"] == self.active_driver_id)
            action.triggered.connect(lambda _checked, uid=d["id"]: self._set_active_driver(uid))
            self.driver_action_group.addAction(action)
            self.driver_menu.addAction(action)
            self.driver_actions.append(action)

        self._update_driver_labels()

    def _on_driver_combo_changed(self, index: int):
        if index >= 0:
            self._set_active_driver(self.driver_combo.itemData(index))

    def _set_active_driver(self, user_id):
        if user_id is None or user_id == self.active_driver_id:
            return
        # Only a driver the server currently offers. The widgets are built
        # from that same list, so this only bites if one goes stale.
        if not any(d["id"] == user_id for d in self.drivers):
            return
        self.active_driver_id = user_id
        self.settings.setValue("active_driver_id", user_id)

        index = self.driver_combo.findData(user_id)
        if index >= 0 and index != self.driver_combo.currentIndex():
            self.driver_combo.blockSignals(True)
            self.driver_combo.setCurrentIndex(index)
            self.driver_combo.blockSignals(False)
        for action in self.driver_actions:
            action.setChecked(action.data() == user_id)

        name = self._active_driver_name()
        self._log(f"Driver switched to {name} – laps are filed under that name")
        self.tray_icon.showMessage(
            APP_NAME, f"Now driving as {name}",
            QSystemTrayIcon.MessageIcon.Information, 2500
        )
        self._update_driver_labels()

    def _update_driver_labels(self):
        """Keep the tray tooltip and status lines honest about who laps go to."""
        driver = self._active_driver_name()
        self.tray_icon.setToolTip(
            f"{APP_NAME} — driving as {driver}" if self._lap_user_id() else APP_NAME
        )
        if self._connected:
            label = self._connection_label()
            self.tray_status.setText(f"Connected: {label}")
            self.connection_status.setText(f"Connected as {label}")
            self.connection_status.setStyleSheet("color: #2ec866; font-size: 12px; font-weight: 600;")

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
