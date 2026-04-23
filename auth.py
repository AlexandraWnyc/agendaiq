"""
auth.py — Authentication and multi-tenancy middleware for AgendaIQ

Provides:
  - User model compatible with Flask-Login
  - Password hashing via bcrypt
  - Login/logout/register routes
  - org_id injection into Flask's g object for every request
  - login_required decorator on API routes
  - Seed admin helper for first-time setup

Every authenticated request sets:
  g.user    — the User object (id, username, email, role, org_id, display_name)
  g.org_id  — shortcut to g.user.org_id (used by all query functions)
"""

import logging
from datetime import datetime as dt
from functools import wraps

import bcrypt
from flask import (
    Blueprint, request, jsonify, redirect, url_for, g, session,
    render_template_string, flash, current_app
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required as _flask_login_required, current_user
)

from db import get_db

log = logging.getLogger("oca-agent")

# ── Flask-Login setup ─────────────────────────────────────────

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Please log in to access AgendaIQ."
login_manager.login_message_category = "info"


class User(UserMixin):
    """Lightweight user object for Flask-Login sessions."""

    def __init__(self, id, org_id, username, email, display_name, role, is_active):
        self.id = id
        self.org_id = org_id
        self.username = username
        self.email = email
        self.display_name = display_name or username
        self.role = role
        self._is_active = is_active

    @property
    def is_active(self):
        return bool(self._is_active)

    def get_id(self):
        return str(self.id)

    def to_dict(self):
        return {
            "id": self.id,
            "org_id": self.org_id,
            "username": self.username,
            "email": self.email,
            "display_name": self.display_name,
            "role": self.role,
        }


@login_manager.user_loader
def _load_user(user_id):
    """Called by Flask-Login on every request to hydrate current_user."""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT id, org_id, username, email, display_name, role, is_active "
                "FROM users WHERE id=? AND is_active=1", (int(user_id),)
            ).fetchone()
        if row:
            return User(**dict(row))
    except Exception as e:
        log.warning(f"User loader failed for id={user_id}: {e}")
    return None


# ── Password helpers ──────────────────────────────────────────

def hash_password(plain: str) -> str:
    """Hash a plaintext password with bcrypt."""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def check_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


# ── User CRUD ─────────────────────────────────────────────────

def create_user(org_id: int, username: str, email: str, password: str,
                display_name: str = None, role: str = "analyst") -> int:
    """Create a new user. Returns the user id. Raises on duplicate email."""
    now = dt.utcnow().isoformat()
    pw_hash = hash_password(password)
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO users (org_id, username, email, password_hash,
               display_name, role, is_active, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (org_id, username, email.lower().strip(), pw_hash,
             display_name or username, role, now, now)
        )
        return cur.lastrowid


def get_user_by_email(email: str):
    """Look up a user by email. Returns dict or None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email=? AND is_active=1",
            (email.lower().strip(),)
        ).fetchone()
    return dict(row) if row else None


def get_user_count(org_id: int = None) -> int:
    """Count active users, optionally scoped to an org."""
    with get_db() as conn:
        if org_id:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM users WHERE org_id=? AND is_active=1",
                (org_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM users WHERE is_active=1"
            ).fetchone()
    return row["cnt"] if row else 0


# ── Org-id middleware ─────────────────────────────────────────

def inject_org_context():
    """Before-request hook: set g.org_id and g.user from the session.
    Call this in your app's before_request or register via init_auth()."""
    if current_user.is_authenticated:
        g.user = current_user
        g.org_id = current_user.org_id
    else:
        g.user = None
        g.org_id = None


