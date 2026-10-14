"""
Application factory — AL-ISTI'ANAH ACADEMY DAMATURU attendance system.

Local development:
    flask --app app db-migrate          # create/upgrade tables (SQLite file)
    python seed.py                      # OPTIONAL dev data (never use in production)
    flask --app app run --debug

Production (Railway): gunicorn "app:create_app()"  — see DEPLOY_RAILWAY.md.
The app NEVER creates, resets or migrates the database on startup.
"""

import logging
import os
import sys

from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from config import get_config
from database import db as database
from database.db import DatabaseUnavailable
from routes.auth import ROLE_DASHBOARD
from utils.csrf import csrf_protect, get_csrf_token

# Endpoints reachable while a forced password change is pending
_PASSWORD_CHANGE_ALLOWED = {"auth.change_password", "auth.logout", "static", "healthz", "readyz"}


def create_app():
    app = Flask(__name__)
    config_cls = get_config()
    config_cls.validate()                      # production refuses to boot with unsafe settings
    app.config.from_object(config_cls)

    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not app.config.get("TESTING") and not app.config.get("DEBUG"):
        # Railway terminates TLS in front of the app; trust one proxy hop for scheme/client IP
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    for path in (app.config["FACE_DATA_DIR"], app.config["BACKUP_DIR"]):
        os.makedirs(path, exist_ok=True)

    database.init_app(app)

    # ---- Load the current user onto g for every request ----
    @app.before_request
    def load_logged_in_user():
        g.user = None
        if request.endpoint in ("static", "healthz"):
            return
        user_id = session.get("user_id")
        if user_id is None:
            return
        user = database.get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        # A reset/deactivation bumps session_version, instantly ending older sessions.
        if user is None or not user["is_active"] or session.get("sv") != user["session_version"]:
            session.clear()
            return
        g.user = user

    @app.before_request
    def enforce_password_change():
        user = g.get("user")
        if (user is not None and user["must_change_password"]
                and request.endpoint not in _PASSWORD_CHANGE_ALLOWED
                and not request.path.startswith("/api/")):
            return redirect(url_for("auth.change_password"))

    # ---- CSRF protection for all state-changing requests (API excluded: device-key auth) ----
    @app.before_request
    def enforce_csrf():
        if request.path.startswith("/api/") or request.endpoint in ("healthz", "readyz"):
            return
        if not csrf_protect():
            abort(400, description="Invalid or missing CSRF token.")

    app.jinja_env.globals["csrf_token"] = get_csrf_token
    from utils.permissions import can
    app.jinja_env.globals["can"] = can

    # ---- Branding available to every template without per-view plumbing ----
    @app.context_processor
    def inject_branding():
        from services.settings_service import get_settings
        try:
            settings = get_settings()
            return {
                "school_settings": settings,
                "storage_warning": (not app.config["STORAGE_PERSISTENT"]) and not app.config["DEBUG"],
            }
        except Exception:
            return {}

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
        if not app.config["DEBUG"] and not app.config.get("TESTING"):
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        # Pages for signed-in users (and anything showing a temporary password) must never be cached
        if request.endpoint != "static" and (g.get("user") is not None or request.path.startswith("/auth")):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---- Register blueprints ----
    from routes import (admin_accounts, admin_biometrics, admin_devices, admin_ops,
                         admin_manage, admin_people, api, auth, parent, reports, school_admin,
                         student, teacher)

    app.register_blueprint(auth.bp)
    app.register_blueprint(school_admin.bp)
    app.register_blueprint(admin_people.bp)
    app.register_blueprint(admin_manage.bp)
    app.register_blueprint(admin_accounts.bp)
    app.register_blueprint(admin_devices.bp)
    app.register_blueprint(admin_biometrics.bp)
    app.register_blueprint(admin_ops.bp)
    app.register_blueprint(teacher.bp)
    app.register_blueprint(student.bp)
    app.register_blueprint(parent.bp)
    app.register_blueprint(reports.bp)
    app.register_blueprint(api.bp)

    # ---- Root redirect + health checks ----
    @app.route("/")
    def index():
        if g.user is None:
            return redirect(url_for("auth.login"))
        return redirect(url_for(ROLE_DASHBOARD.get(g.user["role"], "auth.login")))

    @app.route("/healthz")
    def healthz():
        """Liveness: the process is up (no database call). Used by Railway's healthcheck."""
        return jsonify(status="ok")

    @app.route("/readyz")
    def readyz():
        """Readiness: can we reach the database?"""
        try:
            database.get_db().execute("SELECT 1").fetchone()
        except Exception:
            return jsonify(status="database unavailable"), 503
        return jsonify(status="ready")

    # ---- Error handlers: never leak stack traces or credentials ----
    @app.errorhandler(400)
    def bad_request(e):
        return render_template("errors/400.html", description=getattr(e, "description", None)), 400

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(413)
    def too_large(e):
        return render_template("errors/400.html", description="That upload is too large."), 413

    @app.errorhandler(429)
    def too_many_requests(e):
        return render_template("errors/429.html"), 429

    @app.errorhandler(500)
    def server_error(e):
        return render_template("errors/500.html"), 500

    @app.errorhandler(DatabaseUnavailable)
    def database_unavailable(e):
        app.logger.error("Database unavailable: %s", type(e).__name__)  # message may hold host info
        return render_template("errors/503.html"), 503

    # ---- CLI commands ----
    @app.cli.command("db-migrate")
    def db_migrate_command():
        """Apply pending schema migrations. Safe to run repeatedly; never deletes data."""
        with app.app_context():
            database.init_db()

    @app.cli.command("init-db")
    def init_db_command():
        """Alias of db-migrate (kept for the old workflow)."""
        with app.app_context():
            database.init_db()

    @app.cli.command("create-super-admin")
    def create_super_admin_command():
        """Interactively create the first Super Administrator (no default password exists)."""
        import click
        from utils.passwords import hash_password, password_problem
        from utils.validators import is_valid_email
        with app.app_context():
            db = database.get_db()
            if db.execute("SELECT 1 FROM users WHERE role = 'super_admin' LIMIT 1").fetchone():
                raise click.ClickException("A Super Administrator already exists.")
            name = click.prompt("Full name").strip()
            email = click.prompt("Email (used to sign in)").strip().lower()
            if not is_valid_email(email):
                raise click.ClickException("That is not a valid email address.")
            password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
            problem = password_problem(password, email, name)
            if problem:
                raise click.ClickException(problem)
            db.execute(
                "INSERT INTO users (email, password_hash, role, full_name, must_change_password) "
                "VALUES (?, ?, 'super_admin', ?, 0)", (email, hash_password(password), name))
            db.commit()
            click.echo("Super Administrator created.")

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=app.config.get("DEBUG", True))
