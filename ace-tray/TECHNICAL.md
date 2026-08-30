# ACE Tray — Technical Documentation

> **Version 3.0.0** — [User Guide](README.md)

This document covers the internal architecture, log parsing logic, configuration storage, and extension points for the tray app.

---

## Architecture

```
┌──────────────┐   watches (every 2s)   ┌──────────────┐   HTTP POST    ┌──────────────┐
│  Assetto     │ ─────────────────────▶ │  Tray App    │ ─────────────▶ │  ACE Lap     │
│  Corsa Evo   │   log.txt              │  (this app)  │  /api/laptimes │  Tracker API │
└──────────────┘                        └──────────────┘                └──────────────┘
```

The app runs entirely on the local Windows machine. It has no server component of its own — it is purely a client that reads a local file and calls the ACE Lap Tracker REST API.

---

## Stack

| Component | Technology |
|-----------|-----------|
| GUI framework | PyQt6 6.6+ |
| HTTP client | requests 2.31+ |
| Log monitoring | Python file tail loop (2s interval) |
| Config storage | JSON file, per Windows user profile |
| Packaging | Python venv + `start.bat` launcher |

---

## File Structure

```
ace-tray/
├── ace_tray.py            # Main application — all logic in a single file
├── requirements.txt       # Python dependencies
├── start.bat              # First-run installer + launcher
└── install_autostart.bat  # Windows startup installer
```

All logic lives in `ace_tray.py`. It is a single-file PyQt6 application with no external modules beyond the declared dependencies.

---

## Configuration Storage

Settings are stored as JSON at:

```
%APPDATA%\ACETray\config.json
```

Because `%APPDATA%` is user-scoped (`C:\Users\<username>\AppData\Roaming`), each Windows user has an independent configuration file. This is what enables per-driver credentials on a shared PC.

Example config structure:

```json
{
  "server_url": "http://your-server-ip:8099",
  "username": "driver1",
  "password": "...",
  "log_path": "C:\\Users\\driver1\\Saved Games\\ACE\\Logs\\log.txt",
  "auto_submit": true
}
```

---

## Log File Monitoring

The watcher runs in a background thread and polls ACE's `log.txt` every 2 seconds using a tail-style approach (tracking file position and reading new lines only).

ACE's log file is located at:

```
C:\Users\<WindowsUser>\Saved Games\ACE\Logs\log.txt
```

The watcher parses three types of log entries in order:

### 1. Session Start

Detected by the `Game Started!` line. Extracts track name, car ID, and weather condition.

**Regex pattern (`RE_SESSION_START`):**

```
Game Started! GameModeType_\w+ \| (.+?) \.\.\. \| (\w+) \| GameModeSelectionWeatherType_(\w+)
```

Capture groups: `track_name`, `car_id`, `weather`

### 2. Player Car Assignment

Identifies which car UUID belongs to the local player. Needed to filter out other AI or multiplayer car entries from split time lines.

**Regex pattern (`RE_PLAYER_CAR`):**

```
onSetPlayerCurrentCarCommand: Set new car ([0-9a-f-]+) content\\cars\\(\w+)\\
```

Capture groups: `car_uuid`, `car_id`

### 3. Split Times

Each sector completion is logged as a split. The watcher collects splits for the player's car UUID only, and sums them when all sectors are complete to produce the full lap time.

**Regex pattern (`RE_SPLIT`):**

```
Split completed for car ([0-9a-f-]+): \((\d+) ms, splitindex (\d+)\) lap:(\d+)
```

Capture groups: `car_uuid`, `time_ms`, `split_index`, `lap_number`

When all expected splits for a lap arrive, the watcher:
1. Sums the sector times to get the total lap time in milliseconds
2. Converts to `mm:ss.mmm` format
3. Checks whether this is an outlap (see below)
4. If valid, submits to the API or queues for later

### Outlap Filtering

The first lap of a session (outlap / formation lap) typically has an inflated time due to the standing start. The watcher skips lap number `0` (or the first lap index per session, depending on the ACE version) to avoid logging formation laps.

