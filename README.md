# ACE Lap Tracker

A self-hosted lap time tracker for [Assetto Corsa Evo](https://store.steampowered.com/app/3058630/Assetto_Corsa_EVO/). Track, compare, and analyze lap times with your racing group.

Built for sim racers who want to own their data without relying on third-party services.

## Features

- **Automatic lap detection** — Windows tray app reads ACE log files in real time, no manual entry needed
- **Race + Practice support** — captures laps from both game modes with sector breakdowns
- **User accounts** — password auth with JWT tokens, multiple drivers on one server
- **API keys** — the tray app uploads with a revocable, upload-only key instead of your password
- **Groups** — a privacy boundary, not just a label: you see your own laps and those of drivers you share a group with, nobody else's
- **Driver switching** — share a rig: pick who is in the seat from the tray, or move a mis-filed lap to the right driver on the website
- **Leaderboard** — compare best times across drivers in your groups, filtered by track and car
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

> Already running an older version? See [docs/UPGRADING.md](docs/UPGRADING.md) —
> upgrade the server before the tray app.

**Requirements:** Docker and Docker Compose

Images are published for `linux/amd64` and `linux/arm64`, so a
Raspberry Pi 3 or newer on a 64-bit OS works out of the box. 32-bit ARM
(ARMv7/armhf) and ARMv6 (Pi Zero / Pi Zero W / Pi 1) are **not supported** —
see [docs/BUILDS.md](docs/BUILDS.md).

1. Download [`ace-laptimes/docker-compose.yml`](ace-laptimes/docker-compose.yml)

2. Create a `.env` file next to it, with a secret you generate yourself:
   ```bash
   echo "SECRET_KEY=$(openssl rand -hex 32)" > .env
   ```
   This key signs every login session. There is no default and no fallback —
   the stack refuses to start without it, on purpose. Anyone who knows your
   key can sign in as any user, including the superadmin, so treat it like a
   password and never commit it.

3. Start the stack:
   ```bash
   docker compose up -d
   ```

4. Open `http://your-server-ip:8099` and create your account

The first registered user automatically becomes superadmin.

> **Keep port 8099 off the internet.** The stack speaks plain HTTP, so
> passwords, session tokens and API keys cross the network unencrypted. That
> is fine on a home LAN and not fine when port-forwarded. To reach it from
> outside, use a VPN or put a TLS reverse proxy in front — see
> [docs/TLS.md](docs/TLS.md).

### Tray app (Windows)

**Easiest:** download `ACELapTracker.exe` from the
[latest release](../../releases/latest) and run it — no Python needed. Then skip to step 3.

**From source:**

1. Copy the `ace-tray` folder to your gaming PC

2. Run `start.bat` — it creates a virtual environment and launches the app

3. On the website, go to **My Profile → API Keys**, create a key, and copy it

4. In the tray app go to **Settings**, enter your server URL, paste the API key, click **Connect**

   (Or expand *"Or sign in with username & password"* — the app then creates a key for
   itself and stores only that. Your password is never written to disk.)

5. The app watches `C:\Users\<you>\Saved Games\ACE\Logs\log.txt` by default

6. Race — laps are detected and submitted automatically

7. Optional: run `install_autostart.bat` to launch on Windows login

Upgrading from an older tray version? It swaps your saved login for an API key
automatically on first launch — nothing to do.

### Multi-user on one PC

The tray app stores its API key per Windows user account (under `HKCU\Software\ACELaps` in the registry). Log into each Windows account, run the app, and connect with that user's own key. Each account tracks independently.

## How lap detection works

The tray app parses the ACE game log in real time. It handles two formats:

**Race mode** — splits are logged per car with UUIDs. The app identifies your car and filters out AI opponents:
```
Split completed for car <your-uuid>: (37170 ms, splitindex 0) lap:1
```

**Practice mode** (including Time Attack) — splits are logged without car IDs (solo session):
```
On Split start false end false id 0 splittime 65505
```
The `start`/`end` fields were integers on ACE 0.5/0.6 and became booleans on ACE 0.7; the parser handles both.

Session metadata (track, car, weather) comes from the `Game Started!` log line. Sector times are summed for the total lap time. Partial laps and invalid times are filtered out.

## API

All endpoints except auth and health require `Authorization: Bearer <token>`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/auth/register` | Create account |
| POST | `/api/auth/login` | Sign in |
| GET | `/api/auth/me` | Current user |
| GET | `/api/laptimes` | List laps you can see (filterable) |
| POST | `/api/laptimes` | Record a lap |
| PUT | `/api/laptimes/:id` | Update a lap, or move it to another driver |
| DELETE | `/api/laptimes/:id` | Delete a lap |
| GET | `/api/leaderboard` | Best times per driver/track/car, within your groups |
| GET | `/api/personal-bests` | PBs per track/car combo |
| GET | `/api/progress` | Time series for charts |
| GET | `/api/meta/tracks` | All track names |
| GET | `/api/meta/cars` | All car names |
| GET | `/api/meta/users` | Driver directory |
| GET | `/api/meta/assignable-users` | Drivers you may file a lap under |
| GET | `/api/export/csv` | Download CSV |
| GET | `/api/export/json` | Download JSON |
| GET | `/api/keys` | List your API keys |
| POST | `/api/keys` | Create an API key (returned once) |
| DELETE | `/api/keys/:id` | Revoke an API key |
| DELETE | `/api/auth/sessions` | Sign out everywhere; `{"revoke_api_keys": true}` also revokes your keys |
| GET | `/api/health` | Health check |

### Authentication

Two credentials types, deliberately not interchangeable:

- **JWT** (`Authorization: Bearer <token>`) — issued by `/api/auth/login`, used by the web UI. Full access for the account's role.
- **API key** (`X-API-Key: alt_...`) — used by the tray app. Accepted on **only** these endpoints:
  `GET /api/auth/me`, `GET|POST /api/laptimes`, `GET /api/meta/tracks`, `GET /api/meta/cars`,
  `GET /api/meta/assignable-users`, `POST /api/client/heartbeat`, `POST /api/client/disconnect`.

Everything else — admin routes, profile changes, exports, lap deletion, lap editing, key
management — rejects API keys with `403`, so a new endpoint is closed to keys unless
explicitly opened. A key also carries no role: even a superadmin's key acts as a plain
member, so it can never reach an admin route or record for anyone outside its owner's
groups.

Keys are stored as a SHA-256 hash; the plaintext is shown once at creation and is
unrecoverable afterwards. Revoking a key does not affect your password or web login.

### Who can see what

Groups decide visibility. A driver sees their own laps plus the laps of
everyone they share at least one group with — on the leaderboard, in history,
in progress charts, in the track and car lists, and in exports. Laps recorded
by someone with no group in common are not visible and cannot be reached by
passing their `user_id` to the API.

| | Sees |
|---|---|
| Superadmin | Everything on the instance |
| Group admin | Their own laps and their groups' members' laps; only co-members in the directory |
| Member | Their own laps and their groups' members' laps; only co-members in the directory |
| Member with no group | Only their own laps |

A driver with no group sees only themselves, so put everyone in a group if
you want a shared leaderboard.

Only a superadmin can place someone into a group directly. A group admin
invites instead — *Groups → pick a group → Invite Links* — and the person
joins by opening the link. That is deliberate: a group is what the visibility
and lap-attribution rules below are built on, so being added to one has to be
something you agreed to, not something done to you.

### Who a lap can be filed under

Reading a lap and writing one in someone else's name are separate questions.
Sharing a group lets you *see* a driver's laps; it does not let you *file*
laps as them.

| | May record for, edit, move or delete the laps of |
|---|---|
| Superadmin | Anyone on the instance |
| Group admin | Themselves and every member of the groups they administer |
| Member | Only themselves |

This is what both driver pickers are built on — *Record For* and the driver
column on Lap History in the web UI, and *Driving as* in the tray app. A plain
member sees no picker at all, because there is nobody else they could pick.

An API key follows the same rule minus the superadmin row: a superadmin's tray
key can file for the members of the groups that account group-admins, and no
further. Whichever driver a lap ends up filed under, it is theirs — it counts
towards their leaderboard entry and personal bests, not the uploader's.

## Tech stack

**Server:** Python, Flask, SQLite, Gunicorn, Nginx, Docker

**CI/CD:** GitHub Actions — multi-arch images to Docker Hub, PyInstaller Windows build

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
- [x] API key auth for the tray app
- [x] Multi-arch image builds via GitHub Actions (amd64 / arm64)
- [x] Windows .exe built in CI
- [x] Reassign a lap to another driver, on the web and from the tray
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
