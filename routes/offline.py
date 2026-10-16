"""
Offline support routes (Stage 1).

Public, data-free PWA assets
    /offline-app/          static application shell (identical for everyone, no user data,
                           no CSRF token, so it is safe to keep in the browser's shell cache)
    /sw.js                 service worker (served from the site root so it can control all pages)
    /manifest.webmanifest  install manifest

Authenticated JSON API (session cookie + X-CSRF-Token, teacher roles only)
    GET  /offline-api/ping        lightweight health + auth probe used by the status indicator
    GET  /offline-api/bootstrap   the minimum class-scoped data a phone may keep offline
    POST /offline-api/sync        idempotent upload of ONE offline attendance session

Administrator review
    /admin/sync-conflicts         conflicts and rejections that were never silently dropped

The /offline-api/ prefix is deliberately NOT under /api/ so the existing CSRF
protection applies to it.
"""

import json
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from functools import wraps

from flask import (Blueprint, Response, current_app, flash, g, jsonify, redirect,
                    render_template, request, url_for)

from database.db import get_db
from services import face_offline_service, settings_service, sync_service
from services.sync_service import PayloadError
from utils.csrf import get_csrf_token
from utils.decorators import role_required

bp = Blueprint("offline", __name__)

# Bump when ANY file in the shell precache list changes. A new value makes
# browsers fetch the new shell; it never touches saved attendance (IndexedDB).
OFFLINE_APP_VERSION = "1.1.0"
TEACHER_ROLES = ("teacher", "attendance_operator", "super_admin")
DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

_SYNC_WINDOW_SECONDS = 60
_SYNC_MAX_PER_WINDOW = 90
_sync_hits = defaultdict(deque)


def _rate_limited(user_id):
    """Small in-process limiter. Each worker keeps its own counter, so it is a
    safety net against runaway clients, not a security boundary."""
    now = time.monotonic()
    hits = _sync_hits[user_id]
    while hits and now - hits[0] > _SYNC_WINDOW_SECONDS:
        hits.popleft()
    if len(hits) >= _SYNC_MAX_PER_WINDOW:
        return True
    hits.append(now)
    return False


def _json(data, status=200):
    resp = jsonify(data)
    resp.status_code = status
    resp.headers["Cache-Control"] = "no-store"
    return resp


