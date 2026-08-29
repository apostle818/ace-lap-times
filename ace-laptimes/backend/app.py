import os
import json
import sqlite3
import csv
import io
import secrets
import hashlib
import hmac
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify, g, Response
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import bcrypt
import jwt

app = Flask(__name__)

# ─── Secret key ──────────────────────────────────────────────────────
#
# There is deliberately no fallback. A default here would be published in
# this repository, and anyone holding it can forge a token for any account
# and any role — so an unset or placeholder key has to stop the server
# rather than quietly produce a working but wide-open instance.

MIN_SECRET_KEY_LENGTH = 32

# Values that have appeared in this repo's docs and compose file over time.
# Someone who pastes one into .env is no better off than someone who set
# nothing at all, so they are refused by name.
_REJECTED_SECRET_KEYS = {
    "dev-secret-key",
    "change-me-to-a-random-string",
    "your-random-secret-here",
    "ci-placeholder",
    "changeme",
    "secret",
}

def _load_secret_key():
    key = os.environ.get("SECRET_KEY", "").strip()
    hint = "Generate one with:  openssl rand -hex 32"
    if not key:
        raise RuntimeError(
            "SECRET_KEY is not set. The server will not start without one, "
            f"because sessions signed with a guessable key are forgeable. {hint}"
        )
    if key.lower() in _REJECTED_SECRET_KEYS:
        raise RuntimeError(
            "SECRET_KEY is set to a well-known placeholder value from this "
            f"project's documentation. Choose a real secret. {hint}"
        )
    if len(key) < MIN_SECRET_KEY_LENGTH:
        raise RuntimeError(
            f"SECRET_KEY is {len(key)} characters; at least "
            f"{MIN_SECRET_KEY_LENGTH} are required. {hint}"
        )
    return key

app.config["SECRET_KEY"] = _load_secret_key()
DATABASE_PATH = os.environ.get("DATABASE_PATH", "./data/laptimes.db")

# Nothing this API accepts is remotely this large; anything bigger is a
# mistake or an attempt to fill the disk, and Flask rejects it with a 413
# before a handler ever sees it.
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024

# ─── Client addresses behind the proxy ───────────────────────────────
#
# In the shipped stack nginx is the only hop, so exactly one entry of
# X-Forwarded-For is ours and the rest is whatever the client sent. ProxyFix
# takes the correct one and puts it in request.remote_addr; nothing else in
# the app reads the header directly.
#
# This has to be right for rate limiting to work at all: without it every
# request appears to come from the nginx container and all users share a
# single bucket. Set TRUSTED_PROXY_HOPS to 0 when the backend is exposed
# directly (then no forwarding header is trusted), or higher when you run
# additional proxies in front of nginx.

TRUSTED_PROXY_HOPS = int(os.environ.get("TRUSTED_PROXY_HOPS", "1"))
if TRUSTED_PROXY_HOPS > 0:
    app.wsgi_app = ProxyFix(
        app.wsgi_app, x_for=TRUSTED_PROXY_HOPS, x_proto=TRUSTED_PROXY_HOPS,
        x_host=0, x_prefix=0,
    )

# ─── Rate limiting ───────────────────────────────────────────────────
#
# Credential endpoints get strict per-IP limits; everything else gets a
# generous default that only catches runaway clients and scripted abuse.
#
# Storage is in-process, so with the default two gunicorn workers the real
# ceiling is roughly double the configured number. That is accurate enough
# to stop brute force and avoids making Redis a requirement for a homelab
# deployment. Set RATELIMIT_STORAGE_URI to a shared backend if you scale out.

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["600 per hour"],
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
    strategy="fixed-window",
    headers_enabled=True,
)

def _rate_limit_key_username():
    """Limit by the username being attempted, so one account cannot be
    ground down from many source addresses."""
    data = request.get_json(silent=True) or {}
    return (data.get("username") or "").strip().lower() or get_remote_address()

@app.errorhandler(429)
def _ratelimited(e):
    return jsonify({
        "error": "Too many attempts. Wait a minute and try again."
    }), 429

# Everything the client talks to is JSON, so the error paths are too —
# otherwise the frontend gets Werkzeug's HTML page and fails to parse it.

@app.errorhandler(413)
def _too_large(e):
    return jsonify({"error": "Request too large"}), 413

@app.errorhandler(404)
def _not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found"}), 404
    return e

