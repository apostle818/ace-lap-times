# Contributing to ACE Lap Tracker

Thanks for your interest in contributing! This project is built by sim racers, for sim racers. Whether you're fixing a bug, adding a feature, or improving docs — your help is welcome.

## Ways to contribute

### Report a bug

Open an issue with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Your ACE version (shown in the game's log.txt first line)
- Your setup: server (Docker/Portainer) or tray app (Windows version)

If the tray app isn't detecting laps, include the relevant section of your ACE `log.txt` and the tray app's Activity Log output.

### Suggest a feature

Open an issue with the "feature" label. Describe the problem you're solving, not just the solution you want. A good feature request explains *why* before *what*.

### Submit code

1. Fork the repo
2. Create a branch from `main` (`git checkout -b feature/your-thing`)
3. Make your changes
4. Test locally (see below)
5. Commit with a clear message
6. Open a pull request against `main`

Keep PRs focused — one feature or fix per PR. Large PRs are harder to review and slower to merge.

## Development setup

### Server (backend + frontend)

```bash
# Clone the repo
git clone <repo-url>
cd ace-lap-times

# Backend
cd ace-laptimes/backend
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
python app.py  # runs on http://localhost:5000

# Frontend
cd ../frontend
# Just open index.html in a browser, or use any static file server
npx serve -s . -l 3000
```

### Tray app

```bash
cd ace-tray
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
python ace_tray.py
```

### Docker (full stack)

```bash
docker compose up --build
# App available at http://localhost:8099
```

## Code style

### Python (backend + tray app)

- Python 3.10+
- Use type hints where it helps readability
- Keep functions short and focused
- No linting tool is enforced yet — just be consistent with the existing code

### Frontend

- Vanilla JS, no framework
- Keep it in a single HTML file (it's intentionally simple)
- CSS variables for all colors and spacing
- Mobile-first: test on narrow screens

### General

- No unnecessary dependencies — every new package needs a good reason
- Comment *why*, not *what* — the code should explain itself
- Keep commit messages short and descriptive: `Fix practice lap detection for 4-sector tracks`

## ACE log format

The game is in Early Access and Kunos may change the log format at any time. If you're working on the log parser, here's what we know:

**Session start** (one line, both race and practice):
```
Game Started! GameModeType_INSTANT_RACE | Track Name Race Race  4 laps @... | car_id | GameModeSelectionWeatherType_CLEAR | ...
Game Started! GameModeType_PRACTICE | Track Name Time Attack Practice  600 seconds @... | car_id | GameModeSelectionWeatherType_CLEAR | ...
```

**Player car identification:**
```
onSetPlayerCurrentCarCommand: Set new car <uuid-with-dashes> content\cars\<car_id>\presets\...
```

**Race splits** (includes car UUID, need to filter for player):
```
Split completed for car <uuid-no-dashes>: (<time_ms> ms, splitindex <N>) lap:<L>
```

**Practice splits** (no car UUID, always the player):
```
On Split start X end X id <splitindex> splittime <time_ms>
```

**Lap completion marker:**
```
Lap test evOnLapCompleted <N> completed
```

Log location: `C:\Users\<user>\Saved Games\ACE\Logs\log.txt`

If you discover new log patterns after an ACE update, please open an issue or PR with a sample log snippet.

## Project structure

```
ace-lap-times/
├── ace-laptimes/          # Server (Docker stack)
│   ├── backend/           # Flask API + SQLite
│   │   ├── app.py
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   ├── frontend/          # Static web UI
│   │   ├── index.html
│   │   └── Dockerfile
│   ├── nginx/             # Reverse proxy
│   │   ├── nginx.conf
│   │   └── Dockerfile
│   └── build-and-push.sh  # One-time Docker Hub build script
├── ace-tray/              # Windows tray app
│   ├── ace_tray.py
│   ├── requirements.txt
│   ├── start.bat
│   └── install_autostart.bat
├── README.md
├── LICENSE
└── CONTRIBUTING.md
```

## Questions?

Open an issue. There's no Discord or forum yet — GitHub issues is the place for everything.