# ── Blueprint with login/logout/register routes ──────────────

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect("/")

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        user_row = get_user_by_email(email)
        if user_row and check_password(password, user_row["password_hash"]):
            user = User(
                id=user_row["id"],
                org_id=user_row["org_id"],
                username=user_row["username"],
                email=user_row["email"],
                display_name=user_row["display_name"],
                role=user_row["role"],
                is_active=user_row["is_active"],
            )
            login_user(user, remember=True)
            # Update last_login_at
            try:
                with get_db() as conn:
                    conn.execute(
                        "UPDATE users SET last_login_at=? WHERE id=?",
                        (dt.utcnow().isoformat(), user.id)
                    )
            except Exception:
                pass
            log.info(f"User logged in: {email} (org_id={user.org_id})")
            next_page = request.args.get("next") or "/"
            return redirect(next_page)
        else:
            return render_template_string(
                LOGIN_HTML, error="Invalid email or password.", email=email
            )

    return render_template_string(LOGIN_HTML, error=None, email="")


@auth_bp.route("/logout")
def logout():
    logout_user()
    return redirect("/login")


@auth_bp.route("/setup", methods=["GET", "POST"])
def setup():
    """First-time setup: create the initial admin user.
    Only accessible when no users exist in the database."""
    if get_user_count() > 0:
        return redirect("/login")

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""

        errors = []
        if not name:
            errors.append("Name is required.")
        if not email or "@" not in email:
            errors.append("Valid email is required.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")

        if errors:
            return render_template_string(
                SETUP_HTML, errors=errors, name=name, email=email
            )

        try:
            user_id = create_user(
                org_id=1, username=name, email=email,
                password=password, display_name=name, role="admin"
            )
            log.info(f"Setup complete: admin user created (id={user_id}, email={email})")
            # Auto-login
            user_row = get_user_by_email(email)
            if user_row:
                user = User(**{k: user_row[k] for k in
                             ["id", "org_id", "username", "email",
                              "display_name", "role", "is_active"]})
                login_user(user, remember=True)
            return redirect("/")
        except Exception as e:
            return render_template_string(
                SETUP_HTML, errors=[str(e)], name=name, email=email
            )

    return render_template_string(SETUP_HTML, errors=[], name="", email="")


@auth_bp.route("/api/auth/me")
def api_me():
    """Return current user info (for JS to know who's logged in)."""
    if current_user.is_authenticated:
        return jsonify({"ok": True, "user": current_user.to_dict()})
    return jsonify({"ok": False, "user": None}), 401


# ── App initialization helper ─────────────────────────────────

def init_auth(app):
    """Wire auth into the Flask app. Call once at startup."""
    # Secret key for sessions — use env var in production
    import os
    app.secret_key = os.environ.get("SECRET_KEY", "agendaiq-dev-secret-change-me")

    login_manager.init_app(app)
    app.register_blueprint(auth_bp)
    app.before_request(inject_org_context)

    # Redirect to setup if no users exist
    @app.before_request
    def _check_first_run():
        if request.endpoint in ("auth.setup", "auth.login", "auth.logout",
                                "static", "api_healthz"):
            return
        if request.path.startswith("/static"):
            return
        try:
            if get_user_count() == 0 and request.endpoint != "auth.setup":
                return redirect("/setup")
        except Exception:
            pass  # Table might not exist yet during init

    # API routes return 401 JSON instead of redirect
    @login_manager.unauthorized_handler
    def _unauthorized():
        if request.path.startswith("/api/"):
            return jsonify({"error": "Authentication required"}), 401
        return redirect(url_for("auth.login", next=request.url))


# ── HTML Templates ────────────────────────────────────────────
# Minimal inline templates — styled to match AgendaIQ's look.