@app.errorhandler(500)
def _server_error(e):
    # The traceback still goes to the log; the client gets no internals.
    return jsonify({"error": "Something went wrong on the server"}), 500

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
            token_version INTEGER NOT NULL DEFAULT 0,
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
            expires_at TEXT,
            max_uses INTEGER,
            uses INTEGER NOT NULL DEFAULT 0,
            revoked_at TEXT,
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
        CREATE TABLE IF NOT EXISTS client_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            hostname TEXT DEFAULT '',
            platform TEXT DEFAULT '',
            app_version TEXT DEFAULT '',
            user_agent TEXT DEFAULT '',
            ip_address TEXT DEFAULT '',
            started_at TEXT DEFAULT (datetime('now')),
            last_seen_at TEXT DEFAULT (datetime('now')),
            disconnected_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_client_sessions_last_seen ON client_sessions(last_seen_at);
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            key_id TEXT UNIQUE NOT NULL,
            key_hash TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'tray',
            created_at TEXT DEFAULT (datetime('now')),
            last_used_at TEXT,
            expires_at TEXT,
            revoked_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
        CREATE INDEX IF NOT EXISTS idx_api_keys_key_id ON api_keys(key_id);
    """)
    # Migrations
    user_cols = [r[1] for r in db.execute("PRAGMA table_info(users)").fetchall()]
    if 'role' not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")
        db.commit()
    if 'bio' not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN bio TEXT DEFAULT ''")
        db.commit()
    if 'token_version' not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0")
        db.commit()
    invite_cols = [r[1] for r in db.execute("PRAGMA table_info(group_invites)").fetchall()]
    # Invites created before this release have no limits recorded. They are
    # left usable rather than silently broken, but they now show up in the
    # UI where a group admin can see and revoke them.
    if 'expires_at' not in invite_cols:
        db.execute("ALTER TABLE group_invites ADD COLUMN expires_at TEXT")
        db.execute("ALTER TABLE group_invites ADD COLUMN max_uses INTEGER")
        db.execute("ALTER TABLE group_invites ADD COLUMN uses INTEGER NOT NULL DEFAULT 0")
        db.execute("ALTER TABLE group_invites ADD COLUMN revoked_at TEXT")
        db.commit()

    group_cols = [r[1] for r in db.execute("PRAGMA table_info(groups)").fetchall()]
    if 'description' not in group_cols:
        db.execute("ALTER TABLE groups ADD COLUMN description TEXT DEFAULT ''")
        db.commit()
    db.close()

init_db()

# ─── API keys ────────────────────────────────────────────────────────
#
# Format:  alt_<key_id>_<secret>
#   e.g.   alt_30962196d5e1_LLMkH0BiKIlYp1CccpvVj2nx_STy20zO-NLMk5CftBw
#
# key_id is the public half (hex, indexed) and the secret half carries 256
# bits of entropy. Only the SHA-256 of the whole key is stored; the plaintext
# is shown once at creation and is unrecoverable afterwards. SHA-256 rather
# than bcrypt is deliberate: with that much entropy there is nothing to
# brute-force, and bcrypt would add ~100ms to every lap upload and heartbeat.

API_KEY_PREFIX = "alt"
API_KEY_ID_BYTES = 6       # 12 hex chars, the public half used for lookup
API_KEY_SECRET_BYTES = 32  # ~43 chars, ~256 bits of entropy

def generate_api_key():
    """Return (full_key, key_id, key_hash). Full key is never stored."""
    # key_id is hex, never urlsafe base64: the base64url alphabet contains
    # "_", which would break parsing of the underscore-delimited key.
    key_id = secrets.token_hex(API_KEY_ID_BYTES)
    secret = secrets.token_urlsafe(API_KEY_SECRET_BYTES)
    full = f"{API_KEY_PREFIX}_{key_id}_{secret}"
    return full, key_id, hash_api_key(full)

def hash_api_key(full_key):
    return hashlib.sha256(full_key.encode()).hexdigest()

def parse_api_key(full_key):
    """Split a presented key into its public id, or None if malformed."""
    # maxsplit=2: the secret half may legitimately contain "_".
    parts = full_key.split("_", 2)
    if len(parts) != 3 or parts[0] != API_KEY_PREFIX:
        return None
    if not parts[1] or not parts[2]:
        return None
    return parts[1]

def _touch_api_key(db, row):
    """Record last_used_at, at most once a minute to avoid a write per request."""
    now = datetime.utcnow()
    last = row["last_used_at"]
    if last:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < 60:
                return
        except ValueError:
            pass
    db.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?",
               (now.isoformat(timespec="seconds"), row["id"]))
    db.commit()

def _authenticate_api_key(presented):
    """
    Validate an X-API-Key header. On success populate g and return None,
    otherwise return a Flask error response.
    """
    key_id = parse_api_key(presented)
    if not key_id:
        return jsonify({"error": "Invalid API key"}), 401

    db = get_db()
    row = db.execute(
        "SELECT * FROM api_keys WHERE key_id = ?", (key_id,)
    ).fetchone()
    # Compare unconditionally against a dummy hash when the id is unknown so
    # that valid and invalid key ids take the same amount of work.
    expected = row["key_hash"] if row else "0" * 64
    matched = hmac.compare_digest(expected, hash_api_key(presented))
    if not row or not matched:
        return jsonify({"error": "Invalid API key"}), 401

    if row["revoked_at"]:
        return jsonify({"error": "API key revoked"}), 401
    if row["expires_at"]:
        try:
            if datetime.fromisoformat(row["expires_at"]) < datetime.utcnow():
                return jsonify({"error": "API key expired"}), 401
        except ValueError:
            pass

    user = db.execute(
        "SELECT id, username, role FROM users WHERE id = ?", (row["user_id"],)
    ).fetchone()
    if not user:
        return jsonify({"error": "Invalid API key"}), 401

    _touch_api_key(db, row)

    g.current_user_id = user["id"]
    g.current_username = user["username"]
    # An API key never carries the owner's role. Even a superadmin's key acts
    # as a plain member, so a leaked tray key cannot reach admin routes.
    g.current_user_role = "member"
    g.auth_method = "api_key"
    g.current_api_key_id = row["id"]
    g.current_api_key_scope = row["scope"]
    return None

# ─── Auth helpers ────────────────────────────────────────────────────

# A token identifies who is asking; it never says what they are allowed to
# do. Role, and the account's continued existence, are read from the database
# on every request — a token that carried its own role stayed superadmin for
# its full lifetime after the account was demoted, and kept working after the
# account was deleted outright.
TOKEN_TTL = timedelta(days=int(os.environ.get("SESSION_DAYS", "7")))

def create_token(user_id, username, token_version):
    payload = {
        "user_id": user_id,
        "username": username,
        # Bumped in the users row whenever sessions must be cut off; a token
        # carrying a stale value is refused. This is what makes a demotion,
        # a deletion or a forced sign-out take effect immediately.
        "tv": token_version,
        "exp": datetime.utcnow() + TOKEN_TTL,
    }
    return jwt.encode(payload, app.config["SECRET_KEY"], algorithm="HS256")

def _parse_token():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"error": "Token required"}), 401
    token = auth_header[7:]
    try:
        data = jwt.decode(token, app.config["SECRET_KEY"], algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Token expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid token"}), 401

    user = get_db().execute(
        "SELECT id, username, role, token_version FROM users WHERE id = ?",
        (data.get("user_id"),),
    ).fetchone()
    if not user:
        return jsonify({"error": "Session no longer valid"}), 401
    if data.get("tv") != user["token_version"]:
        return jsonify({"error": "Session expired - please sign in again"}), 401

    g.current_user_id = user["id"]
    g.current_username = user["username"]
    g.current_user_role = user["role"]
    return None

def token_required(f):
    """
    JWT only. API keys are deliberately rejected here: every route added in
    future is key-inaccessible by default, and only the handful explicitly
    marked with @token_or_key_required opens up to them.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.headers.get("X-API-Key"):
            return jsonify({
                "error": "This endpoint requires a user session, not an API key"
            }), 403
        err = _parse_token()
        if err:
            return err
        g.auth_method = "jwt"
        return f(*args, **kwargs)
    return decorated

