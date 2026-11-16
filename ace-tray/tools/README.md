# ACE shared-memory probe

`shm_probe.py` is the phase 0 spike from [docs/TELEMETRY.md](../../docs/TELEMETRY.md).
It reads Assetto Corsa EVO's shared memory and writes what it sees to a CSV,
so the telemetry design can be checked against the real game before any of it
gets built. It is **not** part of the tray app or the `.exe`, needs nothing
beyond Python's standard library, never writes to the game's memory, and
sends nothing over the network.

## Run it

On the Windows PC with ACE installed, with any Python 3.10+:

```bat
python shm_probe.py --selftest
```

Expect every line to say `ok`. Then start ACE, get on track in a
practice / Time Attack session, and in a second window:

```bat
python shm_probe.py
```

Drive **4–5 laps**: at least one clean one, one where you cut a corner
(an invalid lap), and one where you drive into the pits. Keep the tray app
running at the same time, so its activity log shows the laps it detected from
`log.txt`. Stop the probe with `Ctrl+C`.

## What to send back

- `ace_probe_<timestamp>.summary.txt`
- `ace_probe_<timestamp>.csv` (a few MB at 50 Hz)
- the lap times the tray logged for the same laps (screenshot or copy of its
  activity log)

## What it answers

| Question | Where to look |
|---|---|
| Does the struct layout match this ACE build? | `sm_version` / `ac_evo_version`, plus whether `speedKmh`, `gas`, `brake`, `npos` look sane in the CSV |
| How fast do the pages update? | `physics page rate` / `graphics page rate` in the summary |
| Do shared-memory lap boundaries match the log? | `lap event` lines vs. the tray's lap times |
| Is an invalid lap flagged? | `is_valid_lap` / `timing_is_invalid` columns around the cut |
| Which car slot is the player's? | `player_slot` column (−1 means the id lookup failed; `slot0_*` is the fallback) |
| Is `steerAngle` radians or −1..1? | compare `steerAngle`, `steering_percent`, `steer_degrees` |

If it prints `... not found — is ACE running and on track?`, the game hasn't
created its shared memory yet. Load into a session first.
