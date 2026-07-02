"""
LabSight Central Server — runs on the admin laptop.
Manages multiple Pi lockers over WebSockets (Socket.IO).

Install:
    pip install flask flask-socketio gevent werkzeug

Run:
    python server.py
    Then open http://<laptop-ip>:5000 in any browser on the network.

First-time setup — generate your password hash:
    python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('yourpassword'))"
    Paste the output into ADMIN_PASSWORD_HASH below.
"""

from flask import (Flask, render_template, jsonify, request,
                   session, redirect, url_for, abort)
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.security import check_password_hash, generate_password_hash
from functools import wraps
import threading, time, json, os
from datetime import datetime

app = Flask(__name__)

# ─── Secret key ──────────────────────────────────────────────────────────────
# CHANGE THIS to any long random string before deploying.
# It signs the session cookie — keep it secret.
app.config["SECRET_KEY"] = "awdjaedwjdlajwdjijilwdej"

# ─── Admin credentials ────────────────────────────────────────────────────────
# Username — change to whatever you like.
ADMIN_USERNAME = "admin"

# Password hash — generate yours by running:
#   python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('yourpassword'))"
# Then replace the string below with the output.
# Default password is:  labsight123
ADMIN_PASSWORD_HASH = "scrypt:32768:8:1$bSlYnojWUTjyICGG$7dd22a698d73c63a0f0f7be0dee78f92635a5457eef84ff4f65099b7d3f60187e31447a12205c1ae1809250a145ac128ef17d8fdfd2c266e76517ca6a896718e"

# ─── Brute-force rate limiting ────────────────────────────────────────────────
# Tracks failed login attempts per IP address.
# {ip: {"count": int, "locked_until": float}}
_login_attempts: dict = {}
_attempts_lock  = threading.Lock()

MAX_ATTEMPTS    = 5      # failed attempts before lockout
LOCKOUT_SECONDS = 60     # lockout duration in seconds


def _check_rate_limit(ip: str) -> tuple[bool, int]:
    """
    Returns (allowed, seconds_remaining).
    allowed=False means the IP is currently locked out.
    """
    with _attempts_lock:
        entry = _login_attempts.get(ip)
        if not entry:
            return True, 0
        if time.monotonic() < entry.get("locked_until", 0):
            remaining = int(entry["locked_until"] - time.monotonic())
            return False, remaining
        return True, 0


def _record_failed_attempt(ip: str):
    """Increment failed-attempt counter; lock out after MAX_ATTEMPTS."""
    with _attempts_lock:
        entry = _login_attempts.setdefault(ip, {"count": 0, "locked_until": 0})
        entry["count"] += 1
        if entry["count"] >= MAX_ATTEMPTS:
            entry["locked_until"] = time.monotonic() + LOCKOUT_SECONDS
            entry["count"] = 0   # reset so counter works after lockout expires


def _clear_attempts(ip: str):
    """Clear attempt counter on successful login."""
    with _attempts_lock:
        _login_attempts.pop(ip, None)


# ─── Auth helpers ─────────────────────────────────────────────────────────────

def is_logged_in() -> bool:
    return session.get("authenticated") is True


