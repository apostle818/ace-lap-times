# ACE Tray — Windows Companion App

> **Version 1.4.0** — [Technical Documentation](TECHNICAL.md)

A Windows system tray app that watches Assetto Corsa Evo for completed laps and automatically submits them to your [ACE Lap Tracker](../README.md) server. No manual entry needed during a session.

---

## What It Does

- **Runs in the background** — sits in the system tray while you race, using minimal resources
- **Auto-detects laps** — monitors ACE's log file and captures lap time, track, car, and sector splits the moment a lap completes
- **Auto-submits to server** — sends each lap to your ACE Lap Tracker backend automatically
- **Manual entry** — includes a form if you want to log a lap by hand
- **Desktop notifications** — shows a toast notification whenever a lap is recorded
- **Per-user configuration** — settings and credentials are saved per Windows account, so each driver on a shared PC has their own setup
- **Driver switching** — hand the rig to a mate and file their laps under their own name, without signing out or swapping Windows accounts
- **Queues laps when offline** — if the server is unreachable, laps are held and submitted once the connection is restored

---

## Requirements

- **Windows 10 or 11**
- **Python 3.10 or newer** — [download from python.org](https://www.python.org/downloads/)
  - During installation, check **"Add Python to PATH"**
  - Not needed if you use the prebuilt `ACELapTracker.exe` from the
    [latest release](../../../releases/latest) — see step 1 below
- **Assetto Corsa Evo** installed and run at least once (so the log file exists)
- **ACE Lap Tracker server** reachable on your network — see the [server setup guide](../README.md#server)

---

## Setup

### 1. Install

**Easiest:** download `ACELapTracker.exe` from the
[latest release](../../../releases/latest) and run it — no Python, no virtual
environment, nothing to build. Then skip to step 3.

**From source:** copy the `ace-tray` folder to your gaming PC. Any location
works, for example:

```
C:\Tools\ace-tray\
```

### 2. First Launch

Double-click `start.bat`. It will:
1. Create a Python virtual environment inside the folder
2. Install the required packages (PyQt6 and requests)
3. Launch the tray app

The first launch takes a minute while packages are installed. Subsequent launches are instant.

### 3. Configure

When the app opens for the first time:

1. On the website, go to **My Profile → API Keys**, create a key, and copy it
   (it is shown once and cannot be recovered afterwards)
2. In the app, go to the **Settings** tab
3. Enter your **server URL** — e.g. `http://your-server-ip:8099`
4. Paste the **API key**
5. Click **Connect** — the status indicator should turn green
6. Verify the **log file path** points to your ACE `Logs` folder (the app fills
   in the default path automatically)

No password is involved, and none is stored. The key uploads laps and nothing
else, and you can revoke it from the website without changing your password.

> **Prefer not to visit the website?** Expand *"Or sign in with username &
> password"* and connect with your account instead. The app creates a key for
> itself and stores only that — your password is never written to disk.

If other people use this PC with their own Windows accounts, they should repeat
this step while logged in as themselves, with their own key. Settings live in
the registry under `HKCU`, so each Windows account is independent.

### 4. Auto-start (Optional)

Run `install_autostart.bat` to add the app to Windows startup. It will launch in the tray automatically every time you log in — no need to remember to start it before racing.

To remove auto-start, open the **Task Scheduler** or the **Startup folder** (`shell:startup`) and delete the entry.

---

## Using the App

### During a Race Session

You don't need to do anything. Once configured, the app runs in the background and submits laps automatically. You'll see a desktop notification each time a lap is recorded.

If a lap isn't detected (e.g. after a game update that changes the log format), use **Manual Entry** as a fallback.

### Someone else takes the seat

Right-click the tray icon → **Driving as**, and pick them. Every lap from then
on — detected or entered by hand — is filed under their name, on their
leaderboard entry and their personal bests. The same picker sits at the top of
the **Dashboard** tab if the window is already open, and the tray tooltip says
who is driving whenever it is not you. Pick yourself again when you get the
rig back.

The choice survives a restart, so check it after a break rather than assuming
you are back on your own name.

**Do not see the menu?** It only appears when the server has someone to switch
to — you must be a **group admin** of a group they belong to. Ask whoever runs
the server to make you one, and note the switch reaches your groups only: it is
not a way to record laps for the whole instance. Nothing changes about your API
key; it stays the one issued to your own account.

### App Window

To open the window: **double-click the tray icon**.
To close to tray without quitting: click the **X** button.
To fully quit: **right-click the tray icon** → **Quit**.

### Tabs

| Tab | Description |
|-----|-------------|
| **Dashboard** | Who laps are filed under, last detected lap, current session info (track, car, weather), and recent laps fetched from the server |
| **Manual Entry** | Form to manually log a lap time — useful as a fallback or for logging laps from another session |
| **Settings** | Server URL, API key, log file path, and auto-submit toggle |
| **Activity Log** | Live feed of what the app is doing — useful for diagnosing issues |

---

## Troubleshooting

**"File not found" for log path**
→ ACE must be launched at least once for the log file to be created. Open the game, wait for the main menu, then close it.

**Laps aren't being submitted**
→ Check the **Activity Log** tab. If the watcher is running but laps aren't detected, the log format may have changed after a game update. See [TECHNICAL.md](TECHNICAL.md) for details on the log parser.

**Can't connect to server**
→ Verify the server URL in Settings. Make sure the Docker stack is running. Try opening `http://your-server-ip:8099` in a browser — if that works, the problem is in the tray app config.

**Wrong account, or a revoked key**
→ Go to Settings, paste a new API key, and click Connect again. *"Invalid,
revoked or expired API key"* means the key no longer works — create a fresh
one on the website. Connecting with a key for a different account also resets
the **Driving as** choice back to that account.

**App doesn't appear in the tray**
→ Check if it's hidden in the overflow tray area (the `^` arrow in the taskbar). If the app crashed, re-run `start.bat` and check the Activity Log for errors.
