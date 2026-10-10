"""
Authentication routes: login, logout, forced/voluntary password change,
and the one-time first-run Super Administrator setup.

Security behaviour:
- Failed-login counting + temporary lockout; every attempt is audit-logged.
- Login errors are deliberately generic (no "no such account" vs "wrong
  password" difference) to limit account enumeration.
- Sessions carry the user's `session_version`; a password reset or
  deactivation bumps it, which logs out every other device immediately.
- Accounts flagged `must_change_password` can do nothing but change their
  password (enforced in app.py's before_request) until they do.
- Students may sign in with their admission number and parents with their
  phone number as a convenience; both just resolve to the account's email.
"""

import secrets
from datetime import datetime, timedelta

from flask import (Blueprint, abort, current_app, flash, g, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash

from database.db import get_db
from services import account_service
from utils.audit import log_action
from utils.decorators import login_required
from utils.passwords import hash_password, password_problem
from utils.validators import is_non_empty, is_valid_email

bp = Blueprint("auth", __name__, url_prefix="/auth")

ROLE_DASHBOARD = {
    "super_admin": "school_admin.dashboard",
    "temp_admin": "admin_people.students_list",
    "teacher": "teacher.dashboard",
    "student": "student.dashboard",
    "parent": "parent.dashboard",
    "attendance_operator": "teacher.dashboard",
    "auditor": "school_admin.audit_log",
}

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
GENERIC_LOGIN_ERROR = "Incorrect email or password."
LOCKED_ERROR = "Too many failed attempts. Please wait a while and try again."


def _resolve_identifier(db, identifier: str) -> str:
    """'ALI/2026/0001' or a phone number -> the matching login email."""
    identifier = identifier.strip().lower()
    if "@" in identifier or not identifier:
        return identifier
    s = account_service.slug(identifier)
    row = db.execute(
        "SELECT email FROM users WHERE email IN (?, ?) LIMIT 1",
        (f"{s}@{account_service.STUDENT_EMAIL_DOMAIN}", f"{s}@{account_service.PARENT_EMAIL_DOMAIN}"),
    ).fetchone()
    return row["email"] if row else identifier


def start_session(user):
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["sv"] = user["session_version"]


@bp.route("/login", methods=("GET", "POST"))
def login():
    if g.get("user") is not None:
        return redirect(url_for(ROLE_DASHBOARD.get(g.user["role"], "auth.login")))

    if request.method == "POST":
        raw_identifier = request.form.get("email", "")
        password = request.form.get("password", "")

        error = None
        if not is_non_empty(raw_identifier) or not is_non_empty(password):
            error = "Email and password are required."

        user = None
        db = get_db()
        email = ""
        if error is None:
            email = _resolve_identifier(db, raw_identifier)
            user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

            if user is not None and user["locked_until"]:
                if datetime.fromisoformat(user["locked_until"]) > datetime.now():
                    error = LOCKED_ERROR
                    log_action("auth.login_blocked_locked", "user", user["id"])

            if error is None:
                if user is None or not check_password_hash(user["password_hash"], password):
                    error = GENERIC_LOGIN_ERROR
                    if user is not None:
                        new_count = user["failed_login_count"] + 1
                        locked_until = None
                        if new_count >= MAX_FAILED_ATTEMPTS:
                            locked_until = (datetime.now() + timedelta(minutes=LOCKOUT_MINUTES)).isoformat(timespec="seconds")
                            error = LOCKED_ERROR
                        db.execute(
                            "UPDATE users SET failed_login_count = ?, locked_until = ? WHERE id = ?",
                            (new_count, locked_until, user["id"]),
                        )
                        db.commit()
                    # never write the attempted password; the email is what the person typed
                    log_action("auth.login_failed", "user", user["id"] if user else None, reason=f"identifier={email[:120]}")
                elif not user["is_active"]:
                    error = "This account has been deactivated. Contact the school administrator."
                elif user["role"] not in ROLE_DASHBOARD:
                    error = "This account type is no longer supported. Contact the Super Administrator."

        if error is None:
            db.execute(
                "UPDATE users SET failed_login_count = 0, locked_until = NULL, last_login_at = datetime('now') WHERE id = ?",
                (user["id"],),
            )
            db.commit()
            start_session(user)
            g.user = user
            log_action("auth.login_success", "user", user["id"])
            if user["must_change_password"]:
                flash("Welcome. For your security, please choose a new password before continuing.", "info")
                return redirect(url_for("auth.change_password"))
            flash(f"Welcome back, {user['full_name']}.", "success")
            return redirect(url_for(ROLE_DASHBOARD[user["role"]]))

        flash(error, "error")

    return render_template("auth/login.html")


@bp.route("/logout", methods=("GET", "POST"))
def logout():
    if g.get("user") is not None:
        log_action("auth.logout", "user", g.user["id"])
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/change-password", methods=("GET", "POST"))
@login_required
def change_password():
    forced = bool(g.user["must_change_password"])
    template = "parent/change_password.html" if g.user["role"] == "parent" else "auth/change_password.html"
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        problem = None
        if not check_password_hash(g.user["password_hash"], current):
            problem = "Your current (temporary) password is incorrect."
        elif new != confirm:
            problem = "New password and confirmation do not match."
        elif check_password_hash(g.user["password_hash"], new):
            problem = "Your new password must be different from the current one."
        else:
            problem = password_problem(new, g.user["email"], g.user["full_name"])
        if problem:
            flash(problem, "error")
            return render_template(template, forced=forced)

        db = get_db()
        session["sv"] = account_service.set_own_password(db, g.user["id"], new)
        db.commit()
        log_action("auth.password_changed", "user", g.user["id"])
        flash("Password changed successfully.", "success")
        return redirect(url_for(ROLE_DASHBOARD[g.user["role"]]))

    return render_template(template, forced=forced)


# --------------------------------------------------------- first-run setup
def _setup_available(db) -> bool:
    token = current_app.config.get("SETUP_TOKEN")
    if not token:
        return False
    return db.execute("SELECT 1 FROM users WHERE role = 'super_admin' LIMIT 1").fetchone() is None


@bp.route("/setup", methods=("GET", "POST"))
def setup():
    """One-time creation of the first Super Administrator.

    Available ONLY while no Super Administrator exists AND the deployer has
    set a SETUP_TOKEN environment variable (so a stranger who finds the URL
    first cannot claim the system). Disappears (404) once an account exists."""
    db = get_db()
    if not _setup_available(db):
        abort(404)

    if request.method == "POST":
        token = request.form.get("setup_token", "")
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        problem = None
        if not secrets.compare_digest(token.encode(), current_app.config["SETUP_TOKEN"].encode()):
            problem = "The setup token is incorrect."
        elif not full_name or not is_valid_email(email):
            problem = "A name and a valid email are required."
        elif password != confirm:
            problem = "Password and confirmation do not match."
        else:
            problem = password_problem(password, email, full_name)
        if problem:
            log_action("auth.setup_failed", "user", None)
            flash(problem, "error")
            return render_template("auth/setup.html")

        cur = db.execute(
            "INSERT INTO users (email, password_hash, role, full_name, must_change_password) VALUES (?, ?, 'super_admin', ?, 0)",
            (email, hash_password(password), full_name),
        )
        db.commit()
        log_action("auth.setup_super_admin_created", "user", cur.lastrowid)
        flash("Super Administrator created. Please sign in. You can now remove the SETUP_TOKEN variable.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/setup.html")
