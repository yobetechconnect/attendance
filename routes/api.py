"""
/api/v1/devices and /api/v1/attendance/offline-sync — the two
endpoints a physical attendance device talks to directly, authenticated
by its own API key rather than a user session (Section 34/35/44).

This is the data-plane half of offline sync: it accepts a device's
queued events, deduplicates by client-generated event_uid, and stores
them for review. It does NOT auto-resolve conflicts against
already-synced attendance — that reconciliation is a manual admin
review step in this release (see README "Known limitations").
"""

import json
import uuid

from flask import Blueprint, jsonify, request
from werkzeug.security import check_password_hash

from database.db import get_db

bp = Blueprint("api", __name__, url_prefix="/api/v1")


def _authenticate_device(db):
    api_key = request.headers.get("X-Device-Key", "")
    if not api_key:
        return None
    devices = db.execute("SELECT * FROM devices").fetchall()
    for device in devices:
        if check_password_hash(device["api_key_hash"], api_key):
            return device
    return None


@bp.route("/devices/heartbeat", methods=("POST",))
def device_heartbeat():
    db = get_db()
    device = _authenticate_device(db)
    if device is None:
        return jsonify({"error": "Invalid or missing device key."}), 401

    software_version = (request.json or {}).get("software_version") if request.is_json else None
    db.execute(
        "UPDATE devices SET status = 'online', last_seen_at = datetime('now'), software_version = COALESCE(?, software_version) WHERE id = ?",
        (software_version, device["id"]),
    )
    db.commit()
    return jsonify({"ok": True, "device": device["device_code"]})


@bp.route("/attendance/offline-sync", methods=("POST",))
def offline_sync():
    """
    Body: {"events": [{"event_uid": "...", "session_id": 1, "student_id": 2,
                        "occurred_at": "2026-09-25T08:03:00", "payload": {...}}, ...]}
    Each event is deduplicated by event_uid (Section 35) and queued as
    'pending' for an admin to review/apply — see admin/sync_review.html.
    """
    db = get_db()
    device = _authenticate_device(db)
    if device is None:
        return jsonify({"error": "Invalid or missing device key."}), 401

    body = request.get_json(silent=True) or {}
    events = body.get("events", [])
    accepted, duplicates = 0, 0

    for event in events:
        event_uid = event.get("event_uid") or str(uuid.uuid4())
        existing = db.execute("SELECT 1 FROM offline_sync_events WHERE event_uid = ?", (event_uid,)).fetchone()
        if existing:
            duplicates += 1
            continue
        db.execute(
            """INSERT INTO offline_sync_events
                   (event_uid, device_id, session_id, student_id, payload_json, occurred_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_uid, device["id"], event.get("session_id"), event.get("student_id"),
             json.dumps(event.get("payload", {})), event.get("occurred_at")),
        )
        accepted += 1
    db.commit()

    return jsonify({"accepted": accepted, "duplicates": duplicates, "total": len(events)})
