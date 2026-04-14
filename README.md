# ACE Lap Tracker

A self-hosted lap time tracker for [Assetto Corsa Evo](https://store.steampowered.com/app/3058630/Assetto_Corsa_EVO/). Track, compare, and analyze lap times with your racing group.

Built for sim racers who want to own their data without relying on third-party services.

## Features

- **Automatic lap detection** — Windows tray app reads ACE log files in real time, no manual entry needed
- **Race + Practice support** — captures laps from both game modes with sector breakdowns
- **User accounts** — password auth with JWT tokens, multiple drivers on one server
- **Leaderboard** — compare best times across drivers, filtered by track and car
- **Personal bests** — track your fastest time per track/car combo
- **Progress charts** — visualize improvement over time for a specific track and car
- **Export** — download all data as CSV or JSON
- **Self-hosted** — runs on your own hardware via Docker, deploy through Portainer
- **Mobile-friendly** — responsive dark UI that works on phones and tablets

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Your homelab                         │
│                                                          │
│  ┌──────────┐     ┌──────────────┐     ┌──────────────┐ │
│  │  Nginx   │────▶│   Frontend   │     │   Backend    │ │
│  │  :8099   │     │   (static)   │     │  (Flask API) │ │
│  │          │────▶│              │     │  + SQLite    │ │
│  └──────────┘     └──────────────┘     └──────────────┘ │
│        │                                       ▲        │
│        └── /api/* ────────────────────────────┘        │
└─────────────────────────────────────────────────────────┘
         ▲
         │ HTTP POST /api/laptimes
         │
┌────────────────┐
│  Windows Tray  │──── reads ──── ACE log.txt
│  App (PyQt6)   │
└────────────────┘
```

Three Docker containers on a shared bridge network. The tray app runs on your gaming PC and auto-submits detected laps to the server.

## Quick start

### Server

**Requirements:** Docker and Docker Compose

1. Download [`ace-laptimes/docker-compose.yml`](ace-laptimes/docker-compose.yml)

2. Create a `.env` file next to it:
   ```env
   SECRET_KEY=your-random-secret-here
   ```
   Generate one: `openssl rand -hex 32`

3. Start the stack:
   ```bash
   docker compose up -d
   ```

4. Open `http://your-server-ip:8099` and create your account

The first registered user automatically becomes superadmin.

### Tray app (Windows)

1. Copy the `ace-tray` folder to your gaming PC

2. Run `start.bat` — it creates a virtual environment and launches the app

3. Go to **Settings**, enter your server URL and credentials, click **Connect**

4. The app watches `C:\Users\<you>\Saved Games\ACE\Logs\log.txt` by default

5. Race — laps are detected and submitted automatically

6. Optional: run `install_autostart.bat` to launch on Windows login

### Multi-user on one PC

The tray app stores credentials per Windows user account via `%APPDATA%`. Log into each Windows account, run `start.bat`, and connect with that user's credentials. Each account tracks independently.

## How lap detection works

The tray app parses the ACE game log in real time. It handles two formats:

**Race mode** — splits are logged per car with UUIDs. The app identifies your car and filters out AI opponents:
```
Split completed for car <your-uuid>: (37170 ms, splitindex 0) lap:1
```

**Practice mode** — splits are logged without car IDs (solo session):
```
On Split start 0 end 0 id 0 splittime 65505
```

Session metadata (track, car, weather) comes from the `Game Started!` log line. Sector times are summed for the total lap time. Partial laps and invalid times are filtered out.

## API

All endpoints except auth and health require `Authorization: Bearer <token>`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/auth/register` | Create account |
| POST | `/api/auth/login` | Sign in |
| GET | `/api/auth/me` | Current user |
| GET | `/api/laptimes` | List laps (filterable) |
| POST | `/api/laptimes` | Record a lap |
| PUT | `/api/laptimes/:id` | Update a lap |
| DELETE | `/api/laptimes/:id` | Delete a lap |
| GET | `/api/leaderboard` | Best times per driver/track/car |
| GET | `/api/personal-bests` | PBs per track/car combo |
| GET | `/api/progress` | Time series for charts |
| GET | `/api/meta/tracks` | All track names |
| GET | `/api/meta/cars` | All car names |
| GET | `/api/meta/users` | All users |
| GET | `/api/export/csv` | Download CSV |
| GET | `/api/export/json` | Download JSON |
| GET | `/api/health` | Health check |

## Tech stack

**Server:** Python, Flask, SQLite, Gunicorn, Nginx, Docker

**Tray app:** Python, PyQt6, requests

**Frontend:** Vanilla HTML/CSS/JS, Chart.js

## Roadmap

- [x] Docker backend with REST API
- [x] Web UI with leaderboard, PBs, progress charts, export
- [x] Windows tray app with ACE log parsing
- [x] Race + Practice mode support
- [x] Portainer-ready deployment
- [x] Superadmin role for platform management
- [x] Groups with group admin roles
- [x] User profiles and group profiles
- [x] Invite link system
- [x] Progress chart requires track + car selection
- [x] GitHub release
- [ ] Public site with MFA & group/team setup
- [ ] Track/car thumbnails
- [ ] Head-to-head delta tracking
- [ ] Session grouping
- [ ] Mobile PWA

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

[MIT](LICENSE) — do whatever you want with it.

## Acknowledgments

- [Kunos Simulazioni](https://www.kunos-simulazioni.com/) for Assetto Corsa Evo
- The sim racing community for log format discoveries
