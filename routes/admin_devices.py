"""
Device management (Section 34) — registers attendance hardware
(scanners/tablets) and tracks online/offline status. The API key
issued here is what a device presents to /api/v1/devices/heartbeat
and /api/v1/attendance/offline-sync.
"""

import secrets

from flask import Blueprint, flash, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from database.db import get_db
from utils.audit import log_action
from utils.decorators import role_required
from utils.validators import validate_required_fields

bp = Blueprint("admin_devices", __name__, url_prefix="/admin/devices")
ADMIN_ROLES = ("super_admin",)   # only the Super Administrator is unrestricted


@bp.route("/", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def devices_list():
    db = get_db()
    new_api_key = None
    if request.method == "POST":
        errors = validate_required_fields(request.form, ["device_code", "name"])
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            new_api_key = secrets.token_urlsafe(32)
            try:
                cur = db.execute(
                    """INSERT INTO devices (device_code, name, location, assigned_class_id, api_key_hash)
                       VALUES (?, ?, ?, ?, ?)""",
                    (request.form["device_code"].strip(), request.form["name"].strip(),
                     request.form.get("location", "").strip() or None,
                     request.form.get("assigned_class_id") or None,
                     generate_password_hash(new_api_key)),
                )
                db.commit()
                log_action("device.create", "device", cur.lastrowid)
                flash("Device registered. Copy the API key now — it will not be shown again.", "success")
            except Exception:
                flash("A device with that code already exists.", "error")
                new_api_key = None

    rows = db.execute(
        """SELECT d.*, c.name AS class_name FROM devices d
           LEFT JOIN classes c ON c.id = d.assigned_class_id ORDER BY d.name"""
    ).fetchall()
    classes = db.execute("SELECT * FROM classes WHERE is_active = 1 ORDER BY name").fetchall()
    return render_template("admin/devices_list.html", rows=rows, classes=classes, new_api_key=new_api_key)


@bp.route("/<int:device_id>/deactivate", methods=("POST",))
@role_required(*ADMIN_ROLES)
def deactivate_device(device_id):
    db = get_db()
    db.execute("UPDATE devices SET status = 'offline' WHERE id = ?", (device_id,))
    db.commit()
    log_action("device.deactivate", "device", device_id)
    flash("Device marked offline.", "info")
    return redirect(url_for("admin_devices.devices_list"))
