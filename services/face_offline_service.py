"""
Offline face recognition - SERVER side (Stage 2 and 3).

The phone computes face embeddings locally. The server never sees a photo:
it stores and distributes small numeric templates, validates what it can,
and keeps a complete audit trail.

What the server CAN verify
  * the configured engine/version, template size and finite, unit-length values
  * who is allowed to enrol (Super Administrator) and to download (the teacher
    assigned to that class)
  * consent was confirmed, only one active template per student and engine
  * for face attendance: the claimed match refers to a real template of that
    student and meets the similarity floor

What it CANNOT verify: that a phone really computed the embedding it claims.
A modified client could lie. That is why every face record carries the device,
account, engine, template id and confidence, and why a manual mark can never
silently replace one. See OFFLINE_SYNC.md.
"""

import array
import base64
import binascii
import json
import math
import sys
from datetime import datetime, timezone

from utils.audit import log_action

MAX_TEMPLATE_BYTES = 8192            # 2048 float32 values: far above any real embedding size
MIN_NORM, MAX_NORM = 0.97, 1.03       # clients send L2-normalised vectors


class TemplateError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def get_config(cfg):
    engine = (cfg.get("FACE_OFFLINE_ENGINE") or "").strip()
    version = (cfg.get("FACE_OFFLINE_ENGINE_VERSION") or "").strip()
    dim = int(cfg.get("FACE_OFFLINE_EMBEDDING_DIM") or 0)
    return {
        "enabled": bool(engine and version and dim > 0),
        "engine": engine, "engine_version": version, "dim": dim,
        "min_similarity": float(cfg.get("FACE_OFFLINE_MIN_SIMILARITY", 0.60)),
        "margin": float(cfg.get("FACE_OFFLINE_MARGIN", 0.05)),
        "min_samples": int(cfg.get("FACE_OFFLINE_MIN_SAMPLES", 5)),
    }


def public_config(fc):
    """What a phone is told (never anything secret)."""
    if not fc["enabled"]:
        return {"offline_supported": False, "engines": []}
    return {"offline_supported": True, "engine": fc["engine"], "engine_version": fc["engine_version"],
            "dim": fc["dim"], "min_similarity": fc["min_similarity"], "margin": fc["margin"],
            "min_samples": fc["min_samples"]}


def decode_template(b64, dim):
    """base64 of little-endian float32 -> list of floats. Raises TemplateError."""
    if not isinstance(b64, str) or not b64 or len(b64) > MAX_TEMPLATE_BYTES * 2:
        raise TemplateError("invalid_template")
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise TemplateError("invalid_template") from None
    if len(raw) != dim * 4 or len(raw) > MAX_TEMPLATE_BYTES:
        raise TemplateError("wrong_dimension")
    vec = array.array("f")
    vec.frombytes(raw)
    if sys.byteorder == "big":
        vec.byteswap()
    values = vec.tolist()
    if not all(math.isfinite(v) for v in values):
        raise TemplateError("invalid_template")
    norm = math.sqrt(sum(v * v for v in values))
    if not (MIN_NORM <= norm <= MAX_NORM):
        raise TemplateError("not_normalised")
    return values


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------- validation
def validate_enrollment(raw, fc):
    """Pure validation. Returns a clean dict or raises PayloadProblem(message)."""
    from services.sync_service import UID_RE, PayloadError, _naive_iso, _parse_iso
    if not isinstance(raw, dict):
        raise PayloadError("Invalid request.")
    uid = raw.get("client_uid")
    if not isinstance(uid, str) or not UID_RE.match(uid):
        raise PayloadError("Enrollment id is invalid.")
    try:
        student_id = int(raw.get("student_id"))
    except (TypeError, ValueError):
        raise PayloadError("Student is invalid.") from None
    count = raw.get("sample_count")
    if not isinstance(count, int) or isinstance(count, bool) or not (1 <= count <= 50):
        raise PayloadError("Sample count is invalid.")
    quality = raw.get("quality_score")
    if quality is not None and (not isinstance(quality, (int, float)) or isinstance(quality, bool)
                                or not (0 <= quality <= 1)):
        raise PayloadError("Quality score is invalid.")
    note = raw.get("consent_note")
    if note is not None and (not isinstance(note, str) or len(note) > 200):
        raise PayloadError("Consent note is too long.")
    if raw.get("consent_confirmed") is not True:
        raise PayloadError("Consent must be confirmed before a face is enrolled.")
    for key in ("engine", "engine_version"):
        if not isinstance(raw.get(key), str) or not (1 <= len(raw[key]) <= 40):
            raise PayloadError("Engine information is invalid.")
    return {
        "client_uid": uid, "student_id": student_id, "sample_count": count, "quality_score": quality,
        "consent_note": (note or "").strip() or None, "replace": raw.get("replace") is True,
        "engine": raw["engine"], "engine_version": raw["engine_version"],
        "template_b64": raw.get("template_b64"),
        "captured_at": _naive_iso(_parse_iso(raw.get("captured_at"), "Capture time")),
    }


