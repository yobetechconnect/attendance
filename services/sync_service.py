"""
Offline synchronisation service (Stage 1: manual attendance).

Everything the server decides about data that was captured offline lives
here, so the rules are in one reviewable place:

  * bootstrap_for_teacher()  - the minimum, class-scoped data a phone may keep
  * validate_session_payload() - strict input validation
  * sync_session()           - idempotent, transactional save of one session
  * decide_conflict()        - explicit per-student conflict policy

Principles
  - Never trust the client: ownership of the class/subject, the student's
    class membership and every value are re-checked on the server.
  - Idempotent: every client record has a stable UUID. A replayed request
    returns the stored decision (sync_receipts) and creates nothing new.
  - Nothing is silently dropped: conflicts and rejections are written to
    sync_conflicts with the full incoming data for administrator review.
  - One session = one database transaction (no partial attendance sessions).
"""

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone

from services.attendance_service import generate_session_code
from utils.audit import log_action

# 'face' records come from the on-device recognition engine (Stage 2). The server cannot re-run
# the match, so it validates the claim (see _face_claim_problem) and keeps the evidence.
ACCEPTED_METHODS = ("manual", "face")
ACCEPTED_STATUSES = ("present", "late", "absent", "excused")
AUTOMATIC_METHODS = ("face", "qr", "rfid")
MAX_RECORDS_PER_SESSION = 500
FACE_REJECTIONS = {
    "face_not_enabled": "Offline face recognition is not enabled on the server.",
}
UID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PayloadError(ValueError):
    """The request is malformed. Mapped to HTTP 400 with a safe message."""


# ------------------------------------------------------------------ helpers
def _now_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _school_today(tz_name):
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name or "Africa/Lagos")).date()
    except Exception:
        return datetime.now(timezone.utc).date()


def _parse_iso(value, field):
    if not isinstance(value, str) or len(value) > 40:
        raise PayloadError(f"{field} is not a valid date/time.")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise PayloadError(f"{field} is not a valid date/time.") from None


def _naive_iso(dt):
    """Store school-local wall-clock time, no offset (matches existing rows)."""
    return dt.replace(tzinfo=None).isoformat(timespec="seconds")


def _is_unique_violation(exc):
    name = type(exc).__name__
    return "IntegrityError" in name or "UniqueViolation" in name


