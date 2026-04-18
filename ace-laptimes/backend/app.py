import os
import json
import sqlite3
import csv
import io
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify, g, Response
import bcrypt
import jwt

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key")
DATABASE_PATH = os.environ.get("DATABASE_PATH", "./data/laptimes.db")

# ─── Database ────────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = sqlite3.connect(DATABASE_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            bio TEXT DEFAULT '',
            role TEXT NOT NULL DEFAULT 'member',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT DEFAULT '',
            created_by INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (created_by) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS group_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'member',
            joined_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(group_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS group_invites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            created_by INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
            FOREIGN KEY (created_by) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS laptimes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            track TEXT NOT NULL,
            car TEXT NOT NULL,
            laptime_ms INTEGER NOT NULL,
            weather TEXT DEFAULT 'Clear',
            notes TEXT DEFAULT '',
            recorded_at TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_laptimes_user ON laptimes(user_id);
        CREATE INDEX IF NOT EXISTS idx_laptimes_track_car ON laptimes(track, car);
    """)
    # Migrations
    user_cols = [r[1] for r in db.execute("PRAGMA table_info(users)").fetchall()]
    if 'role' not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")
        db.commit()
    if 'bio' not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN bio TEXT DEFAULT ''")
        db.commit()
    group_cols = [r[1] for r in db.execute("PRAGMA table_info(groups)").fetchall()]
    if 'description' not in group_cols:
        db.execute("ALTER TABLE groups ADD COLUMN description TEXT DEFAULT ''")
        db.commit()
    db.close()

init_db()

# ─── Auth helpers ────────────────────────────────────────────────────

def create_token(user_id, username, role):
    payload = {
        "user_id": user_id,
        "username": username,
        "role": role,
        "exp": datetime.utcnow() + timedelta(days=30),
    }
    return jwt.encode(payload, app.config["SECRET_KEY"], algorithm="HS256")

def _parse_token():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"error": "Token required"}), 401
    token = auth_header[7:]
    try:
        data = jwt.decode(token, app.config["SECRET_KEY"], algorithms=["HS256"])
        g.current_user_id = data["user_id"]
        g.current_username = data["username"]
        g.current_user_role = data.get("role", "member")
        return None
    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Token expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid token"}), 401

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        err = _parse_token()
        if err:
            return err
        return f(*args, **kwargs)
    return decorated

def superadmin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        err = _parse_token()
        if err:
            return err
        if g.current_user_role != "superadmin":
            return jsonify({"error": "Superadmin access required"}), 403
        return f(*args, **kwargs)
    return decorated

# ─── Auth routes ─────────────────────────────────────────────────────

@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.get_json()
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")
    display_name = data.get("display_name", "").strip()

    if not username or not password or not display_name:
        return jsonify({"error": "All fields required"}), 400
    if len(password) < 4:
        return jsonify({"error": "Password must be at least 4 characters"}), 400

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    db = get_db()

    count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    role = "superadmin" if count == 0 else "member"

    try:
        cursor = db.execute(
            "INSERT INTO users (username, password_hash, display_name, role) VALUES (?, ?, ?, ?)",
            (username, password_hash, display_name, role),
        )
        db.commit()
        user_id = cursor.lastrowid
        token = create_token(user_id, username, role)
        return jsonify({
            "token": token,
            "user": {"id": user_id, "username": username, "display_name": display_name, "role": role, "groups": []}
        }), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "Username already taken"}), 409

@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json()
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not user or not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        return jsonify({"error": "Invalid credentials"}), 401

    groups = db.execute("""
        SELECT g.id, g.name, gm.role as group_role
        FROM groups g JOIN group_members gm ON g.id = gm.group_id
        WHERE gm.user_id = ?
    """, (user["id"],)).fetchall()

    token = create_token(user["id"], user["username"], user["role"])
    return jsonify({
        "token": token,
        "user": {
            "id": user["id"], "username": user["username"],
            "display_name": user["display_name"], "role": user["role"],
            "bio": user["bio"] or "",
            "groups": [{"id": gr["id"], "name": gr["name"], "group_role": gr["group_role"]} for gr in groups]
        },
    })

@app.route("/api/auth/me", methods=["GET"])
@token_required
def me():
    db = get_db()
    user = db.execute("SELECT id, username, display_name, bio, role FROM users WHERE id = ?", (g.current_user_id,)).fetchone()
    if not user:
        return jsonify({"error": "User not found"}), 404
    groups = db.execute("""
        SELECT g.id, g.name, gm.role as group_role
        FROM groups g JOIN group_members gm ON g.id = gm.group_id
        WHERE gm.user_id = ?
    """, (g.current_user_id,)).fetchall()
    return jsonify({
        "id": user["id"], "username": user["username"],
        "display_name": user["display_name"], "role": user["role"],
        "bio": user["bio"] or "",
        "groups": [{"id": gr["id"], "name": gr["name"], "group_role": gr["group_role"]} for gr in groups]
    })

@app.route("/api/auth/profile", methods=["PUT"])
@token_required
def update_profile():
    data = request.get_json()
    display_name = data.get("display_name", "").strip()
    bio = data.get("bio", "").strip()
    if not display_name:
        return jsonify({"error": "Display name required"}), 400
    db = get_db()
    db.execute("UPDATE users SET display_name = ?, bio = ? WHERE id = ?",
               (display_name, bio, g.current_user_id))
    db.commit()
    return jsonify({"message": "Profile updated"})

# ─── User profile ─────────────────────────────────────────────────────

@app.route("/api/users/<int:user_id>", methods=["GET"])
@token_required
def get_user_profile(user_id):
    db = get_db()
    user = db.execute(
        "SELECT id, username, display_name, bio, role, created_at FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()
    if not user:
        return jsonify({"error": "User not found"}), 404
    stats = db.execute(
        "SELECT COUNT(*) as total_laps, COUNT(DISTINCT track || '|' || car) as combos FROM laptimes WHERE user_id = ?",
        (user_id,)
    ).fetchone()
    groups = db.execute("""
        SELECT g.id, g.name, gm.role as group_role
        FROM groups g JOIN group_members gm ON g.id = gm.group_id
        WHERE gm.user_id = ?
    """, (user_id,)).fetchall()
    return jsonify({
        "id": user["id"], "username": user["username"],
        "display_name": user["display_name"], "bio": user["bio"] or "",
        "role": user["role"], "created_at": user["created_at"],
        "stats": {"total_laps": stats["total_laps"], "combos": stats["combos"]},
        "groups": [{"id": gr["id"], "name": gr["name"], "group_role": gr["group_role"]} for gr in groups]
    })

# ─── Admin routes ────────────────────────────────────────────────────

@app.route("/api/admin/users", methods=["GET"])
@superadmin_required
def admin_list_users():
    db = get_db()
    rows = db.execute("SELECT id, username, display_name, role, created_at FROM users ORDER BY created_at").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/admin/users/<int:user_id>", methods=["PUT"])
@superadmin_required
def admin_update_user(user_id):
    if user_id == g.current_user_id:
        return jsonify({"error": "Cannot modify your own role"}), 400
    data = request.get_json()
    role = data.get("role")
    if role not in ("member", "superadmin"):
        return jsonify({"error": "Invalid role"}), 400
    db = get_db()
    db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    db.commit()
    return jsonify({"message": "Updated"})

@app.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
@superadmin_required
def admin_delete_user(user_id):
    if user_id == g.current_user_id:
        return jsonify({"error": "Cannot delete yourself"}), 400
    db = get_db()
    db.execute("DELETE FROM group_members WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM laptimes WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    return jsonify({"message": "Deleted"})

# ─── Group routes ────────────────────────────────────────────────────

@app.route("/api/groups", methods=["GET"])
@token_required
def list_groups():
    db = get_db()
    if g.current_user_role == "superadmin":
        rows = db.execute("""
            SELECT g.id, g.name, g.description, g.created_at, u.display_name as created_by_name,
                   COUNT(gm.user_id) as member_count
            FROM groups g
            JOIN users u ON g.created_by = u.id
            LEFT JOIN group_members gm ON g.id = gm.group_id
            GROUP BY g.id ORDER BY g.name
        """).fetchall()
    else:
        rows = db.execute("""
            SELECT g.id, g.name, g.description, g.created_at, u.display_name as created_by_name,
                   COUNT(gm2.user_id) as member_count, my_gm.role as my_group_role
            FROM groups g
            JOIN group_members my_gm ON g.id = my_gm.group_id AND my_gm.user_id = ?
            JOIN users u ON g.created_by = u.id
            LEFT JOIN group_members gm2 ON g.id = gm2.group_id
            GROUP BY g.id ORDER BY g.name
        """, (g.current_user_id,)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/groups", methods=["POST"])
@superadmin_required
def create_group():
    data = request.get_json()
    name = data.get("name", "").strip()
    description = data.get("description", "").strip()
    if not name:
        return jsonify({"error": "Group name required"}), 400
    db = get_db()
    try:
        cursor = db.execute(
            "INSERT INTO groups (name, description, created_by) VALUES (?, ?, ?)",
            (name, description, g.current_user_id)
        )
        group_id = cursor.lastrowid
        # Auto-add creator as group_admin
        db.execute(
            "INSERT INTO group_members (group_id, user_id, role) VALUES (?, ?, 'group_admin')",
            (group_id, g.current_user_id)
        )
        db.commit()
        return jsonify({"id": group_id, "name": name}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "Group name already taken"}), 409

@app.route("/api/groups/<int:group_id>", methods=["GET"])
@token_required
def get_group(group_id):
    db = get_db()
    if g.current_user_role != "superadmin":
        member = db.execute(
            "SELECT * FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, g.current_user_id)
        ).fetchone()
        if not member:
            return jsonify({"error": "Group not found"}), 404
    group = db.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    if not group:
        return jsonify({"error": "Group not found"}), 404
    members = db.execute("""
        SELECT u.id, u.username, u.display_name, u.role as app_role, gm.role as group_role, gm.joined_at
        FROM group_members gm JOIN users u ON gm.user_id = u.id
        WHERE gm.group_id = ?
        ORDER BY CASE gm.role WHEN 'group_admin' THEN 0 ELSE 1 END, u.display_name
    """, (group_id,)).fetchall()
    return jsonify({
        "id": group["id"], "name": group["name"],
        "description": group["description"] or "",
        "created_at": group["created_at"],
        "members": [dict(m) for m in members]
    })

@app.route("/api/groups/<int:group_id>", methods=["PUT"])
@token_required
def update_group(group_id):
    db = get_db()
    is_superadmin = g.current_user_role == "superadmin"
    if not is_superadmin:
        my_m = db.execute(
            "SELECT role FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, g.current_user_id)
        ).fetchone()
        if not my_m or my_m["role"] != "group_admin":
            return jsonify({"error": "Permission denied"}), 403
    data = request.get_json()
    description = data.get("description", "").strip()
    if is_superadmin:
        name = data.get("name", "").strip()
        if not name:
            return jsonify({"error": "Group name required"}), 400
        try:
            db.execute("UPDATE groups SET name = ?, description = ? WHERE id = ?",
                       (name, description, group_id))
            db.commit()
            return jsonify({"message": "Updated"})
        except sqlite3.IntegrityError:
            return jsonify({"error": "Group name already taken"}), 409
    else:
        db.execute("UPDATE groups SET description = ? WHERE id = ?", (description, group_id))
        db.commit()
        return jsonify({"message": "Updated"})

@app.route("/api/groups/<int:group_id>", methods=["DELETE"])
@superadmin_required
def delete_group(group_id):
    db = get_db()
    db.execute("DELETE FROM groups WHERE id = ?", (group_id,))
    db.commit()
    return jsonify({"message": "Deleted"})

@app.route("/api/groups/<int:group_id>/members", methods=["POST"])
@token_required
def add_group_member(group_id):
    db = get_db()
    if g.current_user_role != "superadmin":
        my_m = db.execute(
            "SELECT role FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, g.current_user_id)
        ).fetchone()
        if not my_m or my_m["role"] != "group_admin":
            return jsonify({"error": "Permission denied"}), 403
    data = request.get_json()
    user_id = data.get("user_id")
    role = data.get("role", "member")
    if role not in ("member", "group_admin"):
        return jsonify({"error": "Invalid role"}), 400
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    try:
        db.execute(
            "INSERT INTO group_members (group_id, user_id, role) VALUES (?, ?, ?)",
            (group_id, int(user_id), role)
        )
        db.commit()
        return jsonify({"message": "Member added"}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "User already in group"}), 409

@app.route("/api/groups/<int:group_id>/members/<int:user_id>", methods=["PUT"])
@token_required
def update_group_member(group_id, user_id):
    db = get_db()
    if g.current_user_role != "superadmin":
        my_m = db.execute(
            "SELECT role FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, g.current_user_id)
        ).fetchone()
        if not my_m or my_m["role"] != "group_admin":
            return jsonify({"error": "Permission denied"}), 403
    data = request.get_json()
    role = data.get("role")
    if role not in ("member", "group_admin"):
        return jsonify({"error": "Invalid role"}), 400
    db.execute(
        "UPDATE group_members SET role = ? WHERE group_id = ? AND user_id = ?",
        (role, group_id, user_id)
    )
    db.commit()
    return jsonify({"message": "Updated"})

@app.route("/api/groups/<int:group_id>/members/<int:user_id>", methods=["DELETE"])
@token_required
def remove_group_member(group_id, user_id):
    db = get_db()
    if g.current_user_role != "superadmin":
        my_m = db.execute(
            "SELECT role FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, g.current_user_id)
        ).fetchone()
        if not my_m or my_m["role"] != "group_admin":
            return jsonify({"error": "Permission denied"}), 403
    db.execute(
        "DELETE FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user_id)
    )
    db.commit()
    return jsonify({"message": "Removed"})

# ─── Invite routes ───────────────────────────────────────────────────

@app.route("/api/groups/<int:group_id>/invites", methods=["POST"])
@token_required
def create_invite(group_id):
    db = get_db()
    if g.current_user_role != "superadmin":
        my_m = db.execute(
            "SELECT role FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, g.current_user_id)
        ).fetchone()
        if not my_m or my_m["role"] != "group_admin":
            return jsonify({"error": "Permission denied"}), 403
    token = secrets.token_urlsafe(16)
    db.execute(
        "INSERT INTO group_invites (group_id, token, created_by) VALUES (?, ?, ?)",
        (group_id, token, g.current_user_id)
    )
    db.commit()
    return jsonify({"token": token}), 201

@app.route("/api/invites/<token>", methods=["GET"])
def get_invite(token):
    db = get_db()
    invite = db.execute("""
        SELECT gi.group_id, g.name as group_name,
               (SELECT COUNT(*) FROM group_members WHERE group_id = g.id) as member_count
        FROM group_invites gi JOIN groups g ON gi.group_id = g.id
        WHERE gi.token = ?
    """, (token,)).fetchone()
    if not invite:
        return jsonify({"error": "Invalid invite link"}), 404
    return jsonify({
        "token": token,
        "group_id": invite["group_id"],
        "group_name": invite["group_name"],
        "member_count": invite["member_count"]
    })

@app.route("/api/invites/<token>/join", methods=["POST"])
@token_required
def join_via_invite(token):
    db = get_db()
    invite = db.execute(
        "SELECT * FROM group_invites WHERE token = ?", (token,)
    ).fetchone()
    if not invite:
        return jsonify({"error": "Invalid invite link"}), 404
    try:
        db.execute(
            "INSERT INTO group_members (group_id, user_id, role) VALUES (?, ?, 'member')",
            (invite["group_id"], g.current_user_id)
        )
        db.commit()
        return jsonify({"message": "Joined", "group_id": invite["group_id"]}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "Already a member"}), 409

# ─── Laptime CRUD ────────────────────────────────────────────────────

@app.route("/api/laptimes", methods=["POST"])
@token_required
def create_laptime():
    data = request.get_json()
    track = data.get("track", "").strip()
    car = data.get("car", "").strip()
    laptime_ms = data.get("laptime_ms")
    weather = data.get("weather", "Clear").strip()
    notes = data.get("notes", "").strip()
    recorded_at = data.get("recorded_at", datetime.utcnow().isoformat())
    target_user_id = data.get("user_id")

    if not track or not car or laptime_ms is None:
        return jsonify({"error": "Track, car, and laptime required"}), 400

    db = get_db()
    lap_owner_id = g.current_user_id

    if target_user_id and int(target_user_id) != g.current_user_id:
        if g.current_user_role == "superadmin":
            lap_owner_id = int(target_user_id)
        else:
            shared = db.execute("""
                SELECT 1 FROM group_members ga
                JOIN group_members gm ON ga.group_id = gm.group_id
                WHERE ga.user_id = ? AND ga.role = 'group_admin' AND gm.user_id = ?
            """, (g.current_user_id, int(target_user_id))).fetchone()
            if shared:
                lap_owner_id = int(target_user_id)
            else:
                return jsonify({"error": "Permission denied"}), 403

    cursor = db.execute(
        """INSERT INTO laptimes (user_id, track, car, laptime_ms, weather, notes, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (lap_owner_id, track, car, int(laptime_ms), weather, notes, recorded_at),
    )
    db.commit()
    return jsonify({"id": cursor.lastrowid, "message": "Lap recorded"}), 201

@app.route("/api/laptimes", methods=["GET"])
@token_required
def get_laptimes():
    db = get_db()
    user_filter = request.args.get("user_id")
    track_filter = request.args.get("track")
    car_filter = request.args.get("car")

    query = """
        SELECT l.*, u.display_name, u.username
        FROM laptimes l JOIN users u ON l.user_id = u.id
        WHERE 1=1
    """
    params = []
    if user_filter:
        query += " AND l.user_id = ?"
        params.append(int(user_filter))
    if track_filter:
        query += " AND l.track = ?"
        params.append(track_filter)
    if car_filter:
        query += " AND l.car = ?"
        params.append(car_filter)
    query += " ORDER BY l.recorded_at DESC"
    rows = db.execute(query, params).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/laptimes/<int:lap_id>", methods=["DELETE"])
@token_required
def delete_laptime(lap_id):
    db = get_db()
    lap = db.execute("SELECT * FROM laptimes WHERE id = ?", (lap_id,)).fetchone()
    if not lap:
        return jsonify({"error": "Lap not found"}), 404

    if lap["user_id"] == g.current_user_id or g.current_user_role == "superadmin":
        pass
    else:
        shared = db.execute("""
            SELECT 1 FROM group_members ga
            JOIN group_members gm ON ga.group_id = gm.group_id
            WHERE ga.user_id = ? AND ga.role = 'group_admin' AND gm.user_id = ?
        """, (g.current_user_id, lap["user_id"])).fetchone()
        if not shared:
            return jsonify({"error": "Lap not found or not yours"}), 404

    db.execute("DELETE FROM laptimes WHERE id = ?", (lap_id,))
    db.commit()
    return jsonify({"message": "Deleted"})

@app.route("/api/laptimes/<int:lap_id>", methods=["PUT"])
@token_required
def update_laptime(lap_id):
    db = get_db()
    lap = db.execute("SELECT * FROM laptimes WHERE id = ? AND user_id = ?", (lap_id, g.current_user_id)).fetchone()
    if not lap:
        return jsonify({"error": "Lap not found or not yours"}), 404

    data = request.get_json()
    db.execute(
        """UPDATE laptimes SET track=?, car=?, laptime_ms=?, weather=?, notes=?, recorded_at=?
           WHERE id=?""",
        (
            data.get("track", lap["track"]),
            data.get("car", lap["car"]),
            int(data.get("laptime_ms", lap["laptime_ms"])),
            data.get("weather", lap["weather"]),
            data.get("notes", lap["notes"]),
            data.get("recorded_at", lap["recorded_at"]),
            lap_id,
        ),
    )
    db.commit()
    return jsonify({"message": "Updated"})

# ─── Leaderboard & PBs ──────────────────────────────────────────────

@app.route("/api/leaderboard", methods=["GET"])
@token_required
def leaderboard():
    track = request.args.get("track")
    car = request.args.get("car")
    db = get_db()
    query = """
        SELECT l.user_id, u.display_name, u.username, l.track, l.car,
               MIN(l.laptime_ms) as best_time, COUNT(*) as total_laps
        FROM laptimes l JOIN users u ON l.user_id = u.id
        WHERE 1=1
    """
    params = []
    if track:
        query += " AND l.track = ?"
        params.append(track)
    if car:
        query += " AND l.car = ?"
        params.append(car)
    query += " GROUP BY l.user_id, l.track, l.car ORDER BY best_time ASC"
    rows = db.execute(query, params).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/personal-bests", methods=["GET"])
@token_required
def personal_bests():
    user_id = request.args.get("user_id", g.current_user_id)
    db = get_db()
    rows = db.execute(
        """SELECT track, car, MIN(laptime_ms) as best_time, COUNT(*) as attempts
           FROM laptimes WHERE user_id = ?
           GROUP BY track, car ORDER BY track, car""",
        (user_id,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/progress", methods=["GET"])
@token_required
def progress():
    track = request.args.get("track")
    car = request.args.get("car")
    user_id = request.args.get("user_id", g.current_user_id)
    db = get_db()
    query = "SELECT laptime_ms, recorded_at, weather, notes FROM laptimes WHERE user_id = ?"
    params = [user_id]
    if track:
        query += " AND track = ?"
        params.append(track)
    if car:
        query += " AND car = ?"
        params.append(car)
    query += " ORDER BY recorded_at ASC"
    rows = db.execute(query, params).fetchall()
    return jsonify([dict(r) for r in rows])

# ─── Metadata ────────────────────────────────────────────────────────

@app.route("/api/meta/tracks", methods=["GET"])
@token_required
def get_tracks():
    db = get_db()
    rows = db.execute("SELECT DISTINCT track FROM laptimes ORDER BY track").fetchall()
    return jsonify([r["track"] for r in rows])

@app.route("/api/meta/cars", methods=["GET"])
@token_required
def get_cars():
    db = get_db()
    rows = db.execute("SELECT DISTINCT car FROM laptimes ORDER BY car").fetchall()
    return jsonify([r["car"] for r in rows])

@app.route("/api/meta/users", methods=["GET"])
@token_required
def get_users():
    db = get_db()
    rows = db.execute("SELECT id, username, display_name FROM users ORDER BY display_name").fetchall()
    return jsonify([dict(r) for r in rows])

# ─── Export ──────────────────────────────────────────────────────────

@app.route("/api/export/csv", methods=["GET"])
@token_required
def export_csv():
    db = get_db()
    rows = db.execute(
        """SELECT u.display_name as driver, l.track, l.car, l.laptime_ms, l.weather, l.notes, l.recorded_at
           FROM laptimes l JOIN users u ON l.user_id = u.id
           ORDER BY l.recorded_at DESC"""
    ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Driver", "Track", "Car", "Laptime (ms)", "Laptime (formatted)", "Weather", "Notes", "Date"])
    for r in rows:
        ms = r["laptime_ms"]
        minutes = ms // 60000
        seconds = (ms % 60000) // 1000
        millis = ms % 1000
        formatted = f"{minutes}:{seconds:02d}.{millis:03d}"
        writer.writerow([r["driver"], r["track"], r["car"], ms, formatted, r["weather"], r["notes"], r["recorded_at"]])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=ace_laptimes_{datetime.now().strftime('%Y%m%d')}.csv"},
    )

@app.route("/api/export/json", methods=["GET"])
@token_required
def export_json():
    db = get_db()
    rows = db.execute(
        """SELECT u.display_name as driver, l.track, l.car, l.laptime_ms, l.weather, l.notes, l.recorded_at
           FROM laptimes l JOIN users u ON l.user_id = u.id
           ORDER BY l.recorded_at DESC"""
    ).fetchall()
    return Response(
        json.dumps([dict(r) for r in rows], indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename=ace_laptimes_{datetime.now().strftime('%Y%m%d')}.json"},
    )

# ─── Health ──────────────────────────────────────────────────────────

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