# ----------------------------------------------------------------- enrolment
def _receipt(db, uid):
    return db.execute("SELECT * FROM sync_receipts WHERE client_uid = ?", (uid,)).fetchone()


def _add_receipt(db, uid, user_id, device_uid, server_id, result, reason=None):
    db.execute("""INSERT INTO sync_receipts (client_uid, kind, user_id, device_uid, server_id, result, reason)
                  VALUES (?, 'enrollment', ?, ?, ?, ?, ?)""", (uid, user_id, device_uid, server_id, result, reason))


def _conflict(db, user_id, device_uid, uid, student_id, reason, incoming, existing=None):
    db.execute(
        """INSERT INTO sync_conflicts (kind, student_id, user_id, device_uid, client_uid, reason,
                                       existing_json, incoming_json, resolution)
           VALUES ('enrollment_conflict', ?, ?, ?, ?, ?, ?, ?, 'needs_review')""",
        (student_id, user_id, device_uid, uid, reason,
         json.dumps(existing) if existing is not None else None, json.dumps(incoming)))


def enroll(db, user, device_uid, p, fc):
    """Idempotent. Commits once. Returns {result, reason, server_id}."""
    for attempt in (1, 2):
        try:
            out = _enroll_once(db, user, device_uid, p, fc)
            db.commit()
            break
        except Exception as exc:
            db.rollback()
            name = type(exc).__name__
            if attempt == 1 and ("IntegrityError" in name or "UniqueViolation" in name):
                continue
            raise
    log_action("face.enroll", "student", p["student_id"],
               new_value={"enrollment_uid": p["client_uid"], "device": device_uid, "result": out["result"],
                          "engine": p["engine"], "engine_version": p["engine_version"]})
    return out


def _enroll_once(db, user, device_uid, p, fc):
    prior = _receipt(db, p["client_uid"])
    if prior is not None:
        if prior["user_id"] not in (None, user["id"]):
            from services.sync_service import PayloadError
            raise PayloadError("This enrollment id is already in use.")
        return {"result": prior["result"], "reason": prior["reason"], "server_id": prior["server_id"]}

    def reject(reason):
        _add_receipt(db, p["client_uid"], user["id"], device_uid, None, "rejected", reason)
        return {"result": "rejected", "reason": reason, "server_id": None}

    if not fc["enabled"]:
        return reject("face_not_enabled")
    if p["engine"] != fc["engine"] or p["engine_version"] != fc["engine_version"]:
        return reject("engine_mismatch")
    try:
        decode_template(p["template_b64"], fc["dim"])
    except TemplateError as exc:
        return reject(exc.code)
    if p["sample_count"] < fc["min_samples"]:
        return reject("too_few_samples")
    student = db.execute("SELECT id, status FROM students WHERE id = ?", (p["student_id"],)).fetchone()
    if student is None or student["status"] != "active":
        return reject("student_not_active")

    existing = db.execute(
        "SELECT * FROM face_templates WHERE student_id = ? AND engine = ? AND is_active = 1",
        (p["student_id"], p["engine"])).fetchone()
    result = "created"
    if existing is not None:
        if existing["enrollment_uid"] == p["client_uid"]:
            _add_receipt(db, p["client_uid"], user["id"], device_uid, existing["id"], "duplicate")
            return {"result": "duplicate", "reason": None, "server_id": existing["id"]}
        if not p["replace"]:
            incoming = dict(p)                              # kept so an administrator can still apply it
            snapshot = {"template_id": existing["id"], "enrollment_uid": existing["enrollment_uid"],
                        "created_at": existing["created_at"], "device_uid": existing["device_uid"],
                        "source": existing["source"]}
            _add_receipt(db, p["client_uid"], user["id"], device_uid, existing["id"], "conflict", "already_enrolled")
            _conflict(db, user["id"], device_uid, p["client_uid"], p["student_id"], "already_enrolled", incoming, snapshot)
            return {"result": "conflict", "reason": "already_enrolled", "server_id": existing["id"]}
        db.execute("UPDATE face_templates SET is_active = 0, deactivated_at = ? WHERE id = ?", (_now(), existing["id"]))
        result = "replaced"
    new_id = _insert_template(db, p, user["id"], device_uid)
    _add_receipt(db, p["client_uid"], user["id"], device_uid, new_id, result)
    return {"result": result, "reason": None, "server_id": new_id}


