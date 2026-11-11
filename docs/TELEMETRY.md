# Telemetry — design plan

> Status: **phase 0 (spike)**. Nothing below phase 0 is built yet. This
> document is the plan of record; rewrite it when Assetto Corsa Rally is
> onboarded (see the ACR section) rather than bolting rally onto it.

## Context
Today the tray app only parses ACE's `log.txt` (sector splits → lap time). That shows *who* is faster, not *where* or *why*. The goal is to record per-lap telemetry (speed, throttle, brake, steering, gear, position on track) and compare two laps on the same track/car side by side: speed in corners, braking points, throttle pickup, time won or lost per corner.

Assetto Corsa Rally (ACR) is coming later, so everything below the tray's game reader stays game-neutral. Rally changes the *domain* (stages, not laps). It does not change the telemetry pipeline.

## Research summary: best practices this plan follows
- **Source: shared memory, not logs.** ACE publishes `Local\acevo_pmf_physics` / `_graphics` / `_static` (Kunos guide: Steam "Shared Memory Documentation"). The first 416 bytes match AC1's layout, followed by ACE-only fields. The layout still changes between Early Access builds, so check the struct size and version and fail soft. ACR uses the **AC1 `Local\acpmf_*` layout**. That means one reader with two struct adapters.
- **Index by distance, not time.** Every serious tool (MoTeC i2, Garage 61, Track Titan, VRS) lines laps up by position along the track. Resample each lap onto a fixed grid of normalised track position (`normalizedCarPosition` × track length, e.g. every 2 m). Comparing two laps is then a sample-by-sample operation.
- **Delta-time trace** = cumulative time difference along distance. It is the single most useful chart: its slope shows exactly where time goes.
- **Corner segmentation** comes from local speed minima plus steering magnitude. For each corner: entry speed, minimum speed, exit speed, braking-start distance, throttle-pickup distance, time delta over the segment. This turns graphs into actionable text ("T3: you brake 18 m earlier, min speed −6 km/h, −0.21 s").
- **Track map** drawn from world X/Z, coloured by delta or speed. It needs no extra asset.
- **Keep it small.** About 10 channels × ~2 500 samples as int16 is ~50 KB raw, roughly 15–25 KB after zlib. Retention matters more than per-lap size.

## Design

### 1. Tray: capture (`ace-tray/ace_tray.py`)
- New `TelemetryReader(QThread)` opens shared memory with stdlib `mmap.mmap(-1, size, tagname=...)` plus `ctypes.Structure`. **No new dependency**, so the `pip-audit` scope is unchanged.
- Samples at ~50 Hz. Fields: `normalizedCarPosition`, `completedLaps`, `iCurrentTime`, `isValidLap`, `speedKmh`, `gas`, `brake`, `steerAngle`, `gear`, `rpms`, `accG[3]`, `carCoordinates` x/z.
- A game adapter per title (`AceAdapter` now, `AcrAdapter` later) maps the raw struct to a neutral sample dict. Checks static-page `smVersion` and the struct size. On mismatch, telemetry is disabled with an activity-log message and lap timing carries on from the log.
- **Lap boundaries stay owned by the existing `LogWatcher`.** It is proven, and its valid-lap handling (`On Split end with all splits`) is subtle. The reader buffers samples per `completedLaps` value. When `_on_lap_detected` (≈l.1275) fires, it takes the buffer of the lap that just closed and attaches it to the `LapRecord` (l.70). Discard the buffer if its duration differs from the log lap time by more than ~1 %. A wrong trace is worse than none.
- Resample client-side onto the distance grid. Encode as a versioned binary blob: header (format version, game, sample count, track length, channel list), then int16 columns with a fixed scale per channel. Send base64 in JSON.
- Settings toggle "Record telemetry" (default on) and a "Telemetry: active / unsupported version" status indicator.
- The offline queue holds the blob alongside the lap (same lifetime limits as today).