def token_or_key_required(f):
    """
    Accepts either a JWT or a scope='tray' API key. Applied only to the
    endpoints the tray app actually needs.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        presented = request.headers.get("X-API-Key", "").strip()
        if presented:
            err = _authenticate_api_key(presented)
            if err:
                return err
            if g.current_api_key_scope != "tray":
                return jsonify({"error": "API key scope does not allow this"}), 403
            return f(*args, **kwargs)
        err = _parse_token()
        if err:
            return err
        g.auth_method = "jwt"
        return f(*args, **kwargs)
    return decorated

def superadmin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.headers.get("X-API-Key"):
            return jsonify({
                "error": "This endpoint requires a user session, not an API key"
            }), 403
        err = _parse_token()
        if err:
            return err
        if g.current_user_role != "superadmin":
            return jsonify({"error": "Superadmin access required"}), 403
        return f(*args, **kwargs)
    return decorated

# ─── Passwords ───────────────────────────────────────────────────────

MIN_PASSWORD_LENGTH = 12
# bcrypt refuses anything longer outright rather than truncating, so reject
# it here with a clear message instead of raising deep in the hash call.
MAX_PASSWORD_BYTES = 72

# A handful of passwords that show up at the top of every breach corpus.
# Not a substitute for a real list, but it catches the worst choices.
_COMMON_PASSWORDS = {
    "password", "password1", "password123", "passw0rd", "123456", "1234567",
    "12345678", "123456789", "1234567890", "qwerty", "qwerty123", "iloveyou",
    "admin", "administrator", "welcome", "welcome1", "letmein", "monkey",
    "abc123", "football", "baseball", "dragon", "sunshine", "princess",
    "changeme", "secret", "trustno1", "assettocorsa", "acelaptracker",
}

def validate_password(password):
    """Return an error string, or None when the password is acceptable."""
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        return f"Password must be at most {MAX_PASSWORD_BYTES} bytes"
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
    if password.lower() in _COMMON_PASSWORDS:
        return "That password is too common - pick something else"
    return None

def hash_password(password):
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def check_password(password, password_hash):
    """Verify a password, returning False rather than raising on bad input."""
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except (ValueError, TypeError):
        return False

# Verified against when the username does not exist, so that a login attempt
# on an unknown account costs the same as one on a real account. Without it
# the miss returns in ~2 ms and the hit in ~280 ms, which is a clean oracle
# for deciding whether a username is registered.
_DUMMY_PASSWORD_HASH = bcrypt.hashpw(b"dummy-password-for-timing", bcrypt.gensalt()).decode()

# ─── Input limits ────────────────────────────────────────────────────
#
# Free text was unbounded, so a single request could store a megabyte and
# fill the volume behind the SQLite file. These caps are generous for real
# use and are enforced on write rather than silently truncating.

FIELD_LIMITS = {
    "username": 64,
    "display_name": 80,
    "bio": 500,
    "track": 120,
    "car": 120,
    "weather": 40,
    "notes": 1000,
    "recorded_at": 40,
    "group_name": 80,
    "group_description": 500,
    "key_name": 100,
    "client_id": 100,
}

# A lap between 1 ms and 24 hours. Wider than any real lap, narrow enough
# that a bad value is caught rather than stored.
MAX_LAPTIME_MS = 24 * 60 * 60 * 1000

def clean_text(data, field, limit_key=None, default=""):
    """Trimmed string for `field`, or None when it exceeds its limit."""
    raw = data.get(field, default)
    if raw is None:
        raw = ""
    text = str(raw).strip()
    if len(text) > FIELD_LIMITS[limit_key or field]:
        return None
    return text

def too_long(field, limit_key=None):
    limit = FIELD_LIMITS[limit_key or field]
    return jsonify({"error": f"{field.replace('_', ' ').capitalize()} must be at most {limit} characters"}), 400

def parse_int(value, name, minimum=None, maximum=None):
    """Return (value, None) or (None, error_response)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None, (jsonify({"error": f"{name} must be a whole number"}), 400)
    if minimum is not None and n < minimum:
        return None, (jsonify({"error": f"{name} must be at least {minimum}"}), 400)
    if maximum is not None and n > maximum:
        return None, (jsonify({"error": f"{name} must be at most {maximum}"}), 400)
    return n, None