def _insert_template(db, p, user_id, device_uid, source="offline"):
    cur = db.execute(
        """INSERT INTO face_templates (student_id, engine, engine_version, enrollment_uid, template_b64, sample_count,
                                       quality_score, source, device_uid, created_by_user_id, consent_confirmed,
                                       consent_note, synced_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        (p["student_id"], p["engine"], p["engine_version"], p["client_uid"], p["template_b64"], p["sample_count"],
         p["quality_score"], source, device_uid, user_id, p.get("consent_note"), _now()))
    return cur.lastrowid


def apply_enrollment_conflict(db, incoming, admin_user_id):
    """Administrator chose 'use this enrollment': replace the active template, keep the old one inactive."""
    old = db.execute("SELECT id FROM face_templates WHERE student_id = ? AND engine = ? AND is_active = 1",
                     (incoming["student_id"], incoming["engine"])).fetchone()
    if old is not None:
        db.execute("UPDATE face_templates SET is_active = 0, deactivated_at = ? WHERE id = ?", (_now(), old["id"]))
    if db.execute("SELECT 1 FROM face_templates WHERE enrollment_uid = ?", (incoming["client_uid"],)).fetchone():
        return False
    _insert_template(db, incoming, admin_user_id, incoming.get("device_uid"), source="offline")
    return True


# -------------------------------------------------------------- distribution
def teacher_owns_class(db, user, class_id):
    t = db.execute("SELECT id FROM teachers WHERE user_id = ?", (user["id"],)).fetchone()
    if t is None:
        return False
    return db.execute("""SELECT 1 FROM subject_assignments sa JOIN classes c ON c.id = sa.class_id
                         WHERE sa.teacher_id = ? AND sa.class_id = ? AND sa.is_active = 1 AND c.is_active = 1""",
                      (t["id"], class_id)).fetchone() is not None


def templates_for_class(db, class_id, fc):
    rows = db.execute(
        """SELECT ft.student_id, ft.enrollment_uid, ft.template_b64, ft.created_at
           FROM face_templates ft JOIN students st ON st.id = ft.student_id
           WHERE st.class_id = ? AND st.status = 'active' AND ft.is_active = 1
                 AND ft.engine = ? AND ft.engine_version = ?
           ORDER BY ft.student_id""", (class_id, fc["engine"], fc["engine_version"])).fetchall()
    have = {r["student_id"] for r in rows}
    missing = [r["id"] for r in db.execute(
        "SELECT id FROM students WHERE class_id = ? AND status = 'active' ORDER BY id", (class_id,)).fetchall()
        if r["id"] not in have]
    return [dict(r) for r in rows], missing


def enrollment_bootstrap(db, settings, fc, grace_hours, app_version, user):
    rows = db.execute(
        """SELECT st.id, st.admission_number, st.first_name, st.surname, c.name AS class_name,
                  (SELECT COUNT(*) FROM face_templates ft WHERE ft.student_id = st.id AND ft.is_active = 1
                     AND ft.engine = ? AND ft.engine_version = ?) AS browser_templates,
                  (SELECT COUNT(*) FROM biometric_profiles bp WHERE bp.student_id = st.id AND bp.is_active = 1) AS lbph
           FROM students st LEFT JOIN classes c ON c.id = st.class_id
           WHERE st.status = 'active' ORDER BY st.surname, st.first_name""",
        (fc["engine"], fc["engine_version"])).fetchall()
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    return {
        "ok": True, "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "app_version": app_version,
        "user": {"id": user["id"], "name": user["full_name"], "role": user["role"]},
        "offline_policy": {"grace_hours": grace_hours,
                           "expires_at": (now + timedelta(hours=grace_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")},
        "settings": {"timezone": settings["timezone"] or "Africa/Lagos"},
        "face": public_config(fc),
        "students": [{"id": r["id"], "admission_number": r["admission_number"], "first_name": r["first_name"],
                      "surname": r["surname"], "class_name": r["class_name"],
                      "face_offline_enrolled": bool(r["browser_templates"]), "face_lbph_enrolled": bool(r["lbph"])}
                     for r in rows],
    }


# ---------------------------------------------------------- admin / retention
def deactivate_template(db, template_id, admin_user_id):
    cur = db.execute("UPDATE face_templates SET is_active = 0, deactivated_at = ? WHERE id = ? AND is_active = 1",
                     (_now(), template_id))
    db.commit()
    if cur.rowcount:
        log_action("face.template_deactivate", "face_template", template_id)
    return cur.rowcount > 0


def delete_template(db, template_id, admin_user_id):
    row = db.execute("SELECT student_id, engine, enrollment_uid FROM face_templates WHERE id = ?", (template_id,)).fetchone()
    if row is None:
        return False
    db.execute("DELETE FROM face_templates WHERE id = ?", (template_id,))
    db.commit()
    log_action("face.template_delete", "face_template", template_id,
               previous_value={"student_id": row["student_id"], "engine": row["engine"]})
    return True


def purge(db, days, dry_run=False):
    """Retention: delete INACTIVE templates older than `days`, and ALL templates of students who
    are no longer active (withdrawn/graduated) once they have been inactive that long."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
    inactive = db.execute(
        "SELECT id FROM face_templates WHERE is_active = 0 AND COALESCE(deactivated_at, created_at) < ?", (cutoff,)).fetchall()
    gone = db.execute(
        """SELECT ft.id FROM face_templates ft JOIN students st ON st.id = ft.student_id
           WHERE st.status != 'active' AND ft.created_at < ?""", (cutoff,)).fetchall()
    ids = sorted({r["id"] for r in inactive} | {r["id"] for r in gone})
    if ids and not dry_run:
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            db.execute(f"DELETE FROM face_templates WHERE id IN ({','.join('?' for _ in chunk)})", chunk)
        db.commit()
    return ids
