"""
Biometric enrollment (Section 39). Kept deliberately thin: this route
layer only orchestrates capture/train/remove — all OpenCV logic stays
in services/face_detection.py and services/face_recognition.py so the
recognition engine can be swapped later without touching this file.
"""

from flask import (Blueprint, current_app, flash, g, redirect,
                    render_template, request, url_for)

from database.db import get_db
from services import face_detection
from services.face_recognition import FaceRecognitionService
from utils.audit import log_action
from utils.decorators import role_required

bp = Blueprint("admin_biometrics", __name__, url_prefix="/admin/biometrics")
ADMIN_ROLES = ("super_admin",)   # only the Super Administrator is unrestricted


def _recognizer(app):
    return FaceRecognitionService(app.config["FACE_DATA_DIR"], app.config["FACE_RECOGNITION_THRESHOLD"])


@bp.route("/")
@role_required(*ADMIN_ROLES)
def enrollment_list():
    db = get_db()
    q = request.args.get("q", "").strip()
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except ValueError:
        page = 1
    per_page = 50
    where, params = "WHERE st.status = 'active'", []
    if q:
        where += " AND (st.first_name LIKE ? OR st.surname LIKE ? OR st.admission_number LIKE ?)"
        params = [f"%{q}%"] * 3
    total = db.execute(f"SELECT COUNT(*) c FROM students st {where}", params).fetchone()["c"]
    rows = db.execute(
        f"""SELECT st.id, st.admission_number, st.first_name, st.surname,
                   bp.sample_count, bp.is_active AS profile_active
            FROM students st
            LEFT JOIN biometric_profiles bp ON bp.student_id = st.id
            {where}
            ORDER BY st.surname, st.first_name LIMIT ? OFFSET ?""",
        params + [per_page, (page - 1) * per_page],
    ).fetchall()
    return render_template("admin/face_enrollment_list.html", students=rows, q=q, page=page,
                           per_page=per_page, total=total)


@bp.route("/<int:student_id>")
@role_required(*ADMIN_ROLES)
def enrollment_capture(student_id):
    db = get_db()
    student = db.execute(
        "SELECT * FROM students WHERE id = ?", (student_id,)
    ).fetchone()
    if student is None:
        flash("Student not found.", "error")
        return redirect(url_for("admin_biometrics.enrollment_list"))

    recognizer = _recognizer(current_app)
    current_samples = recognizer.sample_count(student_id)
    required = current_app.config["FACE_SAMPLES_REQUIRED"]
    return render_template("admin/face_enrollment_capture.html", student=student,
                            current_samples=current_samples, required_samples=required)


@bp.route("/<int:student_id>/capture", methods=("POST",))
@role_required(*ADMIN_ROLES)
def enrollment_submit_sample(student_id):
    db = get_db()
    student = db.execute("SELECT id FROM students WHERE id = ?", (student_id,)).fetchone()
    if student is None:
        return {"ok": False, "error": "Student not found."}, 404

    file = request.files.get("frame")
    if file is None or file.filename == "":
        return {"ok": False, "error": "No frame uploaded."}, 400

    try:
        image = face_detection.decode_image(file.read())
        face_crop = face_detection.extract_single_face(image)
    except face_detection.NoFaceDetected:
        return {"ok": False, "error": "No face detected. Please face the camera directly."}, 200
    except face_detection.MultipleFacesDetected:
        return {"ok": False, "error": "More than one face detected. Only one person should be in frame."}, 200
    except ValueError:
        return {"ok": False, "error": "Could not read the captured frame."}, 400

    recognizer = _recognizer(current_app)
    index = recognizer.sample_count(student_id)
    recognizer.save_sample(student_id, face_crop, index)
    new_count = recognizer.sample_count(student_id)
    required = current_app.config["FACE_SAMPLES_REQUIRED"]
    return {"ok": True, "sample_count": new_count, "required": required, "complete": new_count >= required}


@bp.route("/<int:student_id>/finish", methods=("POST",))
@role_required(*ADMIN_ROLES)
def enrollment_finish(student_id):
    db = get_db()
    student = db.execute("SELECT id FROM students WHERE id = ?", (student_id,)).fetchone()
    if student is None:
        flash("Student not found.", "error")
        return redirect(url_for("admin_biometrics.enrollment_list"))

    recognizer = _recognizer(current_app)
    sample_count = recognizer.sample_count(student_id)
    if sample_count == 0:
        flash("No face samples were captured — cannot enroll.", "error")
        return redirect(url_for("admin_biometrics.enrollment_capture", student_id=student_id))

    existing = db.execute("SELECT id FROM biometric_profiles WHERE student_id = ?", (student_id,)).fetchone()
    if existing:
        db.execute(
            "UPDATE biometric_profiles SET sample_count = ?, is_active = 1, updated_at = datetime('now') WHERE student_id = ?",
            (sample_count, student_id),
        )
    else:
        db.execute("INSERT INTO biometric_profiles (student_id, sample_count, is_active) VALUES (?, ?, 1)",
                   (student_id, sample_count))
    db.commit()

    active_ids = [r["student_id"] for r in db.execute(
        "SELECT student_id FROM biometric_profiles WHERE is_active = 1").fetchall()]
    try:
        n_faces, n_people = recognizer.train_all(active_ids)
        log_action("biometric.enroll", "student", student_id, new_value={"sample_count": sample_count})
        flash(f"Face enrollment complete. Model retrained on {n_faces} samples across {n_people} students.", "success")
    except ValueError as e:
        flash(str(e), "error")

    return redirect(url_for("admin_people.student_view", student_id=student_id))


@bp.route("/<int:student_id>/remove", methods=("POST",))
@role_required(*ADMIN_ROLES)
def enrollment_remove(student_id):
    db = get_db()
    recognizer = _recognizer(current_app)
    recognizer.delete_student_samples(student_id)
    db.execute("DELETE FROM biometric_profiles WHERE student_id = ?", (student_id,))
    db.commit()
    log_action("biometric.remove", "student", student_id)

    active_ids = [r["student_id"] for r in db.execute(
        "SELECT student_id FROM biometric_profiles WHERE is_active = 1").fetchall()]
    if active_ids:
        try:
            recognizer.train_all(active_ids)
        except ValueError:
            pass

    flash("Face profile removed.", "success")
    return redirect(url_for("admin_people.student_view", student_id=student_id))