# ─── Visibility ──────────────────────────────────────────────────────
#
# Groups are a privacy boundary: you see your own laps and the laps of people
# you share at least one group with, and nothing else. A superadmin sees
# everything. Before this, every read endpoint took a caller-supplied user_id
# and answered for any account on the instance, so group membership gated
# writes but not a single read.
#
# The user directory (/api/meta/users) is deliberately not scoped this way
# for group admins — they need to see who exists in order to add members —
# but it carries no lap data, only id, username and display name.

def _visible_user_ids(db, user_id):
    """Ids whose lap data `user_id` may read: themselves plus co-members."""
    rows = db.execute(
        """SELECT DISTINCT theirs.user_id
           FROM group_members mine
           JOIN group_members theirs ON mine.group_id = theirs.group_id
           WHERE mine.user_id = ?""",
        (user_id,),
    ).fetchall()
    ids = {r["user_id"] for r in rows}
    ids.add(user_id)  # you can always see yourself, groups or not
    return ids

def _visibility_clause(column):
    """
    SQL fragment and params restricting `column` to the visible set. Returns
    ("", []) for a superadmin. The set always contains the caller, so the IN
    list is never empty.
    """
    if g.current_user_role == "superadmin":
        return "", []
    ids = sorted(_visible_user_ids(get_db(), g.current_user_id))
    return f" AND {column} IN ({','.join('?' * len(ids))})", ids

def _can_view_user(target_user_id):
    if g.current_user_role == "superadmin":
        return True
    return int(target_user_id) in _visible_user_ids(get_db(), g.current_user_id)

def _is_group_admin_somewhere(db, user_id):
    return db.execute(
        "SELECT 1 FROM group_members WHERE user_id = ? AND role = 'group_admin'",
        (user_id,),
    ).fetchone() is not None

# ─── Auth routes ─────────────────────────────────────────────────────

@app.route("/api/auth/register", methods=["POST"])
@limiter.limit("5 per hour; 20 per day")
def register():
    data = request.get_json(silent=True) or {}
    username = clean_text(data, "username").lower() if clean_text(data, "username") is not None else None
    display_name = clean_text(data, "display_name")
    password = data.get("password", "") or ""

    if username is None:
        return too_long("username")
    if display_name is None:
        return too_long("display_name")
    if not username or not password or not display_name:
        return jsonify({"error": "All fields required"}), 400
    err = validate_password(password)
    if err:
        return jsonify({"error": err}), 400

    password_hash = hash_password(password)
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
        token = create_token(user_id, username, 0)
        return jsonify({
            "token": token,
            "user": {"id": user_id, "username": username, "display_name": display_name, "role": role, "groups": []}
        }), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "Username already taken"}), 409

@app.route("/api/auth/login", methods=["POST"])
@limiter.limit("10 per minute; 50 per hour")
@limiter.limit("5 per minute; 20 per hour", key_func=_rate_limit_key_username)
def login():
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    # Hash unconditionally, against a dummy when the username is unknown, so
    # both outcomes take the same time and neither confirms the account exists.
    expected_hash = user["password_hash"] if user else _DUMMY_PASSWORD_HASH
    password_ok = check_password(password, expected_hash)
    if not user or not password_ok:
        return jsonify({"error": "Invalid credentials"}), 401

    groups = db.execute("""
        SELECT g.id, g.name, gm.role as group_role
        FROM groups g JOIN group_members gm ON g.id = gm.group_id
        WHERE gm.user_id = ?
    """, (user["id"],)).fetchall()

    token = create_token(user["id"], user["username"], user["token_version"])
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
@token_or_key_required
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
    data = request.get_json(silent=True) or {}
    display_name = clean_text(data, "display_name")
    bio = clean_text(data, "bio")
    if display_name is None:
        return too_long("display_name")
    if bio is None:
        return too_long("bio")
    if not display_name:
        return jsonify({"error": "Display name required"}), 400
    db = get_db()
    db.execute("UPDATE users SET display_name = ?, bio = ? WHERE id = ?",
               (display_name, bio, g.current_user_id))
    db.commit()
    return jsonify({"message": "Profile updated"})

@app.route("/api/auth/sessions", methods=["DELETE"])
@token_required
def revoke_sessions():
    """
    Sign out everywhere. Invalidates every token issued to this account —
    including any copied out of another browser — and returns a fresh one so
    the caller stays signed in here.
    """
    db = get_db()
    db.execute(
        "UPDATE users SET token_version = token_version + 1 WHERE id = ?",
        (g.current_user_id,),
    )
    db.commit()
    user = db.execute(
        "SELECT id, username, token_version FROM users WHERE id = ?",
        (g.current_user_id,),
    ).fetchone()
    return jsonify({
        "message": "All other sessions signed out",
        "token": create_token(user["id"], user["username"], user["token_version"]),
    })

# ─── API key management ──────────────────────────────────────────────

def _key_row_to_json(r):
    return {
        "id": r["id"],
        "name": r["name"],
        "key_id": r["key_id"],
        # Enough to recognise a key in the UI without revealing it.
        "masked": f"{API_KEY_PREFIX}_{r['key_id']}_" + "\u2022" * 8,
        "scope": r["scope"],
        "created_at": r["created_at"],
        "last_used_at": r["last_used_at"],
        "expires_at": r["expires_at"],
        "revoked_at": r["revoked_at"],
    }

@app.route("/api/keys", methods=["GET"])
@token_required
def list_api_keys():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM api_keys WHERE user_id = ? ORDER BY revoked_at IS NOT NULL, created_at DESC",
        (g.current_user_id,),
    ).fetchall()
    return jsonify([_key_row_to_json(r) for r in rows])

