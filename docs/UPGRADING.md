# Upgrading

This release changes how the tray app authenticates: lap upload now uses a
revocable **API key** instead of a stored login token. Both the server and the
tray app need updating, and **the order matters**.

> **Upgrade the server first, then the tray app.**
> An old tray app works fine against the new server. A new tray app cannot talk
> to an old server, because the old server has no API key support.

Nothing about your data changes. Lap times, users, groups and invites are
untouched, and no configuration or environment variables change.

---

## Compatibility

| Server | Tray app | Works? |
|---|---|---|
| New | New | Yes — the intended combination |
| New | Old | Yes — old tray keeps using its saved login token until you update it |
| Old | New | **No** — tray shows *"Server too old — it has no API key support yet"* |
| Old | Old | Yes — unchanged, but you get none of the security benefit |

---

## Part 1 — Server

### Standard Docker Compose

```bash
cd /path/to/ace-laptimes
docker compose pull
docker compose up -d
```

That's it. On startup the backend creates the new `api_keys` table itself
(`CREATE TABLE IF NOT EXISTS`), so there is no migration script to run and no
downtime beyond the container restart.

### Portainer

Stacks → your stack → **Pull and redeploy** (make sure *Re-pull image* is
enabled). Or Images → pull `apostle818/ace-laptimes-backend:latest`,
`-frontend:latest` and `-nginx:latest`, then restart the stack.

### Verify it worked

```bash
docker compose logs backend --tail 20      # should start cleanly, no errors
curl http://your-server-ip:8099/api/health # should return ok
```

Then open the web UI, go to **My Profile**, and confirm an **API Keys** card
appears below Groups. If it does, the upgrade is complete.

### What changed on the server

- New `api_keys` table (keys stored as SHA-256 hashes, never plaintext).
- New endpoints: `GET/POST /api/keys`, `DELETE /api/keys/:id`, and
  `GET /api/admin/keys`, `DELETE /api/admin/keys/:id` for superadmins.
- The seven endpoints the tray app uses now also accept an `X-API-Key` header.
  Every other endpoint rejects API keys with `403`.
- Web UI login is unchanged — still username/password, still a JWT.

### Rolling back the server

```bash
docker compose down
# edit docker-compose.yml to pin the previous version, e.g.
#   image: apostle818/ace-laptimes-backend:v1.1.0
docker compose up -d
```

Your database stays compatible: the old backend simply ignores the `api_keys`
table. But **any tray app already switched to an API key will stop uploading**
until you roll it back too, or reconnect it with a username and password.

---

## Part 2 — Tray app

### If you upgraded from an earlier version

**You almost certainly need to do nothing.**

On first launch the new tray app finds your saved login token, exchanges it for
an API key, stores the key, and deletes the token. You'll see this in the
Activity Log:

```
Upgraded saved login to an API key - your password is no longer needed
```

From then on it uploads with the key. Your password is never written to disk.

**If the automatic upgrade fails** — most often because the saved token has
already expired (they last 30 days) — the log shows:

```
Could not upgrade saved login to an API key: ...
```

and the app shows *Not connected*. Fix it in either of these ways:

1. **Sign in once.** Settings → expand *"Or sign in with username & password"* →
   enter your credentials → **Connect**. The app mints a key for itself and
   stores only the key.
2. **Paste a key.** On the website go to **My Profile → API Keys**, create a key,
   copy it, and paste it into the **API Key** field in Settings → **Connect**.

### Updating the app itself

**Option A — standalone executable (new, no Python needed)**

Download `ACELapTracker.exe` from the
[latest release](../../releases/latest) and run it. If you previously used
`start.bat`, close the old app first — both read the same saved settings, so
your connection carries over.

**Option B — from source**

```
cd ace-tray
git pull            # or re-copy the ace-tray folder
start.bat
```

`start.bat` reuses the existing `.venv`. No new dependencies were added, so
there is nothing extra to install.

### For a fresh install

1. Open the website and log in.
2. **My Profile → API Keys** → give the key a name (e.g. *Tray on GAMING-PC*) →
   **Create Key**.
3. Copy the key immediately — it is shown **once** and cannot be retrieved
   later. If you lose it, revoke it and create another.
4. In the tray app: Settings → enter the Server URL → paste the key →
   **Connect**.

### What changed in the tray app

- Settings now has an **API Key** field instead of Username/Password. The
  username/password fields still exist behind a toggle, as a convenience that
  provisions a key for you.
- Only the API key is saved. The password is never written to disk, and the
  old login token is deleted once migrated.
- Version bumped to 1.2.0.

### Rolling back the tray app

Reinstall the previous version and reconnect with your username and password.
The old version cannot use an API key, so it will re-create a login token.
You can revoke the now-unused API key on the website afterwards.

---

## After upgrading

Worth doing once everything is working:

- **Check your keys.** My Profile → API Keys shows each key's *Last used*
  column. Anything that never gets used is a key you can revoke.
- **Revoke spares.** If you signed in on several PCs, each one made its own key,
  named after that machine's hostname. Revoke any you don't recognise.
- **Superadmins** can review every key on the server via `GET /api/admin/keys`.

Revoking a key only stops that one tray app. It never affects your password or
your web login.

---

## Troubleshooting

**Tray says "Invalid, revoked or expired API key"**
The key was revoked, expired, or mistyped. Create a new one on the website and
paste it in. Note the field is masked — paste rather than retype.

**Tray says "Server too old — it has no API key support yet"**
The server hasn't been upgraded. Do Part 1 first.

**Tray says "Cannot reach server"**
Not an auth problem. Check the Server URL, that the stack is running, and that
port 8099 is reachable from the gaming PC.

**Laps stopped uploading after the upgrade**
Open Settings and check the connection status line. If it says *Not connected*,
follow "If the automatic upgrade fails" above. Laps detected while
disconnected are queued and submitted once you reconnect — but that queue is
held in memory only, so reconnect before closing the app or those laps are
lost.

**I want to start completely fresh on a PC**
Close the tray app and delete the registry key
`HKEY_CURRENT_USER\Software\ACELaps\ACE Lap Tracker`
(Win+R → `regedit`). Relaunch and reconnect. This clears the saved server URL,
key and log path for that Windows user only.

**Multiple Windows users on one PC**
Settings are per Windows user, so each account needs its own key. That is
intentional — it keeps each driver's laps attributed correctly.