# --------------------------------------------------------------- validation
def validate_session_payload(raw, tz_name):
    """Return a cleaned dict or raise PayloadError. No database access."""
    if not isinstance(raw, dict):
        raise PayloadError("Invalid request.")
    p = {}
    uid = raw.get("client_uid")
    if not isinstance(uid, str) or not UID_RE.match(uid):
        raise PayloadError("Session id is invalid.")
    p["client_uid"] = uid
    try:
        p["subject_assignment_id"] = int(raw.get("subject_assignment_id"))
    except (TypeError, ValueError):
        raise PayloadError("Class/subject is invalid.") from None

    date_s = raw.get("session_date")
    if not isinstance(date_s, str) or not DATE_RE.match(date_s):
        raise PayloadError("Session date is invalid.")
    try:
        session_date = datetime.strptime(date_s, "%Y-%m-%d").date()
    except ValueError:
        raise PayloadError("Session date is invalid.") from None
    if session_date > _school_today(tz_name) + timedelta(days=1):
        raise PayloadError("Session date is in the future.")
    p["session_date"] = date_s

    p["scheduled_start"] = _naive_iso(_parse_iso(raw.get("scheduled_start"), "Start time"))
    p["closed_at"] = _naive_iso(_parse_iso(raw.get("closed_at") or raw.get("scheduled_start"), "Close time"))

    records = raw.get("records")
    if not isinstance(records, list) or not records:
        raise PayloadError("A session needs at least one student record.")
    if len(records) > MAX_RECORDS_PER_SESSION:
        raise PayloadError("Too many records in one session.")

    clean, seen_students, seen_uids = [], set(), set()
    for r in records:
        if not isinstance(r, dict):
            raise PayloadError("A record is invalid.")
        ruid = r.get("client_uid")
        if not isinstance(ruid, str) or not UID_RE.match(ruid) or ruid in seen_uids:
            raise PayloadError("A record id is invalid or repeated.")
        try:
            sid = int(r.get("student_id"))
        except (TypeError, ValueError):
            raise PayloadError("A student id is invalid.") from None
        if sid in seen_students:
            raise PayloadError("A student appears twice in one session.")
        status = r.get("status")
        if status not in ACCEPTED_STATUSES:
            raise PayloadError("A record has an unsupported attendance status.")
        method = r.get("method", "manual")
        if method not in ACCEPTED_METHODS:
            raise PayloadError("This verification method is not supported for offline sync yet.")
        recorded = _naive_iso(_parse_iso(r.get("recorded_at"), "Record time"))
        item = {"client_uid": ruid, "student_id": sid, "status": status,
                "method": method, "recorded_at": recorded}
        if method == "face":
            conf = r.get("confidence")
            if (not isinstance(conf, (int, float)) or isinstance(conf, bool) or not math.isfinite(conf)
                    or not (0 < conf <= 1)):
                raise PayloadError("A face record has an invalid confidence value.")
            if status not in ("present", "late"):
                raise PayloadError("A face match can only record present or late.")
            for key in ("engine", "engine_version"):
                if not isinstance(r.get(key), str) or not (1 <= len(r[key]) <= 40):
                    raise PayloadError("A face record has invalid engine information.")
            tuid = r.get("template_uid")
            if not isinstance(tuid, str) or not UID_RE.match(tuid):
                raise PayloadError("A face record has an invalid template id.")
            item.update({"confidence": float(conf), "engine": r["engine"],
                         "engine_version": r["engine_version"], "template_uid": tuid})
        seen_students.add(sid)
        seen_uids.add(ruid)
        clean.append(item)
    p["records"] = clean
    return p