@app.route("/api/keys", methods=["POST"])
@limiter.limit("10 per hour")
@token_required
def create_api_key():
    data = request.get_json(silent=True) or {}
    name = clean_text(data, "name", "key_name")
    if name is None:
        return too_long("key name", "key_name")
    if not name:
        return jsonify({"error": "Key name required"}), 400

    expires_at = None
    raw_days = data.get("expires_in_days")
    if raw_days not in (None, "", 0, "0"):
        try:
            days = int(raw_days)
        except (TypeError, ValueError):
            return jsonify({"error": "expires_in_days must be a number"}), 400
        if days < 1 or days > 3650:
            return jsonify({"error": "expires_in_days must be between 1 and 3650"}), 400
        expires_at = (datetime.utcnow() + timedelta(days=days)).isoformat(timespec="seconds")

    db = get_db()
    active = db.execute(
        "SELECT COUNT(*) FROM api_keys WHERE user_id = ? AND revoked_at IS NULL",
        (g.current_user_id,),
    ).fetchone()[0]
    if active >= 25:
        return jsonify({"error": "Too many active keys - revoke some first"}), 429

    for _attempt in range(5):
        full_key, key_id, key_hash = generate_api_key()
        try:
            cursor = db.execute(
                """INSERT INTO api_keys (user_id, name, key_id, key_hash, scope, expires_at)
                   VALUES (?, ?, ?, ?, 'tray', ?)""",
                (g.current_user_id, name, key_id, key_hash, expires_at),
            )
            break
        except sqlite3.IntegrityError:
            continue  # key_id collision; regenerate
    else:
        return jsonify({"error": "Could not allocate a key, try again"}), 500
    db.commit()
    row = db.execute("SELECT * FROM api_keys WHERE id = ?", (cursor.lastrowid,)).fetchone()
    payload = _key_row_to_json(row)
    # The only time the plaintext key is ever returned.
    payload["key"] = full_key
    return jsonify(payload), 201

@app.route("/api/keys/<int:key_id>", methods=["DELETE"])
@token_required
def revoke_api_key(key_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM api_keys WHERE id = ? AND user_id = ?", (key_id, g.current_user_id)
    ).fetchone()
    if not row:
        return jsonify({"error": "Key not found"}), 404
    if row["revoked_at"]:
        return jsonify({"message": "Already revoked"})
    db.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ?",
               (datetime.utcnow().isoformat(timespec="seconds"), key_id))
    db.commit()
    return jsonify({"message": "Key revoked"})

@app.route("/api/admin/keys", methods=["GET"])
@superadmin_required
def admin_list_api_keys():
    db = get_db()
    rows = db.execute(
        """SELECT k.*, u.username, u.display_name
           FROM api_keys k JOIN users u ON k.user_id = u.id
           ORDER BY k.revoked_at IS NOT NULL, k.created_at DESC"""
    ).fetchall()
    out = []
    for r in rows:
        item = _key_row_to_json(r)
        item.update({"user_id": r["user_id"], "username": r["username"],
                     "display_name": r["display_name"]})
        out.append(item)
    return jsonify(out)

@app.route("/api/admin/keys/<int:key_id>", methods=["DELETE"])
@superadmin_required
def admin_revoke_api_key(key_id):
    db = get_db()
    row = db.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    if not row:
        return jsonify({"error": "Key not found"}), 404
    db.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ?",
               (datetime.utcnow().isoformat(timespec="seconds"), key_id))
    db.commit()
    return jsonify({"message": "Key revoked"})

# ─── User profile ─────────────────────────────────────────────────────

@app.route("/api/users/<int:user_id>", methods=["GET"])
@token_required
def get_user_profile(user_id):
    if not _can_view_user(user_id):
        return jsonify({"error": "User not found"}), 404
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
    data = request.get_json(silent=True) or {}
    role = data.get("role")
    if role not in ("member", "superadmin"):
        return jsonify({"error": "Invalid role"}), 400
    db = get_db()
    # Bumping token_version cuts off sessions issued under the old role, so a
    # demotion takes effect now rather than whenever the token happens to
    # expire. The user signs in again and gets the role they actually have.
    db.execute(
        "UPDATE users SET role = ?, token_version = token_version + 1 WHERE id = ?",
        (role, user_id),
    )
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

# ─── Client session tracking ─────────────────────────────────────────

def _client_ip():
    # ProxyFix has already resolved this from X-Forwarded-For using the
    # trusted hop count; reading the raw header here would let any client
    # name its own address.
    return request.remote_addr or ""