def login_required(f):
    """Decorator — redirects to /login if the user is not authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_logged_in():
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def api_login_required(f):
    """Decorator for API endpoints — returns 401 JSON instead of redirecting."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_logged_in():
            return jsonify({"status": "error", "msg": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ─── Flask + Socket.IO setup ──────────────────────────────────────────────────
# Flask automatically serves files from the `static/` folder at /static/<filename>
# Place style.css at: static/style.css  (same directory as this file)

socketio = SocketIO(app, async_mode="gevent", cors_allowed_origins="*",
                    ping_interval=10, ping_timeout=5)

# ─── Locker Registry ──────────────────────────────────────────────────────────
# {locker_id: {"sid": str, "last_heartbeat": float, "state": dict}}

lockers      = {}
lockers_lock = threading.Lock()

HEARTBEAT_TIMEOUT = 15.0   # seconds before a locker is marked offline


def locker_is_online(locker_id):
    entry = lockers.get(locker_id)
    if not entry:
        return False
    return (time.monotonic() - entry["last_heartbeat"]) < HEARTBEAT_TIMEOUT


def get_locker_list():
    """Return a summary list of all known lockers for the dashboard."""
    with lockers_lock:
        result = []
        for lid, data in lockers.items():
            result.append({
                "locker_id": lid,
                "online":    locker_is_online(lid),
                "state":     data.get("state", {}),
            })
        return sorted(result, key=lambda x: x["locker_id"])


# ─── Reports persistence ──────────────────────────────────────────────────────
REPORTS_FILE = os.path.join(os.path.dirname(__file__), "reports.json")


def load_reports():
    try:
        with open(REPORTS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_report(report):
    reports = load_reports()
    reports.insert(0, report)
    with open(REPORTS_FILE, "w") as f:
        json.dump(reports, f, indent=2)


# ─── Auth routes ──────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    # Already logged in — send straight to dashboard
    if is_logged_in():
        return redirect(url_for("index"))

    error   = None
    locked  = False
    wait    = 0
    ip      = request.remote_addr

    if request.method == "POST":
        allowed, wait = _check_rate_limit(ip)

        if not allowed:
            locked = True
            error  = f"Too many failed attempts. Try again in {wait}s."
        else:
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")

            if (username == ADMIN_USERNAME and
                    check_password_hash(ADMIN_PASSWORD_HASH, password)):
                # ✅ Correct credentials — create server-side session
                session.clear()
                session["authenticated"] = True
                session["username"]      = username
                session.permanent        = False   # session ends when browser closes
                _clear_attempts(ip)
                next_url = request.args.get("next") or url_for("index")
                return redirect(next_url)
            else:
                _record_failed_attempt(ip)
                _, wait = _check_rate_limit(ip)
                if wait > 0:
                    locked = True
                    error  = f"Too many failed attempts. Locked for {wait}s."
                else:
                    error  = "Invalid username or password."

    return render_template("login.html", error=error, locked=locked, wait=wait)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─── Protected HTTP routes ────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/api/lockers")
@api_login_required
def api_lockers():
    return jsonify(get_locker_list())


@app.route("/api/reports")
@api_login_required
def api_reports():
    return jsonify(load_reports())


@app.route("/api/reports/save", methods=["POST"])
@api_login_required
def api_reports_save():
    """
    Called by the browser at the moment a session ends.
    Saves the fully-populated report (with course, students, table_statuses, rows).
    If a report with the same title+course already exists it is replaced, so a
    later Pi 'report_ready' event cannot silently overwrite a richer record.
    """
    report = request.json
    if not report or not isinstance(report, dict):
        return jsonify({"status": "error", "msg": "Invalid payload"}), 400

    reports = load_reports()
    # Replace any existing record with the same title + course (dedup)
    reports = [r for r in reports
               if not (r.get("title") == report.get("title") and
                       r.get("course") == report.get("course"))]
    reports.insert(0, report)
    with open(REPORTS_FILE, "w") as f:
        json.dump(reports, f, indent=2)

    return jsonify({"status": "ok"})


@app.route("/api/reports/delete", methods=["POST"])
@api_login_required
def api_reports_delete():
    data = request.json or {}
    indices = data.get("indices", [])
    if not isinstance(indices, list):
        return jsonify({"status": "error", "msg": "indices must be a list"}), 400
    reports = load_reports()
    for i in sorted(set(indices), reverse=True):
        if 0 <= i < len(reports):
            reports.pop(i)
    with open(REPORTS_FILE, "w") as f:
        json.dump(reports, f, indent=2)
    return jsonify({"status": "ok", "remaining": len(reports)})


@app.route("/api/command/<locker_id>", methods=["POST"])
@api_login_required
def api_command(locker_id):
    with lockers_lock:
        entry = lockers.get(locker_id)
    if not entry:
        return jsonify({"status": "error", "msg": "Locker not found"}), 404
    if not locker_is_online(locker_id):
        return jsonify({"status": "error", "msg": "Locker offline"}), 503

    data = request.json or {}
    socketio.emit("command", data, room=entry["sid"])
    return jsonify({"status": "ok"})


# ─── Socket.IO — Pi ↔ Server events ──────────────────────────────────────────
# Note: Pi connections are NOT gated by the login session — they use a
# separate WebSocket connection that the browser session doesn't cover.
# If you need Pi auth later, add a shared secret token check in on_register.

@socketio.on("connect")
def on_connect():
    print(f"[Server] New connection: {request.sid}")


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    with lockers_lock:
        for lid, data in list(lockers.items()):
            if data["sid"] == sid:
                print(f"[Server] Locker '{lid}' disconnected.")
                data["last_heartbeat"] = 0.0
                break
    socketio.emit("locker_list_update", get_locker_list())


@socketio.on("register")
def on_register(payload):
    """Pi sends: {locker_id: "Locker 01"}"""
    locker_id = payload.get("locker_id", f"Unknown-{request.sid[:6]}")
    join_room(request.sid)
    with lockers_lock:
        lockers[locker_id] = {
            "sid":            request.sid,
            "last_heartbeat": time.monotonic(),
            "state":          {},
        }
    print(f"[Server] Locker '{locker_id}' registered (sid={request.sid})")
    emit("registered", {"locker_id": locker_id})
    socketio.emit("locker_list_update", get_locker_list())


@socketio.on("heartbeat")
def on_heartbeat(payload):
    locker_id = payload.get("locker_id")
    with lockers_lock:
        if locker_id in lockers:
            lockers[locker_id]["last_heartbeat"] = time.monotonic()


@socketio.on("state_update")
def on_state_update(payload):
    """Pi sends its full state dict every ~1 s."""
    locker_id = payload.get("locker_id")
    state     = payload.get("state", {})
    with lockers_lock:
        if locker_id in lockers:
            lockers[locker_id]["state"] = state
            lockers[locker_id]["last_heartbeat"] = time.monotonic()
    socketio.emit("locker_state_update", {
        "locker_id": locker_id,
        "online":    True,
        "state":     state,
    })


@socketio.on("report_ready")
def on_report_ready(payload):
    # Report saving is handled exclusively by /api/reports/save (called by the
    # browser on session end). The Pi's report_ready event is intentionally ignored
    # to avoid duplicate records.
    pass


# ─── Heartbeat watchdog ───────────────────────────────────────────────────────

def heartbeat_watchdog():
    while True:
        time.sleep(5)
        socketio.emit("locker_list_update", get_locker_list())


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    t = threading.Thread(target=heartbeat_watchdog, daemon=True)
    t.start()
    print("LabSight Central Server starting on http://0.0.0.0:5000")
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)