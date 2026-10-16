"""
Offline face recognition endpoints (Stage 2 / Stage 3).

  GET  /offline-api/face-templates?class_id=N   templates for ONE class (assigned teacher only)
  GET  /offline-api/enrollment-bootstrap        student list for offline enrollment (Super Administrator)
  POST /offline-api/enrollments                 idempotent upload of one offline enrollment

  /admin/face-templates                         review, deactivate or delete stored templates

Biometric templates are sensitive: every download and enrolment is audit-logged,
responses are never cached, and nothing here contains a photograph.
"""

from flask import (Blueprint, current_app, flash, g, jsonify, redirect, render_template,
                    request, url_for)

from database.db import get_db
from routes.offline import DEVICE_RE, ENROLL_ROLES, OFFLINE_APP_VERSION, _json, _rate_limited, api_auth, api_roles
from services import face_offline_service as fos
from services import settings_service
from services.sync_service import PayloadError
from utils.audit import log_action
from utils.decorators import role_required

bp = Blueprint("offline_face", __name__)


def _fc():
    return fos.get_config(current_app.config)


def _not_configured():
    return _json({"ok": False, "code": "face_not_configured",
                  "message": "Offline face recognition is not enabled on this server."}, 409)


@bp.route("/offline-api/face-templates")
@api_auth
def face_templates():
    fc = _fc()
    if not fc["enabled"]:
        return _not_configured()
    try:
        class_id = int(request.args.get("class_id", ""))
    except ValueError:
        return _json({"ok": False, "code": "invalid", "message": "class_id is required."}, 400)
    db = get_db()
    if not fos.teacher_owns_class(db, g.user, class_id):
        log_action("face.templates_denied", "class", class_id)
        return _json({"ok": False, "code": "not_assigned",
                      "message": "You are not assigned to this class."}, 403)
    templates, missing = fos.templates_for_class(db, class_id, fc)
    log_action("face.templates_download", "class", class_id, new_value={"count": len(templates)})
    from datetime import datetime, timezone
    return _json({"ok": True, "class_id": class_id, "engine": fc["engine"], "engine_version": fc["engine_version"],
                  "dim": fc["dim"], "min_similarity": fc["min_similarity"], "margin": fc["margin"],
                  "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "templates": templates, "missing_student_ids": missing})


@bp.route("/offline-api/enrollment-bootstrap")
@api_roles(*ENROLL_ROLES)
def enrollment_bootstrap():
    fc = _fc()
    if not fc["enabled"]:
        return _not_configured()
    grace = int(current_app.config.get("OFFLINE_GRACE_HOURS", 72))
    return _json(fos.enrollment_bootstrap(get_db(), settings_service.get_settings(), fc, grace,
                                          OFFLINE_APP_VERSION, g.user))


@bp.route("/offline-api/enrollments", methods=("POST",))
@api_roles(*ENROLL_ROLES)
def enrollments():
    if _rate_limited(g.user["id"]):
        return _json({"ok": False, "code": "rate_limited", "message": "Too many requests."}, 429)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _json({"ok": False, "code": "invalid", "message": "Invalid request."}, 400)
    device_uid = body.get("device_uid")
    if not isinstance(device_uid, str) or not DEVICE_RE.match(device_uid):
        return _json({"ok": False, "code": "invalid", "message": "Device id is invalid."}, 400)
    fc = _fc()
    try:
        clean = fos.validate_enrollment(body.get("enrollment"), fc)
        out = fos.enroll(get_db(), g.user, device_uid, clean, fc)
    except PayloadError as exc:
        return _json({"ok": False, "code": "invalid", "message": str(exc)}, 400)
    return _json({"ok": True, "enrollment": {"client_uid": clean["client_uid"], **out}})


# ----------------------------------------------------------------- admin page
@bp.route("/admin/face-templates")
@role_required("super_admin")
def templates_admin():
    db = get_db()
    q = request.args.get("q", "").strip()
    where, params = "", []
    if q:
        where = "WHERE st.first_name LIKE ? OR st.surname LIKE ? OR st.admission_number LIKE ?"
        params = [f"%{q}%"] * 3
    rows = db.execute(
        f"""SELECT ft.id, ft.engine, ft.engine_version, ft.sample_count, ft.quality_score, ft.source, ft.device_uid,
                   ft.is_active, ft.created_at, ft.deactivated_at, ft.consent_confirmed, ft.consent_note,
                   st.first_name, st.surname, st.admission_number, st.status AS student_status
            FROM face_templates ft JOIN students st ON st.id = ft.student_id {where}
            ORDER BY ft.id DESC LIMIT 300""", params).fetchall()
    return render_template("admin/face_templates.html", rows=rows, q=q, cfg=_fc())


@bp.route("/admin/face-templates/<int:template_id>/<action>", methods=("POST",))
@role_required("super_admin")
def template_action(template_id, action):
    db = get_db()
    if action == "deactivate":
        ok = fos.deactivate_template(db, template_id, g.user["id"])
        flash("Template deactivated." if ok else "Nothing to deactivate.", "success" if ok else "error")
    elif action == "delete":
        ok = fos.delete_template(db, template_id, g.user["id"])
        flash("Template deleted permanently." if ok else "Template not found.", "success" if ok else "error")
    else:
        flash("Unknown action.", "error")
    return redirect(url_for("offline_face.templates_admin"))