@app.route("/api/client/heartbeat", methods=["POST"])
@token_or_key_required
def client_heartbeat():
    data = request.get_json(silent=True) or {}
    client_id = (data.get("client_id") or "").strip()
    if not client_id:
        return jsonify({"error": "client_id required"}), 400
    if len(client_id) > FIELD_LIMITS["client_id"]:
        return too_long("client_id")
    hostname = (data.get("hostname") or "").strip()[:200]
    platform = (data.get("platform") or "").strip()[:100]
    app_version = (data.get("app_version") or "").strip()[:50]
    user_agent = request.headers.get("User-Agent", "")[:300]
    ip = _client_ip()
    now = datetime.utcnow().isoformat(timespec="seconds")

    db = get_db()
    existing = db.execute(
        "SELECT id, user_id FROM client_sessions WHERE client_id = ?", (client_id,)
    ).fetchone()
    if existing and existing["user_id"] != g.current_user_id:
        # client_id comes from the caller. The update used to reassign the row
        # to whoever sent it, so anyone who learned another machine's id could
        # take over its entry in the admin Connected Clients panel and put
        # their own hostname and address against that driver's name.
        return jsonify({
            "error": "That client_id belongs to another account"
        }), 409
    if existing:
        db.execute(
            """UPDATE client_sessions
               SET hostname = ?, platform = ?, app_version = ?,
                   user_agent = ?, ip_address = ?, last_seen_at = ?, disconnected_at = NULL
               WHERE client_id = ? AND user_id = ?""",
            (hostname, platform, app_version,
             user_agent, ip, now, client_id, g.current_user_id),
        )
    else:
        db.execute(
            """INSERT INTO client_sessions
               (client_id, user_id, hostname, platform, app_version, user_agent,
                ip_address, started_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (client_id, g.current_user_id, hostname, platform, app_version,
             user_agent, ip, now, now),
        )
    db.commit()
    return jsonify({"ok": True, "server_time": now})

@app.route("/api/client/disconnect", methods=["POST"])
@token_or_key_required
def client_disconnect():
    data = request.get_json(silent=True) or {}
    client_id = (data.get("client_id") or "").strip()
    if not client_id:
        return jsonify({"error": "client_id required"}), 400
    now = datetime.utcnow().isoformat(timespec="seconds")
    db = get_db()
    db.execute(
        "UPDATE client_sessions SET disconnected_at = ?, last_seen_at = ? WHERE client_id = ? AND user_id = ?",
        (now, now, client_id, g.current_user_id),
    )
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/admin/clients", methods=["GET"])
@superadmin_required
def admin_list_clients():
    db = get_db()
    rows = db.execute(
        """SELECT c.id, c.client_id, c.user_id, c.hostname, c.platform, c.app_version,
                  c.user_agent, c.ip_address, c.started_at, c.last_seen_at, c.disconnected_at,
                  u.display_name, u.username
           FROM client_sessions c
           JOIN users u ON c.user_id = u.id
           ORDER BY c.last_seen_at DESC"""
    ).fetchall()
    return jsonify({
        "server_time": datetime.utcnow().isoformat(timespec="seconds"),
        "clients": [dict(r) for r in rows],
    })

@app.route("/api/admin/clients/<int:session_id>", methods=["DELETE"])
@superadmin_required
def admin_delete_client(session_id):
    db = get_db()
    db.execute("DELETE FROM client_sessions WHERE id = ?", (session_id,))
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
    data = request.get_json(silent=True) or {}
    name = clean_text(data, "name", "group_name")
    description = clean_text(data, "description", "group_description")
    if name is None:
        return too_long("group name", "group_name")
    if description is None:
        return too_long("group description", "group_description")
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
    data = request.get_json(silent=True) or {}
    description = clean_text(data, "description", "group_description")
    if description is None:
        return too_long("group description", "group_description")
    if is_superadmin:
        name = clean_text(data, "name", "group_name")
        if name is None:
            return too_long("group name", "group_name")
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
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    role = data.get("role", "member")
    if role not in ("member", "group_admin"):
        return jsonify({"error": "Invalid role"}), 400
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    user_id, err = parse_int(user_id, "user_id", minimum=1)
    if err:
        return err
    try:
        db.execute(
            "INSERT INTO group_members (group_id, user_id, role) VALUES (?, ?, ?)",
            (group_id, user_id, role)
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
    data = request.get_json(silent=True) or {}
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
    data = request.get_json(silent=True) or {}

    expires_at = None
    raw_days = data.get("expires_in_days", 7)
    if raw_days not in (None, "", 0, "0"):
        days, err = parse_int(raw_days, "expires_in_days", minimum=1, maximum=365)
        if err:
            return err
        expires_at = (datetime.utcnow() + timedelta(days=days)).isoformat(timespec="seconds")

    max_uses = None
    raw_uses = data.get("max_uses")
    if raw_uses not in (None, "", 0, "0"):
        max_uses, err = parse_int(raw_uses, "max_uses", minimum=1, maximum=1000)
        if err:
            return err

    token = secrets.token_urlsafe(16)
    db.execute(
        """INSERT INTO group_invites (group_id, token, created_by, expires_at, max_uses)
           VALUES (?, ?, ?, ?, ?)""",
        (group_id, token, g.current_user_id, expires_at, max_uses)
    )
    db.commit()
    return jsonify({"token": token, "expires_at": expires_at, "max_uses": max_uses}), 201


def _invite_problem(invite):
    """Why this invite cannot be used, or None when it is still good."""
    if invite["revoked_at"]:
        return "This invite link has been revoked"
    if invite["expires_at"]:
        try:
            if datetime.fromisoformat(invite["expires_at"]) < datetime.utcnow():
                return "This invite link has expired"
        except ValueError:
            pass
    if invite["max_uses"] is not None and invite["uses"] >= invite["max_uses"]:
        return "This invite link has already been used the maximum number of times"
    return None


@app.route("/api/groups/<int:group_id>/invites", methods=["GET"])
@token_required
def list_invites(group_id):
    db = get_db()
    if g.current_user_role != "superadmin":
        my_m = db.execute(
            "SELECT role FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, g.current_user_id)
        ).fetchone()
        if not my_m or my_m["role"] != "group_admin":
            return jsonify({"error": "Permission denied"}), 403
    rows = db.execute(
        """SELECT i.*, u.display_name as created_by_name
           FROM group_invites i JOIN users u ON i.created_by = u.id
           WHERE i.group_id = ?
           ORDER BY i.revoked_at IS NOT NULL, i.created_at DESC""",
        (group_id,)
    ).fetchall()
    return jsonify([{
        "id": r["id"],
        "token": r["token"],
        "created_at": r["created_at"],
        "created_by_name": r["created_by_name"],
        "expires_at": r["expires_at"],
        "max_uses": r["max_uses"],
        "uses": r["uses"],
        "revoked_at": r["revoked_at"],
        "problem": _invite_problem(r),
    } for r in rows])


@app.route("/api/groups/<int:group_id>/invites/<int:invite_id>", methods=["DELETE"])
@token_required
def revoke_invite(group_id, invite_id):
    db = get_db()
    if g.current_user_role != "superadmin":
        my_m = db.execute(
            "SELECT role FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, g.current_user_id)
        ).fetchone()
        if not my_m or my_m["role"] != "group_admin":
            return jsonify({"error": "Permission denied"}), 403
    row = db.execute(
        "SELECT * FROM group_invites WHERE id = ? AND group_id = ?", (invite_id, group_id)
    ).fetchone()
    if not row:
        return jsonify({"error": "Invite not found"}), 404
    if row["revoked_at"]:
        return jsonify({"message": "Already revoked"})
    db.execute("UPDATE group_invites SET revoked_at = ? WHERE id = ?",
               (datetime.utcnow().isoformat(timespec="seconds"), invite_id))
    db.commit()
    return jsonify({"message": "Invite revoked"})

@app.route("/api/invites/<token>", methods=["GET"])
@limiter.limit("30 per hour")
def get_invite(token):
    db = get_db()
    invite = db.execute("""
        SELECT gi.*, g.name as group_name,
               (SELECT COUNT(*) FROM group_members WHERE group_id = g.id) as member_count
        FROM group_invites gi JOIN groups g ON gi.group_id = g.id
        WHERE gi.token = ?
    """, (token,)).fetchone()
    if not invite:
        return jsonify({"error": "Invalid invite link"}), 404
    problem = _invite_problem(invite)
    if problem:
        # A spent link reveals nothing about the group it pointed at.
        return jsonify({"error": problem}), 410
    return jsonify({
        "token": token,
        "group_id": invite["group_id"],
        "group_name": invite["group_name"],
        "member_count": invite["member_count"]
    })

@app.route("/api/invites/<token>/join", methods=["POST"])
@limiter.limit("20 per hour")
@token_required
def join_via_invite(token):
    db = get_db()
    invite = db.execute(
        "SELECT * FROM group_invites WHERE token = ?", (token,)
    ).fetchone()
    if not invite:
        return jsonify({"error": "Invalid invite link"}), 404
    problem = _invite_problem(invite)
    if problem:
        return jsonify({"error": problem}), 410
    try:
        db.execute(
            "INSERT INTO group_members (group_id, user_id, role) VALUES (?, ?, 'member')",
            (invite["group_id"], g.current_user_id)
        )
        # Counted only on a join that actually happened, and guarded by
        # max_uses in SQL so two simultaneous joins cannot both slip past
        # the check above.
        db.execute(
            """UPDATE group_invites SET uses = uses + 1
               WHERE id = ? AND (max_uses IS NULL OR uses < max_uses)""",
            (invite["id"],)
        )
        db.commit()
        return jsonify({"message": "Joined", "group_id": invite["group_id"]}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "Already a member"}), 409

# ─── Laptime CRUD ────────────────────────────────────────────────────

@app.route("/api/laptimes", methods=["POST"])
@token_or_key_required
def create_laptime():
    data = request.get_json(silent=True) or {}
    track = clean_text(data, "track")
    car = clean_text(data, "car")
    weather = clean_text(data, "weather", default="Clear")
    notes = clean_text(data, "notes")
    recorded_at = clean_text(data, "recorded_at", default=datetime.utcnow().isoformat())
    laptime_ms = data.get("laptime_ms")
    target_user_id = data.get("user_id")

    for name, value in (("track", track), ("car", car), ("weather", weather),
                        ("notes", notes), ("recorded_at", recorded_at)):
        if value is None:
            return too_long(name)
    if not track or not car or laptime_ms is None:
        return jsonify({"error": "Track, car, and laptime required"}), 400

    laptime_ms, err = parse_int(laptime_ms, "Laptime", minimum=1, maximum=MAX_LAPTIME_MS)
    if err:
        return err

    db = get_db()
    lap_owner_id = g.current_user_id

    if target_user_id is not None and str(target_user_id) != "":
        target_user_id, err = parse_int(target_user_id, "user_id", minimum=1)
        if err:
            return err
    else:
        target_user_id = None

    if target_user_id and target_user_id != g.current_user_id:
        # API keys are self-scoped: they can never attribute a lap to
        # another driver, whatever role the owning account holds.
        if getattr(g, "auth_method", "jwt") == "api_key":
            return jsonify({"error": "API keys can only record your own laps"}), 403
        if g.current_user_role == "superadmin":
            lap_owner_id = target_user_id
        else:
            shared = db.execute("""
                SELECT 1 FROM group_members ga
                JOIN group_members gm ON ga.group_id = gm.group_id
                WHERE ga.user_id = ? AND ga.role = 'group_admin' AND gm.user_id = ?
            """, (g.current_user_id, target_user_id)).fetchone()
            if shared:
                lap_owner_id = target_user_id
            else:
                return jsonify({"error": "Permission denied"}), 403

    cursor = db.execute(
        """INSERT INTO laptimes (user_id, track, car, laptime_ms, weather, notes, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (lap_owner_id, track, car, laptime_ms, weather, notes, recorded_at),
    )
    db.commit()
    return jsonify({"id": cursor.lastrowid, "message": "Lap recorded"}), 201

@app.route("/api/laptimes", methods=["GET"])
@token_or_key_required
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
    scope_sql, scope_params = _visibility_clause("l.user_id")
    query += scope_sql
    params = list(scope_params)
    if user_filter:
        try:
            user_filter = int(user_filter)
        except (TypeError, ValueError):
            return jsonify({"error": "user_id must be a number"}), 400
        if not _can_view_user(user_filter):
            return jsonify({"error": "Driver not found"}), 404
        query += " AND l.user_id = ?"
        params.append(user_filter)
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

    data = request.get_json(silent=True) or {}
    fields = {}
    for name in ("track", "car", "weather", "notes", "recorded_at"):
        value = clean_text(data, name, default=lap[name])
        if value is None:
            return too_long(name)
        fields[name] = value
    laptime_ms, err = parse_int(
        data.get("laptime_ms", lap["laptime_ms"]), "Laptime",
        minimum=1, maximum=MAX_LAPTIME_MS,
    )
    if err:
        return err
    db.execute(
        """UPDATE laptimes SET track=?, car=?, laptime_ms=?, weather=?, notes=?, recorded_at=?
           WHERE id=?""",
        (fields["track"], fields["car"], laptime_ms, fields["weather"],
         fields["notes"], fields["recorded_at"], lap_id),
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
    scope_sql, scope_params = _visibility_clause("l.user_id")
    query += scope_sql
    params = list(scope_params)
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
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return jsonify({"error": "user_id must be a number"}), 400
    if not _can_view_user(user_id):
        return jsonify({"error": "Driver not found"}), 404
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
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return jsonify({"error": "user_id must be a number"}), 400
    if not _can_view_user(user_id):
        return jsonify({"error": "Driver not found"}), 404
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
@token_or_key_required
def get_tracks():
    db = get_db()
    scope_sql, scope_params = _visibility_clause("user_id")
    rows = db.execute(
        f"SELECT DISTINCT track FROM laptimes WHERE 1=1{scope_sql} ORDER BY track",
        scope_params,
    ).fetchall()
    return jsonify([r["track"] for r in rows])

@app.route("/api/meta/cars", methods=["GET"])
@token_or_key_required
def get_cars():
    db = get_db()
    scope_sql, scope_params = _visibility_clause("user_id")
    rows = db.execute(
        f"SELECT DISTINCT car FROM laptimes WHERE 1=1{scope_sql} ORDER BY car",
        scope_params,
    ).fetchall()
    return jsonify([r["car"] for r in rows])

@app.route("/api/meta/users", methods=["GET"])
@token_required
def get_users():
    db = get_db()
    # Group admins need the full directory to add people to their groups;
    # a plain member only ever sees the people they already share a group
    # with. Either way this returns no lap data.
    if g.current_user_role == "superadmin" or _is_group_admin_somewhere(db, g.current_user_id):
        rows = db.execute(
            "SELECT id, username, display_name FROM users ORDER BY display_name"
        ).fetchall()
    else:
        ids = sorted(_visible_user_ids(db, g.current_user_id))
        rows = db.execute(
            f"""SELECT id, username, display_name FROM users
                WHERE id IN ({','.join('?' * len(ids))}) ORDER BY display_name""",
            ids,
        ).fetchall()
    return jsonify([dict(r) for r in rows])

# ─── Export ──────────────────────────────────────────────────────────

# Excel, LibreOffice and Sheets treat a cell starting with any of these as a
# formula, so a lap note reading =cmd|'/c calc'!A1 executes when whoever runs
# the instance opens the export. Any member can plant one.
_CSV_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

def _csv_safe(value):
    """Render a cell so a spreadsheet always treats it as text."""
    if value is None:
        return ""
    text = str(value)
    if text.startswith(_CSV_INJECTION_PREFIXES):
        # A leading apostrophe is the standard escape and is not displayed.
        return "'" + text
    return text

@app.route("/api/export/csv", methods=["GET"])
@token_required
def export_csv():
    db = get_db()
    scope_sql, scope_params = _visibility_clause("l.user_id")
    rows = db.execute(
        f"""SELECT u.display_name as driver, l.track, l.car, l.laptime_ms,
                   l.weather, l.notes, l.recorded_at
            FROM laptimes l JOIN users u ON l.user_id = u.id
            WHERE 1=1{scope_sql}
            ORDER BY l.recorded_at DESC""",
        scope_params,
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
        writer.writerow([
            _csv_safe(r["driver"]), _csv_safe(r["track"]), _csv_safe(r["car"]),
            ms, formatted, _csv_safe(r["weather"]), _csv_safe(r["notes"]),
            _csv_safe(r["recorded_at"]),
        ])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=ace_laptimes_{datetime.now().strftime('%Y%m%d')}.csv"},
    )

@app.route("/api/export/json", methods=["GET"])
@token_required
def export_json():
    db = get_db()
    scope_sql, scope_params = _visibility_clause("l.user_id")
    rows = db.execute(
        f"""SELECT u.display_name as driver, l.track, l.car, l.laptime_ms,
                   l.weather, l.notes, l.recorded_at
            FROM laptimes l JOIN users u ON l.user_id = u.id
            WHERE 1=1{scope_sql}
            ORDER BY l.recorded_at DESC""",
        scope_params,
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
    # Loopback only. debug=True serves the Werkzeug console and source-bearing
    # tracebacks, and binding 0.0.0.0 offered both to everything on the
    # network. Set DEV_HOST deliberately if you really need to reach the dev
    # server from another machine — and turn the debugger off if you do.
    app.run(
        debug=os.environ.get("FLASK_DEBUG", "1") == "1",
        host=os.environ.get("DEV_HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
    )