### Practice / Time Attack Valid-Lap Marker (ACE 0.7+/0.8)

In practice and Time Attack sessions, splits are logged without a car UUID (`On Split start … end … id N splittime <ms>`) and a lap boundary is marked by `Lap test evOnLapCompleted N completed`. "All sectors present" is **not** sufficient to trust a lap: out/in-laps and interrupted laps (where the driver idled in a sector, ballooning that sector's time) still log every sector plus a lap-completed line, and would otherwise be recorded as very slow laps.

ACE 0.7+/0.8 additionally logs `On Split end with all splits, id N` **only** for genuine complete laps. The watcher requires this marker on any file where it appears (learned once per file, sticky across sessions). On older ACE versions that never emit it, the watcher falls back to the sectors-present heuristic, so behavior is unchanged there.

---

## Car Name Formatting

Internal ACE car IDs follow a `ks_make_model_variant` pattern. The app auto-formats these for display:

- Strip the `ks_` prefix
- Replace underscores with spaces
- Title-case each word

Example: `ks_ferrari_f2004` → `Ferrari F2004`

This formatting is applied when submitting to the API (as the `car` field) and when displaying in the dashboard.

---

## API Integration

The tray app uses the ACE Lap Tracker REST API. The relevant endpoints it calls:

| Method | Endpoint | When |
|--------|----------|------|
| POST | `/api/auth/login` | On "Connect" in Settings |
| GET | `/api/auth/me` | To verify the token is still valid |
| POST | `/api/laptimes` | After each detected or manually entered lap |
| GET | `/api/laptimes` | To populate the Dashboard's recent laps list |
| GET | `/api/meta/assignable-users` | On connect, to fill the "Driving as" picker |

Authentication uses a JWT token obtained at login. The token is stored in memory (not persisted to disk) and re-acquired on next launch.

### Driver Switching

`GET /api/meta/assignable-users` returns the drivers this key may file a lap
under: its owner, plus every member of a group the owner is a group admin of.
One entry back means no switching, and both the tray submenu and the Dashboard
picker stay hidden. A server too old for the endpoint 404s, which lands in the
same place.

The chosen id is saved as `active_driver_id` and sent as `user_id` on the lap
POST — omitted entirely when it is the key's own account, so a lap you drive
yourself is the same request an older tray sent. It is stamped onto the
`LapRecord` at detection time rather than at send time, so a lap queued while
the server is unreachable keeps whoever was actually in the seat even if the
driver changes before the queue drains.

The server is the authority: it re-checks the group-admin rule on every POST
and rejects anything else with `403`. A saved id the endpoint no longer returns
— a group change, or a key for a different account — is dropped on the next
connect and the app falls back to the key's own owner.

### Offline Queue

If a POST to `/api/laptimes` fails (network error, server unreachable), the lap is added to an in-memory queue. The app retries queued laps periodically. **Queued laps are lost if the app is closed before they are submitted** — this is a known limitation.

---

## Updating Log Patterns

If a future ACE update changes the log format, the regex patterns at the top of `ace_tray.py` will need updating:

```python
RE_SESSION_START = re.compile(r"...")
RE_PLAYER_CAR    = re.compile(r"...")
RE_SPLIT         = re.compile(r"...")
```

To diagnose a format change:
1. Open the **Activity Log** tab — it shows every line the watcher processes
2. Open `log.txt` manually and locate the relevant lines
3. Update the regex to match the new format

The **Activity Log** tab is the primary debugging tool. Enable verbose mode in settings if available to see raw log lines.

---

## Dependencies

```
PyQt6>=6.6.0
requests>=2.31.0
```

No other runtime dependencies. The Python standard library covers file I/O, JSON, regex, threading, and datetime handling.

To update dependencies:

```bash
# Inside the venv
pip install --upgrade PyQt6 requests
```

---

## Building a Standalone Executable (Optional)

The app can be packaged as a single `.exe` using PyInstaller, removing the Python requirement for end users:

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name ACETray ace_tray.py
```

The resulting `dist/ACETray.exe` can be distributed without any Python installation. Note that `start.bat` would no longer be needed in this case.