def api_roles(*roles):
    """JSON equivalent of role_required: never redirects, so the offline
    client can tell 'sign in again' apart from a network failure."""
    def deco(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = g.get("user")
            if user is None:
                return _json({"ok": False, "code": "auth_required",
                              "message": "Please sign in again to continue."}, 401)
            if user["role"] not in roles:
                return _json({"ok": False, "code": "forbidden",
                              "message": "This account cannot use this offline feature."}, 403)
            if user["must_change_password"]:
                return _json({"ok": False, "code": "password_change_required",
                              "message": "Change your password before using offline features."}, 403)
            return view(*args, **kwargs)
        return wrapped
    return deco


api_auth = api_roles(*TEACHER_ROLES)
ENROLL_ROLES = ("super_admin",)       # same as the existing face-enrollment pages


# ------------------------------------------------------------- PWA assets
@bp.route("/offline-app/")
def shell():
    resp = Response(render_template("offline/app.html", app_version=OFFLINE_APP_VERSION))
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.route("/sw.js")
def service_worker():
    path = os.path.join(current_app.static_folder, "sw.js")
    with open(path, "r", encoding="utf-8") as f:
        body = f.read().replace("__APP_VERSION__", OFFLINE_APP_VERSION)
    resp = Response(body, mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.route("/manifest.webmanifest")
def manifest():
    data = {
        "name": "AL-ISTI'ANAH Attendance",
        "short_name": "Attendance",
        "description": "School attendance that keeps working without internet.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#f4f6fa",
        "theme_color": "#0b3d5c",
        "icons": [
            {"src": url_for("static", filename="icons/icon-192.png"), "sizes": "192x192", "type": "image/png"},
            {"src": url_for("static", filename="icons/icon-512.png"), "sizes": "512x512", "type": "image/png"},
            {"src": url_for("static", filename="icons/icon-maskable-512.png"), "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
    }
    resp = Response(json.dumps(data), mimetype="application/manifest+json")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# --------------------------------------------------------------------- API
@bp.route("/offline-api/ping")
def ping():
    """Cheap probe. Succeeding means the server AND its database answered.
    Works signed-out (authenticated=false) so the client can tell
    'server unreachable' from 'session expired'."""
    try:
        get_db().execute("SELECT 1").fetchone()
    except Exception:
        return _json({"ok": False, "code": "database_unavailable"}, 503)
    user = g.get("user")
    data = {"ok": True, "app_version": OFFLINE_APP_VERSION,
            "server_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "authenticated": user is not None}
    if user is not None:
        data.update({"user_id": user["id"], "role": user["role"],
                     "offline_capable": user["role"] in TEACHER_ROLES
                                        and not user["must_change_password"],
                     "enroll_capable": user["role"] in ENROLL_ROLES and not user["must_change_password"],
                     "csrf_token": get_csrf_token()})
    return _json(data)


@bp.route("/offline-api/bootstrap")
@api_auth
def bootstrap():
    grace = int(current_app.config.get("OFFLINE_GRACE_HOURS", 72))
    fc = face_offline_service.get_config(current_app.config)
    data = sync_service.bootstrap_for_teacher(
        get_db(), g.user, settings_service.get_settings(), grace, OFFLINE_APP_VERSION,
        face_public=face_offline_service.public_config(fc), face_cfg=fc)
    return _json(data)


@bp.route("/offline-api/sync", methods=("POST",))
@api_auth
def sync():
    if _rate_limited(g.user["id"]):
        return _json({"ok": False, "code": "rate_limited",
                      "message": "Too many requests. Retrying automatically."}, 429)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _json({"ok": False, "code": "invalid", "message": "Invalid request."}, 400)
    device_uid = body.get("device_uid")
    if not isinstance(device_uid, str) or not DEVICE_RE.match(device_uid):
        return _json({"ok": False, "code": "invalid", "message": "Device id is invalid."}, 400)
    settings = settings_service.get_settings()
    try:
        clean = sync_service.validate_session_payload(body.get("session"), settings["timezone"])
        result = sync_service.sync_session(
            get_db(), g.user, device_uid, clean, (request.headers.get("User-Agent") or "")[:200],
            face_cfg=face_offline_service.get_config(current_app.config))
    except PayloadError as exc:
        return _json({"ok": False, "code": "invalid", "message": str(exc)}, 400)
    result["server_time"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return _json(result)


# ------------------------------------------------- administrator conflicts
REASON_TEXT = {
    "manual_cannot_override_automatic": "A manual mark cannot replace an automatic (face/QR) record.",
    "conflicting_marks": "Two different marks were recorded for the same student and session.",
    "replaced_automatic_absence": "Replaced the automatic absence written when the session closed.",
    "student_not_in_class": "The student is no longer in this class (or is inactive).",
    "assignment_not_allowed": "The teacher is not (or is no longer) assigned to this class/subject.",
    "no_teacher_profile": "The account has no teacher profile.",
    "already_enrolled": "This student already has an active face template. A second enrollment was not applied.",
    "below_threshold": "The recorded face-match confidence is below the server's minimum.",
    "unknown_template": "The face match refers to a template the server does not have for this student.",
    "engine_mismatch": "The phone used a different face-recognition model version than the server accepts.",
    "face_not_enabled": "Offline face recognition is not enabled on the server.",
    "wrong_dimension": "The face template has the wrong size.",
    "invalid_template": "The face template is not valid.",
    "not_normalised": "The face template is not a normalised vector.",
    "too_few_samples": "Too few samples were captured for a reliable template.",
    "student_not_active": "The student is not active.",
}


@bp.route("/admin/sync-conflicts")
@role_required("super_admin")
def conflicts():
    db = get_db()
    show = request.args.get("show", "open")
    where = "WHERE cf.resolution = 'needs_review'" if show != "all" else ""
    rows = db.execute(
        f"""SELECT cf.*, st.admission_number, st.first_name, st.surname,
                   s.session_code, u.full_name AS teacher_name
            FROM sync_conflicts cf
            LEFT JOIN students st ON st.id = cf.student_id
            LEFT JOIN attendance_sessions s ON s.id = cf.session_id
            LEFT JOIN users u ON u.id = cf.user_id
            {where} ORDER BY cf.id DESC LIMIT 200""").fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["incoming"] = json.loads(r["incoming_json"]) if r["incoming_json"] else {}
        d["existing"] = json.loads(r["existing_json"]) if r["existing_json"] else None
        d["reason_text"] = REASON_TEXT.get(r["reason"], r["reason"])
        items.append(d)
    return render_template("admin/sync_conflicts.html", items=items, show=show)


@bp.route("/admin/sync-conflicts/<int:conflict_id>/resolve", methods=("POST",))
@role_required("super_admin")
def conflict_resolve(conflict_id):
    action = request.form.get("action", "")
    note = (request.form.get("note") or "").strip()[:300] or None
    ok = sync_service.resolve_conflict(get_db(), conflict_id, g.user["id"], action, note)
    flash("Conflict updated." if ok else "That conflict could not be updated (already reviewed, "
          "or the action is not possible).", "success" if ok else "error")
    return redirect(url_for("offline.conflicts"))
