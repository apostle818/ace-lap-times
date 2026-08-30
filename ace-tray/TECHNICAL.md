# ACE Tray — Technical Documentation

> **Version 1.4.0** — [User Guide](README.md)

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
| Config storage | `QSettings` — Windows registry, per Windows user account |
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

Settings go through `QSettings(ORG_NAME, APP_NAME)`, which on Windows is the
registry under:

```
HKCU\Software\ACELaps\ACE Lap Tracker
```

`HKCU` is the *current user's* hive, so each Windows account has an
independent store. That is what enables per-driver setup on a shared PC —
each account connects with its own API key and tracks separately.

| Key | Written by | Holds |
|-----|-----------|-------|
| `server_url` | Connect | Base URL of the backend |
| `api_key` | Connect | The `alt_...` key, the only long-lived secret on disk |
| `username` | Connect | Echoed back from the server, for display |
| `display_name` | Connect | Echoed back from the server, for display |
| `user_id` | Connect | The account the key belongs to; recovered from `/api/auth/me` if missing |
| `active_driver_id` | "Driving as" | Who laps are currently filed under |
| `client_id` | First launch | UUID identifying this tray instance to the heartbeat |
| `log_path` | Settings tab | ACE `Logs` directory, or a single `log.txt` |
| `auto_submit` | Settings tab | `"true"` / `"false"` |

**No password is ever written.** On the password fallback path the credentials
are exchanged for an API key and discarded in memory; only the key is stored.
A `token` key from a pre-API-key build is deleted on sight — see
`_migrate_legacy_token()`, which trades a still-valid JWT for a key once and
then removes it.

Because this is the registry and not a file, there is nothing to copy between
machines: to move a setup, create a new key on the website and paste it in.
Copying the store wholesale also duplicates `client_id`, which the server
refuses with a `409` — the app then mints itself a fresh one.

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
| GET | `/api/auth/me` | On "Connect" to verify the key; then before each submit or refresh, as the connection check |
| POST | `/api/laptimes` | After each detected or manually entered lap |
| GET | `/api/laptimes` | To populate the Dashboard's recent laps list |
| GET | `/api/meta/tracks`, `/api/meta/cars` | To fill the Manual Entry dropdowns |
| GET | `/api/meta/assignable-users` | On connect, to fill the "Driving as" picker |
| POST | `/api/client/heartbeat` | Every 30s, so the admin Connected Clients view can see this instance |
| POST | `/api/client/disconnect` | On quit |
| POST | `/api/auth/login` then `/api/keys` | Password fallback only — see below |

Authentication is an **API key**, sent as `X-API-Key: alt_...` on every request
(`APIClient._headers()`). The key is upload-only: the backend accepts it on a
short allowlist of endpoints — everything in the table above except the two
password-fallback rows, which are JWT routes — and answers `403` everywhere
else. It carries no role either, so even a superadmin's key cannot reach an
admin route, and it is revocable from the website without touching the account
password. The root [README](../README.md#authentication) has the authoritative
list.

A JWT appears in exactly one place. If you use *"Or sign in with username &
password"* in Settings, `provision_key()` posts the credentials to
`/api/auth/login`, uses the returned token once to `POST /api/keys`, and
discards both the token and the password — so the only thing that reaches
disk is the key. That path exists for upgrades and for anyone who would
rather not visit the website; pasting a key created under **My Profile → API
Keys** is the normal route.

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

If a POST to `/api/laptimes` fails (network error, server unreachable), the
lap is added to an in-memory queue, as is any lap detected while auto-submit
is off or the app is disconnected.

The queue is flushed on a successful **Connect** in Settings — and only
there. There is no periodic retry, so a tray that loses the server mid-session
holds its laps until you reconnect by hand. **Queued laps are lost if the app
is closed before they are submitted.** Both are known limitations.

A queued lap keeps the `user_id` it was detected under, so switching driver
before the queue drains does not re-attribute laps somebody else drove.

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