# ----------------------------------------------------------------- bootstrap
def bootstrap_for_teacher(db, user, settings, grace_hours, app_version, face_public=None, face_cfg=None):
    """Class-scoped download for one teacher (data minimisation: only their
    active assignments, only active students of those classes, and only the
    fields needed to mark attendance)."""
    teacher = db.execute("SELECT * FROM teachers WHERE user_id = ?", (user["id"],)).fetchone()
    assignments, classes = [], []
    if teacher is not None:
        rows = db.execute(
            """SELECT sa.id, sa.class_id, sa.subject_id, sa.term_id,
                      sub.code AS subject_code, sub.name AS subject_name, c.name AS class_name
               FROM subject_assignments sa
               JOIN subjects sub ON sub.id = sa.subject_id
               JOIN classes c ON c.id = sa.class_id
               WHERE sa.teacher_id = ? AND sa.is_active = 1 AND c.is_active = 1 AND sub.is_active = 1
               ORDER BY c.name, sub.name""", (teacher["id"],)).fetchall()
        class_ids = []
        for r in rows:
            if r["class_id"] not in class_ids:
                class_ids.append(r["class_id"])
        roster = {}
        for cid in class_ids:
            roster[cid] = [
                {"id": s["id"], "admission_number": s["admission_number"],
                 "first_name": s["first_name"], "surname": s["surname"],
                 # Boolean only. The LBPH data itself never leaves the server.
                 "face_lbph_enrolled": bool(s["lbph"]),
                 "face_offline_enrolled": bool(s["browser"])}
                for s in db.execute(
                    """SELECT st.id, st.admission_number, st.first_name, st.surname,
                              (SELECT COUNT(*) FROM biometric_profiles bp
                               WHERE bp.student_id = st.id AND bp.is_active = 1) AS lbph,
                              (SELECT COUNT(*) FROM face_templates ft
                               WHERE ft.student_id = st.id AND ft.is_active = 1 AND ft.engine = ?
                                     AND ft.engine_version = ?) AS browser
                       FROM students st WHERE st.class_id = ? AND st.status = 'active'
                       ORDER BY st.surname, st.first_name""",
                    ((face_cfg or {}).get("engine", ""), (face_cfg or {}).get("engine_version", ""), cid)).fetchall()]
        names = {r["class_id"]: r["class_name"] for r in rows}
        for cid in class_ids:
            classes.append({"id": cid, "name": names[cid], "students": roster[cid]})
        for r in rows:
            assignments.append({
                "id": r["id"], "class_id": r["class_id"], "class_name": r["class_name"],
                "subject_id": r["subject_id"], "subject_name": r["subject_name"],
                "subject_code": r["subject_code"], "term_id": r["term_id"],
                "student_count": len(roster[r["class_id"]])})

    now = datetime.now(timezone.utc)
    body = {
        "ok": True,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "app_version": app_version,
        "user": {"id": user["id"], "name": user["full_name"], "role": user["role"]},
        "has_teacher_profile": teacher is not None,
        "offline_policy": {
            "grace_hours": grace_hours,
            "expires_at": (now + timedelta(hours=grace_hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "settings": {
            "school_name": settings["school_name"],
            "timezone": settings["timezone"] or "Africa/Lagos",
            "late_after_minutes": settings["late_after_minutes"] or 15,
            "current_term_id": settings["current_term_id"],
        },
        "assignments": assignments,
        "classes": classes,
        "face": face_public or {"offline_supported": False, "engines": []},
    }
    canonical = json.dumps({"a": assignments, "c": classes}, sort_keys=True)
    body["package_id"] = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return body


# ------------------------------------------------------------ conflict rules
def decide_conflict(existing, incoming):
    """Explicit per-student policy when a record already exists for
    (session, student). Returns (decision, reason).

      duplicate         same status -> nothing to do
      apply_incoming    existing is only the automatic 'absent' written when a
                        session closed (no teacher marked it) and the incoming
                        record is an explicit present/late/excused mark
      keep_existing     anything else. The first committed explicit mark wins
                        and the incoming one is kept for administrator review.
                        A manual mark can NEVER overwrite an automatic
                        (face/QR/RFID) record.
    """
    if existing["status"] == incoming["status"]:
        return "duplicate", None
    auto_absent = (existing["status"] == "absent" and existing["verification_method"] == "manual"
                   and existing["marked_by_user_id"] is None)
    if auto_absent and incoming["status"] in ("present", "late", "excused"):
        return "apply_incoming", "replaced_automatic_absence"
    if existing["verification_method"] in AUTOMATIC_METHODS and incoming["method"] == "manual":
        return "keep_existing", "manual_cannot_override_automatic"
    return "keep_existing", "conflicting_marks"


# ---------------------------------------------------------------- the sync
def _face_claim_problem(db, r, face_cfg):
    """What the server can check about a claimed face match. Returns a reason code or None."""
    if not face_cfg.get("enabled"):
        return "face_not_enabled"
    if r["engine"] != face_cfg["engine"] or r["engine_version"] != face_cfg["engine_version"]:
        return "engine_mismatch"
    if r["confidence"] < face_cfg["min_similarity"]:
        return "below_threshold"
    tpl = db.execute("SELECT id FROM face_templates WHERE enrollment_uid = ? AND student_id = ? AND engine = ?",
                     (r["template_uid"], r["student_id"], r["engine"])).fetchone()
    if tpl is None:
        return "unknown_template"
    return None


def _receipts_for(db, uids):
    out = {}
    uids = list(uids)
    for i in range(0, len(uids), 400):
        chunk = uids[i:i + 400]
        marks = ",".join("?" for _ in chunk)
        for r in db.execute(f"SELECT * FROM sync_receipts WHERE client_uid IN ({marks})", chunk).fetchall():
            out[r["client_uid"]] = r
    return out


def _add_receipt(db, uid, kind, user_id, device_uid, server_id, result, reason=None):
    db.execute(
        """INSERT INTO sync_receipts (client_uid, kind, user_id, device_uid, server_id, result, reason)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (uid, kind, user_id, device_uid, server_id, result, reason))


def _add_conflict(db, kind, user_id, device_uid, client_uid, reason, incoming, existing=None,
                  session_id=None, record_id=None, student_id=None, resolution="needs_review"):
    db.execute(
        """INSERT INTO sync_conflicts (kind, session_id, record_id, student_id, user_id, device_uid,
                                       client_uid, reason, existing_json, incoming_json, resolution)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (kind, session_id, record_id, student_id, user_id, device_uid, client_uid, reason,
         json.dumps(existing) if existing is not None else None, json.dumps(incoming), resolution))


def _touch_device(db, user_id, device_uid, user_agent):
    if not device_uid:
        return
    cur = db.execute(
        """UPDATE client_devices SET last_seen_at = datetime('now'), last_sync_at = datetime('now'),
                  user_agent = ? WHERE device_uid = ? AND user_id = ?""",
        (user_agent, device_uid, user_id))
    if cur.rowcount == 0:
        db.execute(
            """INSERT INTO client_devices (device_uid, user_id, user_agent, last_sync_at)
               VALUES (?, ?, ?, datetime('now'))""", (device_uid, user_id, user_agent))


def sync_session(db, user, device_uid, p, user_agent=None, face_cfg=None):
    """Save one offline session. Idempotent; commits exactly once.
    Returns the JSON-ready result. Raises on unexpected database errors
    (after rollback) so the client keeps its record and retries."""
    for attempt in (1, 2):
        try:
            result = _sync_session_once(db, user, device_uid, p, user_agent, face_cfg or {})
            db.commit()
            break
        except Exception as exc:
            db.rollback()
            # A concurrent retry of the same session won the race: run again,
            # this time the committed receipts make it a pure replay.
            if attempt == 1 and _is_unique_violation(exc):
                continue
            raise
    log_action("attendance.offline_sync", "attendance_session", result["session"].get("server_session_id"),
               new_value={"client_uid": p["client_uid"], "device": device_uid,
                          "state": result["session"]["state"], "counts": result["counts"]})
    return result


def _reject_all(db, user, device_uid, p, reason, session_server_id=None):
    out = []
    for r in p["records"]:
        _add_receipt(db, r["client_uid"], "attendance_record", user["id"], device_uid, None, "rejected", reason)
        out.append({"client_uid": r["client_uid"], "result": "rejected", "reason": reason,
                    "server_record_id": None})
    return out


def _sync_session_once(db, user, device_uid, p, user_agent, face_cfg):
    counts = {"created": 0, "duplicate": 0, "conflict": 0, "rejected": 0}
    now = _now_utc_str()

    # ---- replay of a session the server already decided about -------------
    prior = _receipts_for(db, [p["client_uid"]] + [r["client_uid"] for r in p["records"]])
    session_receipt = prior.get(p["client_uid"])
    if session_receipt is not None and session_receipt["user_id"] not in (None, user["id"]):
        # Same id presented by a different account: never reveal or merge.
        raise PayloadError("This session id is already in use.")

    teacher = db.execute("SELECT * FROM teachers WHERE user_id = ?", (user["id"],)).fetchone()
    assignment = db.execute("SELECT * FROM subject_assignments WHERE id = ?",
                            (p["subject_assignment_id"],)).fetchone()
    allowed = (teacher is not None and assignment is not None
               and assignment["teacher_id"] == teacher["id"] and assignment["is_active"] == 1)

    if session_receipt is not None and session_receipt["result"] == "rejected":
        records = []
        for r in p["records"]:
            pr = prior.get(r["client_uid"])
            records.append({"client_uid": r["client_uid"], "result": "rejected",
                            "reason": (pr["reason"] if pr else session_receipt["reason"]),
                            "server_record_id": None})
            counts["rejected"] += 1
        return {"ok": True, "session": {"client_uid": p["client_uid"], "server_session_id": None,
                                        "state": "rejected", "reason": session_receipt["reason"]},
                "records": records, "counts": counts}

    if not allowed:
        reason = "assignment_not_allowed" if teacher is not None else "no_teacher_profile"
        _add_receipt(db, p["client_uid"], "attendance_session", user["id"], device_uid, None, "rejected", reason)
        _add_conflict(db, "rejected_session", user["id"], device_uid, p["client_uid"], reason, p)
        records = _reject_all(db, user, device_uid, p, reason)
        counts["rejected"] = len(records)
        _touch_device(db, user["id"], device_uid, user_agent)
        return {"ok": True, "session": {"client_uid": p["client_uid"], "server_session_id": None,
                                        "state": "rejected", "reason": reason},
                "records": records, "counts": counts}

    # ---- find or create the server session ---------------------------------
    created_session = False
    if session_receipt is not None:
        server_session_id = session_receipt["server_id"]
    else:
        existing = db.execute(
            """SELECT id FROM attendance_sessions
               WHERE subject_assignment_id = ? AND session_date = ? AND source = 'offline'
               ORDER BY id LIMIT 1""", (assignment["id"], p["session_date"])).fetchone()
        if existing is not None:
            # A second device for the same class/subject/day: merge into one
            # sheet instead of creating a duplicate session.
            server_session_id = existing["id"]
        else:
            subject = db.execute("SELECT * FROM subjects WHERE id = ?", (assignment["subject_id"],)).fetchone()
            class_row = db.execute("SELECT * FROM classes WHERE id = ?", (assignment["class_id"],)).fetchone()
            start_dt = datetime.fromisoformat(p["scheduled_start"])
            code = None
            for _ in range(6):
                candidate = generate_session_code(subject["code"], class_row["name"], start_dt)
                if db.execute("SELECT 1 FROM attendance_sessions WHERE session_code = ?", (candidate,)).fetchone() is None:
                    code = candidate
                    break
            if code is None:
                raise RuntimeError("Could not allocate a session code.")
            cur = db.execute(
                """INSERT INTO attendance_sessions
                       (session_code, subject_assignment_id, session_date, scheduled_start, status,
                        started_at, closed_at, started_by_user_id, client_uid, source, device_uid, synced_at)
                   VALUES (?, ?, ?, ?, 'closed', ?, ?, ?, ?, 'offline', ?, ?)""",
                (code, assignment["id"], p["session_date"], p["scheduled_start"],
                 p["scheduled_start"], p["closed_at"], user["id"], p["client_uid"], device_uid, now))
            server_session_id = cur.lastrowid
            created_session = True
        _add_receipt(db, p["client_uid"], "attendance_session", user["id"], device_uid,
                     server_session_id, "created" if created_session else "merged")

    session_row = db.execute("SELECT session_code FROM attendance_sessions WHERE id = ?",
                             (server_session_id,)).fetchone()

    # ---- the student records ------------------------------------------------
    student_ids = [r["student_id"] for r in p["records"]]
    marks = ",".join("?" for _ in student_ids)
    students = {s["id"]: s for s in db.execute(
        f"SELECT id, class_id, status FROM students WHERE id IN ({marks})", student_ids).fetchall()}

    out = []
    for r in sorted(p["records"], key=lambda x: x["recorded_at"]):
        pr = prior.get(r["client_uid"])
        if pr is not None:                      # exact replay -> stored decision
            res = pr["result"]
            out.append({"client_uid": r["client_uid"], "result": res, "reason": pr["reason"],
                        "server_record_id": pr["server_id"]})
            counts[res if res in counts else "duplicate"] += 1
            continue

        st = students.get(r["student_id"])
        if st is None or st["status"] != "active" or st["class_id"] != assignment["class_id"]:
            reason = "student_not_in_class"
            _add_receipt(db, r["client_uid"], "attendance_record", user["id"], device_uid, None, "rejected", reason)
            # student_id column is a foreign key: only link a student that really exists
            # (an unknown id is still preserved inside incoming_json for review).
            _add_conflict(db, "rejected_record", user["id"], device_uid, r["client_uid"], reason, r,
                          session_id=server_session_id,
                          student_id=r["student_id"] if st is not None else None)
            out.append({"client_uid": r["client_uid"], "result": "rejected", "reason": reason,
                        "server_record_id": None})
            counts["rejected"] += 1
            continue

        if r["method"] == "face":
            problem = _face_claim_problem(db, r, face_cfg)
            if problem:
                _add_receipt(db, r["client_uid"], "attendance_record", user["id"], device_uid, None, "rejected", problem)
                _add_conflict(db, "rejected_record", user["id"], device_uid, r["client_uid"], problem, r,
                              session_id=server_session_id, student_id=r["student_id"])
                db.execute("""INSERT INTO verification_events (session_id, student_id, method, result, confidence)
                              VALUES (?, ?, 'face', 'rejected_no_match', ?)""",
                           (server_session_id, r["student_id"], r["confidence"]))
                out.append({"client_uid": r["client_uid"], "result": "rejected", "reason": problem,
                            "server_record_id": None})
                counts["rejected"] += 1
                continue
        cur = db.execute(
            """INSERT INTO attendance_records
                   (session_id, student_id, status, verification_method, confidence, recorded_at, marked_by_user_id,
                    client_uid, source, device_uid, client_recorded_at, synced_at,
                    match_engine, match_engine_version, match_template_uid)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'offline', ?, ?, ?, ?, ?, ?)
               ON CONFLICT (session_id, student_id) DO NOTHING""",
            (server_session_id, r["student_id"], r["status"], r["method"], r.get("confidence"), r["recorded_at"],
             user["id"], r["client_uid"], device_uid, r["recorded_at"], now,
             r.get("engine"), r.get("engine_version"), r.get("template_uid")))
        if cur.rowcount == 1:
            rec_id = cur.lastrowid
            _add_receipt(db, r["client_uid"], "attendance_record", user["id"], device_uid, rec_id, "created")
            db.execute("""INSERT INTO verification_events (session_id, student_id, method, result, confidence)
                          VALUES (?, ?, ?, 'success', ?)""",
                       (server_session_id, r["student_id"], r["method"], r.get("confidence")))
            out.append({"client_uid": r["client_uid"], "result": "created", "reason": None,
                        "server_record_id": rec_id})
            counts["created"] += 1
            continue

        existing = db.execute(
            "SELECT * FROM attendance_records WHERE session_id = ? AND student_id = ?",
            (server_session_id, r["student_id"])).fetchone()
        decision, reason = decide_conflict(existing, r)
        snapshot = {"id": existing["id"], "status": existing["status"],
                    "method": existing["verification_method"], "recorded_at": existing["recorded_at"],
                    "marked_by_user_id": existing["marked_by_user_id"], "source": existing["source"]}
        if decision == "duplicate":
            _add_receipt(db, r["client_uid"], "attendance_record", user["id"], device_uid, existing["id"], "duplicate")
            db.execute("""INSERT INTO verification_events (session_id, student_id, method, result)
                          VALUES (?, ?, ?, 'rejected_duplicate')""", (server_session_id, r["student_id"], r["method"]))
            out.append({"client_uid": r["client_uid"], "result": "duplicate", "reason": None,
                        "server_record_id": existing["id"]})
            counts["duplicate"] += 1
        elif decision == "apply_incoming":
            db.execute(
                """UPDATE attendance_records SET status = ?, verification_method = ?, confidence = ?, recorded_at = ?,
                          marked_by_user_id = ?, client_uid = ?, source = 'offline', device_uid = ?,
                          client_recorded_at = ?, synced_at = ?, match_engine = ?, match_engine_version = ?,
                          match_template_uid = ? WHERE id = ?""",
                (r["status"], r["method"], r.get("confidence"), r["recorded_at"], user["id"], r["client_uid"],
                 device_uid, r["recorded_at"], now, r.get("engine"), r.get("engine_version"),
                 r.get("template_uid"), existing["id"]))
            db.execute(
                """INSERT INTO attendance_corrections (record_id, previous_status, new_status, reason, corrected_by_user_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (existing["id"], existing["status"], r["status"],
                 "Offline attendance synced: replaced the automatic absence recorded when the session closed.",
                 user["id"]))
            _add_receipt(db, r["client_uid"], "attendance_record", user["id"], device_uid, existing["id"], "created", reason)
            _add_conflict(db, "record_conflict", user["id"], device_uid, r["client_uid"], reason, r,
                          existing=snapshot, session_id=server_session_id, record_id=existing["id"],
                          student_id=r["student_id"], resolution="applied_incoming")
            out.append({"client_uid": r["client_uid"], "result": "created", "reason": reason,
                        "server_record_id": existing["id"]})
            counts["created"] += 1
        else:  # keep_existing -> never discard: store both versions for review
            _add_receipt(db, r["client_uid"], "attendance_record", user["id"], device_uid, existing["id"], "conflict", reason)
            _add_conflict(db, "record_conflict", user["id"], device_uid, r["client_uid"], reason, r,
                          existing=snapshot, session_id=server_session_id, record_id=existing["id"],
                          student_id=r["student_id"], resolution="needs_review")
            out.append({"client_uid": r["client_uid"], "result": "conflict", "reason": reason,
                        "server_record_id": existing["id"]})
            counts["conflict"] += 1

    _touch_device(db, user["id"], device_uid, user_agent)
    state = "synced" if counts["conflict"] == 0 and counts["rejected"] == 0 else "partial"
    return {"ok": True,
            "session": {"client_uid": p["client_uid"], "server_session_id": server_session_id,
                        "session_code": session_row["session_code"], "state": state,
                        "created": created_session},
            "records": out, "counts": counts}


# ----------------------------------------------------- administrator review
def resolve_conflict(db, conflict_id, admin_user_id, action, note=None):
    """action: 'apply' (incoming becomes the record, with a correction trail),
    'keep' (existing stays) or 'dismiss'. Returns True when something changed."""
    c = db.execute("SELECT * FROM sync_conflicts WHERE id = ?", (conflict_id,)).fetchone()
    if c is None or c["resolution"] != "needs_review":
        return False
    incoming = json.loads(c["incoming_json"])
    resolution = {"keep": "kept_existing", "dismiss": "dismissed"}.get(action)
    if action == "apply" and c["kind"] == "enrollment_conflict":
        from services import face_offline_service
        if not face_offline_service.apply_enrollment_conflict(db, incoming, admin_user_id):
            return False
        resolution = "applied_incoming"
    elif action == "apply":
        if c["record_id"] is None or incoming.get("status") not in ACCEPTED_STATUSES:
            return False
        reason = "Offline conflict reviewed by administrator" + (f": {note}" if note else ".")
        from services.attendance_service import correct_attendance
        if not correct_attendance(db, c["record_id"], incoming["status"], reason, admin_user_id):
            return False
        resolution = "applied_incoming"
    if resolution is None:
        return False
    db.execute(
        """UPDATE sync_conflicts SET resolution = ?, reviewed_by_user_id = ?, reviewed_at = datetime('now'),
                  review_note = ? WHERE id = ?""", (resolution, admin_user_id, note, conflict_id))
    db.commit()
    log_action("sync_conflict.resolved", "sync_conflict", conflict_id, new_value={"resolution": resolution})
    return True
