"""
Operational admin pages that don't fit the core academic CRUD:
offline sync review, the notification queue, and manual backups.

Honest scope note: this is the review/trigger UI over real tables
(offline_sync_events, notifications) and a real backup file copy —
it is NOT automatic conflict resolution, a live SMS/email/WhatsApp
provider, or scheduled off-server backup. Those need real
credentials/infrastructure the packaged app can't supply on its own
(see README).
"""

import os
import shutil
from datetime import datetime

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, send_from_directory, url_for

from database.db import get_db
from services.attendance_service import record_attendance
from utils.audit import log_action
from utils.decorators import role_required

bp = Blueprint("admin_ops", __name__, url_prefix="/admin")
ADMIN_ROLES = ("super_admin",)   # only the Super Administrator is unrestricted


# --------------------------------------------------------------- offline sync
@bp.route("/sync-review")
@role_required(*ADMIN_ROLES)
def sync_review():
    db = get_db()
    rows = db.execute(
        """SELECT ev.*, d.name AS device_name FROM offline_sync_events ev
           LEFT JOIN devices d ON d.id = ev.device_id
           ORDER BY ev.received_at DESC LIMIT 200"""
    ).fetchall()
    return render_template("admin/sync_review.html", rows=rows)


@bp.route("/sync-review/<int:event_id>/apply", methods=("POST",))
@role_required(*ADMIN_ROLES)
def sync_apply(event_id):
    """Manually apply one queued offline event as an attendance record
    via the same validation chain as any other check-in."""
    import json
    db = get_db()
    event = db.execute("SELECT * FROM offline_sync_events WHERE id = ?", (event_id,)).fetchone()
    if event is None or event["status"] != "pending":
        flash("Event not found or already resolved.", "error")
        return redirect(url_for("admin_ops.sync_review"))

    payload = json.loads(event["payload_json"] or "{}")
    result = record_attendance(
        db, event["session_id"], event["student_id"], "offline_sync",
        confidence=payload.get("confidence"), device_id=event["device_id"],
    )
    new_status = "synced" if result.ok else "conflict"
    db.execute("UPDATE offline_sync_events SET status = ?, resolution_note = ? WHERE id = ?",
               (new_status, result.message, event_id))
    db.commit()
    log_action("offline_sync.apply", "offline_sync_event", event_id, new_value={"status": new_status})
    flash(f"Event {new_status}: {result.message}", "success" if result.ok else "warning")
    return redirect(url_for("admin_ops.sync_review"))


@bp.route("/sync-review/<int:event_id>/reject", methods=("POST",))
@role_required(*ADMIN_ROLES)
def sync_reject(event_id):
    db = get_db()
    db.execute("UPDATE offline_sync_events SET status = 'rejected', resolution_note = ? WHERE id = ?",
               (request.form.get("reason", "").strip() or "Rejected by admin", event_id))
    db.commit()
    log_action("offline_sync.reject", "offline_sync_event", event_id)
    flash("Event rejected.", "info")
    return redirect(url_for("admin_ops.sync_review"))


# --------------------------------------------------------------- notifications
@bp.route("/notifications")
@role_required(*ADMIN_ROLES)
def notifications_queue():
    db = get_db()
    rows = db.execute("SELECT * FROM notifications ORDER BY created_at DESC LIMIT 200").fetchall()
    return render_template("admin/notifications_queue.html", rows=rows)


# --------------------------------------------------------------------- backup
@bp.route("/backup", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def backup():
    """PostgreSQL backups are taken with scripts/dbtool.py or Railway's own backups
    (see BACKUP_RESTORE.md). In local SQLite development this page can still copy the file."""
    backup_dir = current_app.config["BACKUP_DIR"]
    os.makedirs(backup_dir, exist_ok=True)
    is_postgres = bool(current_app.config.get("DATABASE_URL"))

    if request.method == "POST" and not is_postgres:
        db_path = current_app.config["DATABASE_PATH"]
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(backup_dir, f"attendance-{stamp}.db")
        shutil.copy2(db_path, dest)
        log_action("backup.manual", "database", None, new_value={"file": os.path.basename(dest)})
        flash(f"Backup created: attendance-{stamp}.db", "success")
        return redirect(url_for("admin_ops.backup"))

    files = sorted(f for f in os.listdir(backup_dir) if f.endswith(".db")) if os.path.isdir(backup_dir) else []
    files.reverse()
    return render_template("admin/backup.html", files=files, is_postgres=is_postgres,
                           storage_persistent=current_app.config["STORAGE_PERSISTENT"])


@bp.route("/backup/<path:filename>")
@role_required(*ADMIN_ROLES)
def backup_download(filename):
    log_action("backup.download", "database", None, new_value={"file": os.path.basename(filename)})
    return send_from_directory(current_app.config["BACKUP_DIR"], filename, as_attachment=True)


@bp.route("/backup/face-data.zip")
@role_required(*ADMIN_ROLES)
def face_data_download():
    """Super-Administrator-only archive of the enrolled face samples (never publicly served)."""
    import tempfile
    import zipfile
    from flask import after_this_request, send_file
    face_dir = current_app.config["FACE_DATA_DIR"]
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, names in os.walk(face_dir):
            for name in names:
                full = os.path.join(root, name)
                zf.write(full, os.path.relpath(full, face_dir))

    @after_this_request
    def _cleanup(resp):
        try:
            os.remove(tmp.name)
        except OSError:
            pass
        return resp

    log_action("backup.face_data_download", "biometrics", None)
    return send_file(tmp.name, as_attachment=True,
                     download_name=f"face-data-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip")