LOGIN_HTML = """<!DOCTYPE html>
<html><head>
<title>AgendaIQ — Login</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #f1f5f9; display: flex; align-items: center; justify-content: center;
         min-height: 100vh; }
  .card { background: #fff; border-radius: 12px; box-shadow: 0 4px 24px rgba(0,0,0,.08);
          padding: 2.5rem; width: 100%; max-width: 420px; }
  .logo { font-size: 1.6rem; font-weight: 800; color: #1e40af; text-align: center;
          margin-bottom: .3rem; letter-spacing: -.5px; }
  .sub { text-align: center; color: #64748b; font-size: .85rem; margin-bottom: 1.8rem; }
  label { display: block; font-size: .82rem; font-weight: 600; color: #334155;
          margin-bottom: .3rem; margin-top: 1rem; }
  input[type=email], input[type=password], input[type=text] {
    width: 100%; padding: .65rem .85rem; border: 1px solid #cbd5e1; border-radius: 8px;
    font-size: .9rem; outline: none; transition: border .2s; }
  input:focus { border-color: #3b82f6; box-shadow: 0 0 0 3px rgba(59,130,246,.15); }
  .btn { width: 100%; padding: .75rem; border: none; border-radius: 8px; font-size: .95rem;
         font-weight: 700; color: #fff; background: #1e40af; cursor: pointer;
         margin-top: 1.5rem; transition: background .2s; }
  .btn:hover { background: #1e3a8a; }
  .error { background: #fef2f2; color: #dc2626; padding: .6rem .85rem; border-radius: 8px;
           font-size: .82rem; margin-bottom: 1rem; border: 1px solid #fecaca; }
</style>
</head><body>
<div class="card">
  <div class="logo">AgendaIQ</div>
  <div class="sub">Sign in to your account</div>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="POST">
    <label for="email">Email</label>
    <input type="email" id="email" name="email" value="{{ email }}" required autofocus>
    <label for="password">Password</label>
    <input type="password" id="password" name="password" required>
    <button type="submit" class="btn">Sign In</button>
  </form>
</div>
</body></html>"""

SETUP_HTML = """<!DOCTYPE html>
<html><head>
<title>AgendaIQ — Setup</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #f1f5f9; display: flex; align-items: center; justify-content: center;
         min-height: 100vh; }
  .card { background: #fff; border-radius: 12px; box-shadow: 0 4px 24px rgba(0,0,0,.08);
          padding: 2.5rem; width: 100%; max-width: 460px; }
  .logo { font-size: 1.6rem; font-weight: 800; color: #1e40af; text-align: center;
          margin-bottom: .3rem; letter-spacing: -.5px; }
  .sub { text-align: center; color: #64748b; font-size: .85rem; margin-bottom: .5rem; }
  .note { text-align: center; color: #475569; font-size: .78rem; margin-bottom: 1.5rem;
          background: #f0f9ff; padding: .6rem; border-radius: 8px; border: 1px solid #bae6fd; }
  label { display: block; font-size: .82rem; font-weight: 600; color: #334155;
          margin-bottom: .3rem; margin-top: 1rem; }
  input[type=email], input[type=password], input[type=text] {
    width: 100%; padding: .65rem .85rem; border: 1px solid #cbd5e1; border-radius: 8px;
    font-size: .9rem; outline: none; transition: border .2s; }
  input:focus { border-color: #3b82f6; box-shadow: 0 0 0 3px rgba(59,130,246,.15); }
  .btn { width: 100%; padding: .75rem; border: none; border-radius: 8px; font-size: .95rem;
         font-weight: 700; color: #fff; background: #059669; cursor: pointer;
         margin-top: 1.5rem; transition: background .2s; }
  .btn:hover { background: #047857; }
  .error { background: #fef2f2; color: #dc2626; padding: .6rem .85rem; border-radius: 8px;
           font-size: .82rem; margin-bottom: 1rem; border: 1px solid #fecaca; }
</style>
</head><body>
<div class="card">
  <div class="logo">AgendaIQ</div>
  <div class="sub">Welcome! Let's set up your admin account.</div>
  <div class="note">This is a one-time setup. You'll be the first admin user.</div>
  {% if errors %}
    <div class="error">{% for e in errors %}{{ e }}<br>{% endfor %}</div>
  {% endif %}
  <form method="POST">
    <label for="name">Your Name</label>
    <input type="text" id="name" name="name" value="{{ name }}" required autofocus>
    <label for="email">Email</label>
    <input type="email" id="email" name="email" value="{{ email }}" required>
    <label for="password">Password (8+ characters)</label>
    <input type="password" id="password" name="password" required minlength="8">
    <label for="confirm">Confirm Password</label>
    <input type="password" id="confirm" name="confirm" required>
    <button type="submit" class="btn">Create Admin Account</button>
  </form>
</div>
</body></html>"""