### 2. Backend (`ace-laptimes/backend/app.py`)
- New table `lap_telemetry(lap_id PK FK→laptimes ON DELETE CASCADE, format_version, game, sample_count, track_length_m, channels TEXT, data BLOB, created_at)`. `data` is zlib-compressed **by the server**. The client sends uncompressed int16 so the server never inflates untrusted input (no zip-bomb path).
- `POST /api/laptimes/<id>/telemetry`: `@token_or_key_required`, added to the tray key allowlist. Authorise with `_may_act_for(lap.user_id)`. Validate strictly: known format version, known channel names only, `sample_count ≤ 5000`, and decoded length exactly `= samples × channels × 2`. Rate-limited. A separate request (not part of the lap POST), so the lap is never lost if telemetry fails. It stays under the existing 256 KB `MAX_CONTENT_LENGTH` / nginx `client_max_body_size`, so neither changes.
- `GET /api/laptimes/<id>/telemetry`: `@token_required` (an API key has no reason to read it). Visibility goes through `_can_view_user(lap.user_id)`.
- `GET /api/laptimes` / leaderboard rows get a `has_telemetry` flag (same `_visibility_clause`).
- **Retention / disk-fill bound** (CLAUDE.md: single unquota'd SQLite). Per user+track+car, keep telemetry for the PB plus the last N (default 20) laps. Prune on insert. Add a global hard cap as an env var, e.g. `TELEMETRY_MAX_MB`. A full cap refuses new uploads and never deletes PBs.
- Deleting or reassigning a lap carries its telemetry with it (FK cascade; reassignment changes only `laptimes.user_id`).
- No CSV export of telemetry in v1. If added later, the numeric-only columns need no `_csv_safe`, but the header strings do.

### 3. Frontend (`ace-laptimes/frontend/app.js`, Chart.js already vendored)
- Lap rows with `has_telemetry` get an "Analyse" action, which opens a new `analysis` page.
- Pick a reference: "my PB", "fastest visible lap on this track/car", or any visible lap with telemetry.
- Decode the blob in the browser (DataView over int16). Comparison runs client-side, with no server compute.
- Stacked charts sharing one distance X axis and a synced crosshair: **delta time**, speed, throttle/brake, steering, gear.
- Track map on a `<canvas>` from X/Z, coloured by delta. Hovering the map moves the chart crosshair.
- Corner table (auto-detected): min speed A vs B, brake point Δm, throttle pickup Δm, segment Δs. Sorted by time lost, i.e. "where to look first".
- Every server-supplied string (driver, track, car) goes through `escapeHtml`. No CSP change.

### 4. Assetto Corsa Rally — what changes when it lands
Expect a plan rewrite here; the parts above are built to survive it.
- **Reusable as-is:** the blob format (already carries `game`), distance-indexed resampling, delta/overlay charts, storage, auth model.
- **Changes:** ACR uses the AC1 `acpmf_*` struct, so add an `AcrAdapter`. Point-to-point stages have no lap wrap: distance is stage distance from start, and there is no `normalizedCarPosition` loop. Lap detection has no log equivalent yet, so ACR stage start/finish must come from shared memory (session/status fields). Domain additions: `laptimes` needs a `game` + `kind` (lap|stage) column, or a parallel `stage_times` table. Rally-specific channels are worth adding (surface, wheel slip, handbrake, suspension travel). Corner detection needs a different heuristic: rally "corners" are dense and the steering range is huge.
- **Do now to make that cheap:** put `game` in the blob header and the `lap_telemetry` row from day one; keep channel names neutral; keep adapters behind one interface.

## Phasing
0. **Spike (on a Windows PC with ACE; it cannot run in this cloud container).** A debug option dumps raw shared-memory samples to a local CSV for a few laps. It confirms the struct layout and update rate on the current ACE build, and that `completedLaps` lines up with the log's lap events. Go/no-go for the rest.
1. Capture + upload + storage + retention + single-lap view.
2. Two-lap comparison: delta, overlays, track map.
3. Corner table.
4. ACR (re-plan).

Each phase is its own PR, per CLAUDE.md. Telemetry ships in a release, so bump the tray version in all three places (`APP_VERSION`, `ace-tray/README.md`, `TECHNICAL.md`) to the release number.

## Critical files
- `ace-tray/ace_tray.py`: new `TelemetryReader`, adapters, hook into `_on_lap_detected` / `LapRecord`, settings toggle
- `ace-laptimes/backend/app.py`: table, 2 routes, allowlist entry, retention, `has_telemetry`
- `ace-laptimes/frontend/app.js` (+ `index.html` nav): analysis page
- Docs: `README.md` (API list, key allowlist), `ace-tray/TECHNICAL.md`, `docs/UPGRADING.md` (new table, env var)

## Verification
- CI floor: `compileall`, `ruff` (CI flags), `pip-audit --strict`, thumbnail check.
- Backend: script against a local Flask instance. Upload a valid blob; reject a wrong length, an unknown channel, an oversize count, a JWT-less key on GET, and another user's lap (403). Check visibility on GET from a non-group member (404/403). Check that retention prunes past N and keeps the PB.
- Frontend: Playwright (pre-installed) against the local stack with two synthetic laps. The delta chart ends at the lap-time difference, and the corner table finds the injected braking-point offset.
- Tray: synthetic shared-memory fixture (pack structs into bytes) for adapter and resampling unit checks. **Real-game validation must be done by hand on Windows** (phase 0 and before release).

## Phase 0 findings so far (desk research, not yet validated in-game)

- ACE's **graphics page is not AC1's**. It has no `completedLaps`, no
  `wchar_t` time strings, and no single `carCoordinates`. Instead it carries
  `npos`, `current_lap_time_ms`, `last_laptime_ms`, `total_lap_count`,
  `is_valid_lap`, and `car_coordinates[60][3]` indexed by a slot that has to
  be found by matching `player_car_id_*` against `car_ids[60]`. The plan's
  "buffer per `completedLaps`" step therefore becomes "buffer per lap,
  bounded by the `npos` wrap and confirmed by `last_laptime_ms` changing".
  The probe checks which of those lines up with the log.
- The physics page is 800 bytes. The first 416 bytes are AC1-compatible and
  hold every channel the plan needs (gas, brake, gear, rpms, steerAngle,
  speedKmh, accG).
- Upstream disagrees on whether `steerAngle` is radians or normalised −1..1.
  The probe records it next to `steering_percent` and `steer_degrees`.
- Use `OpenFileMappingW` read-only, not `mmap(tagname=...)`. The latter
  creates an empty mapping when the game isn't running and reads zeros.
- Layout source: Kunos' header as mirrored in `dSyncro/acevo-shared-memory`
  (`src/bindings/source/wrapper.hpp`, `#pragma pack(4)`). The canonical
  reference is Kunos' Steam guide "Shared Memory Documentation".

See [`ace-tray/tools/README.md`](../ace-tray/tools/README.md) for how to run
the probe and what to send back.
