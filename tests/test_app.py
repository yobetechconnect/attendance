"""
End-to-end functional tests for the AL-ISTI'ANAH ACADEMY DAMATURU
attendance system.

Uses only Python's standard library `unittest` -- no extra
dependencies required to run:

    python -m unittest discover -s tests -v

Each test gets a fully isolated temp SQLite database and temp
face_data directory (nothing touches the real dev database), seeded
via the project's own seed.py. Tests hit real routes through Flask's
test client, backed by the real OpenCV face detection/LBPH pipeline --
nothing here is mocked. tests/fixtures/sample_face.jpg is a real,
single, detectable photographic face used to exercise enrollment and
recognition without requiring a live camera.
"""

import importlib
import io
import os
import re
import shutil
import sys
import tempfile
import unittest

import cv2
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DEV_PASSWORD = "DevPass123!"
FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "sample_face.jpg")


def get_csrf(html: bytes):
    m = re.search(rb'name="csrf_token"\s+value="([^"]+)"', html)
    if m:
        return m.group(1).decode()
    m = re.search(rb'csrfToken\s*=\s*"([^"]+)"', html)
    return m.group(1).decode() if m else None


def noisy_variant(img: np.ndarray, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noise = rng.integers(-6, 6, size=img.shape).astype(np.int16)
    return np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)


class AcademyTestCase(unittest.TestCase):
    """Base class: spins up a fully isolated app + DB for each test."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.face_dir = os.path.join(self.tmp_dir, "face_data")
        os.makedirs(self.face_dir, exist_ok=True)

        self._env_patch = {
            "FLASK_ENV": "development",
            "DATABASE_PATH": self.db_path,
            "FACE_SAMPLES_REQUIRED": "3",
            "SECRET_KEY": "test-secret-key",
        }
        self._old_env = {k: os.environ.get(k) for k in self._env_patch}
        os.environ.update(self._env_patch)

        import config as config_module
        importlib.reload(config_module)

        import app as app_module
        importlib.reload(app_module)
        self.flask_app = app_module.create_app()
        self.flask_app.config.update(TESTING=True, FACE_DATA_DIR=self.face_dir)

        from database.db import init_db
        with self.flask_app.app_context():
            init_db()

        import seed
        importlib.reload(seed)
        seed.run()

        self.client = self.flask_app.test_client()
        with open(FIXTURE_PATH, "rb") as f:
            self.face_bytes = f.read()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def db(self):
        from database.db import get_db
        return get_db()

    def app_ctx(self):
        return self.flask_app.app_context()

    def login(self, email, password=DEV_PASSWORD):
        r = self.client.get("/auth/login")
        token = get_csrf(r.data)
        return self.client.post(
            "/auth/login",
            data={"email": email, "password": password, "csrf_token": token},
            follow_redirects=True,
        )

    def csrf_from(self, path):
        return get_csrf(self.client.get(path).data)

    def enroll_face(self, student_id, face_img):
        token = self.csrf_from(f"/admin/biometrics/{student_id}")
        last = None
        for i in range(4):
            variant = noisy_variant(face_img, seed=100 + i)
            ok, buf = cv2.imencode(".jpg", variant)
            r = self.client.post(
                f"/admin/biometrics/{student_id}/capture",
                data={"csrf_token": token, "frame": (io.BytesIO(buf.tobytes()), f"e{i}.jpg")},
                content_type="multipart/form-data",
            )
            last = r.get_json()
        self.client.post(f"/admin/biometrics/{student_id}/finish", data={"csrf_token": token})
        return last

    def get_assignment_id(self):
        with self.app_ctx():
            return self.db().execute("SELECT id FROM subject_assignments LIMIT 1").fetchone()["id"]

    def get_student_id(self, admission_number="ALI/2026/0001"):
        with self.app_ctx():
            return self.db().execute(
                "SELECT id FROM students WHERE admission_number = ?", (admission_number,)
            ).fetchone()["id"]

    def start_session_as_teacher(self):
        """Logs in as the seeded teacher and starts the seeded subject
        assignment's attendance session. Returns the session id."""
        self.login("teacher@dev.al-istianah.local")
        assignment_id = self.get_assignment_id()
        token = self.csrf_from("/teacher/dashboard")
        r = self.client.post(
            "/teacher/sessions/start",
            data={"csrf_token": token, "subject_assignment_id": assignment_id},
            follow_redirects=True,
        )
        session_id = int(r.request.path.rsplit("/", 1)[-1])
        return session_id


class TestAuthAndRBAC(AcademyTestCase):

    def test_login_rejects_wrong_password(self):
        r = self.login("admin@dev.al-istianah.local", "wrong-password")
        self.assertIn(b"Incorrect email or password", r.data)

    def test_login_succeeds_and_reaches_real_dashboard(self):
        r = self.login("admin@dev.al-istianah.local")
        self.assertIn(b"Total Students", r.data)

    def test_deactivated_account_blocked_at_login(self):
        with self.app_ctx():
            db = self.db()
            db.execute("UPDATE users SET is_active = 0 WHERE email = 'student2@dev.al-istianah.local'")
            db.commit()
        r = self.login("student2@dev.al-istianah.local")
        self.assertIn(b"deactivated", r.data)

    def test_account_locks_after_repeated_failed_logins(self):
        for _ in range(5):
            self.login("admin@dev.al-istianah.local", "wrong-password")
        r = self.login("admin@dev.al-istianah.local", "wrong-password")
        self.assertIn(b"Too many failed attempts", r.data)

    def test_student_cannot_access_admin_dashboard(self):
        self.login("student1@dev.al-istianah.local")
        r = self.client.get("/admin/")
        self.assertEqual(r.status_code, 403)

    def test_teacher_cannot_access_admin_dashboard(self):
        self.login("teacher@dev.al-istianah.local")
        r = self.client.get("/admin/")
        self.assertEqual(r.status_code, 403)

    def test_teacher_cannot_start_session_for_unassigned_class(self):
        self.login("teacher@dev.al-istianah.local")
        token = self.csrf_from("/teacher/dashboard")
        r = self.client.post(
            "/teacher/sessions/start",
            data={"csrf_token": token, "subject_assignment_id": 999999},
            follow_redirects=True,
        )
        self.assertIn(b"not assigned to you", r.data)

    def test_logout_clears_session(self):
        self.login("admin@dev.al-istianah.local")
        self.client.get("/auth/logout")
        r = self.client.get("/admin/")
        self.assertEqual(r.status_code, 302)

    def test_post_without_csrf_token_rejected(self):
        self.login("admin@dev.al-istianah.local")
        r = self.client.post(
            "/admin/students",
            data={
                "first_name": "No", "surname": "Csrf",
                "admission_number": "DEV/900", "class_id": "1",
            },
        )
        self.assertEqual(r.status_code, 400)


class TestAdminCRUD(AcademyTestCase):

    def test_admin_can_create_student(self):
        self.login("admin@dev.al-istianah.local")
        with self.app_ctx():
            class_id = self.db().execute("SELECT id FROM classes LIMIT 1").fetchone()["id"]
        token = self.csrf_from("/admin/students")
        self.client.post(
            "/admin/students",
            data={
                "csrf_token": token, "first_name": "Ada", "surname": "Newstudent",
                "admission_number": "ALI/2026/0099", "class_id": class_id,
            },
            follow_redirects=True,
        )
        with self.app_ctx():
            row = self.db().execute(
                "SELECT * FROM students WHERE admission_number = 'ALI/2026/0099'"
            ).fetchone()
        self.assertIsNotNone(row)

    def test_admin_can_create_teacher_and_assign_subject(self):
        self.login("admin@dev.al-istianah.local")
        token = self.csrf_from("/admin/teachers")
        self.client.post(
            "/admin/teachers",
            data={"csrf_token": token, "full_name": "New Teacher", "email": "newteacher@dev.al-istianah.local",
                  "staff_id": "DEV-STAFF-099"},
            follow_redirects=True,
        )
        with self.app_ctx():
            teacher = self.db().execute(
                "SELECT t.id FROM teachers t JOIN users u ON u.id=t.user_id WHERE u.email = ?",
                ("newteacher@dev.al-istianah.local",),
            ).fetchone()
        self.assertIsNotNone(teacher)

    def test_admin_can_create_class_and_subject(self):
        self.login("admin@dev.al-istianah.local")
        token = self.csrf_from("/admin/classes")
        self.client.post("/admin/classes", data={"csrf_token": token, "name": "JSS 3B"}, follow_redirects=True)
        with self.app_ctx():
            row = self.db().execute("SELECT id FROM classes WHERE name = 'JSS 3B'").fetchone()
        self.assertIsNotNone(row)

        token = self.csrf_from("/admin/subjects")
        self.client.post(
            "/admin/subjects",
            data={"csrf_token": token, "code": "QURAN101", "name": "Qur'an Memorization", "category": "QURANIC"},
            follow_redirects=True,
        )
        with self.app_ctx():
            row = self.db().execute("SELECT id FROM subjects WHERE code = 'QURAN101'").fetchone()
        self.assertIsNotNone(row)

    def test_bulk_import_reports_invalid_class_and_creates_valid_rows(self):
        self.login("admin@dev.al-istianah.local")
        with self.app_ctx():
            class_name = self.db().execute("SELECT name FROM classes LIMIT 1").fetchone()["name"]
        csv_text = (
            "admission_number,first_name,middle_name,surname,gender,date_of_birth,phone,address,class_name\n"
            f"ALI/2026/0501,Zainab,,Umar,female,,,,{class_name}\n"
            "ALI/2026/0502,Bad,,Row,female,,,,Nonexistent Class\n"
        )
        token = self.csrf_from("/admin/students/import")
        r = self.client.post(
            "/admin/students/import",
            data={"csrf_token": token, "csv_file": (io.BytesIO(csv_text.encode()), "import.csv")},
            follow_redirects=True,
        )
        self.assertEqual(r.status_code, 200)
        with self.app_ctx():
            ok = self.db().execute("SELECT id FROM students WHERE admission_number='ALI/2026/0501'").fetchone()
            bad = self.db().execute("SELECT id FROM students WHERE admission_number='ALI/2026/0502'").fetchone()
        self.assertIsNotNone(ok)
        self.assertIsNone(bad)


class TestFaceEnrollmentAndRecognition(AcademyTestCase):

    def test_enrollment_and_recognition_records_attendance_and_blocks_duplicates(self):
        student_id = self.get_student_id()
        self.login("admin@dev.al-istianah.local")
        arr = np.frombuffer(self.face_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        self.enroll_face(student_id, img)
        self.client.get("/auth/logout")

        session_id = self.start_session_as_teacher()
        scan_token = self.csrf_from(f"/teacher/scanner/{session_id}")

        ok, buf = cv2.imencode(".jpg", img)
        r = self.client.post(
            f"/teacher/scanner/{session_id}/recognize",
            data={"csrf_token": scan_token, "frame": (io.BytesIO(buf.tobytes()), "scan1.jpg")},
            content_type="multipart/form-data",
        )
        data = r.get_json()
        self.assertEqual(data["state"], "RECORDED")
        self.assertEqual(data["admission_number"], "ALI/2026/0001")
        self.assertEqual(data["counts"]["present"] + data["counts"]["late"], 1)   # live counters for the scanner UI

        variant = noisy_variant(img, seed=7)
        ok, buf = cv2.imencode(".jpg", variant)
        r2 = self.client.post(
            f"/teacher/scanner/{session_id}/recognize",
            data={"csrf_token": scan_token, "frame": (io.BytesIO(buf.tobytes()), "scan2.jpg")},
            content_type="multipart/form-data",
        )
        self.assertEqual(r2.get_json()["state"], "ALREADY_RECORDED")

        with self.app_ctx():
            count = self.db().execute(
                "SELECT COUNT(*) c FROM attendance_records WHERE session_id = ? AND student_id = ?",
                (session_id, student_id),
            ).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_recognition_no_face_returns_waiting_not_attendance(self):
        session_id = self.start_session_as_teacher()
        scan_token = self.csrf_from(f"/teacher/scanner/{session_id}")
        noise = np.random.randint(0, 255, (300, 300, 3), dtype=np.uint8)
        ok, buf = cv2.imencode(".jpg", noise)
        r = self.client.post(
            f"/teacher/scanner/{session_id}/recognize",
            data={"csrf_token": scan_token, "frame": (io.BytesIO(buf.tobytes()), "noface.jpg")},
            content_type="multipart/form-data",
        )
        self.assertEqual(r.get_json()["state"], "WAITING")

        with self.app_ctx():
            count = self.db().execute(
                "SELECT COUNT(*) c FROM attendance_records WHERE session_id = ?", (session_id,)
            ).fetchone()["c"]
        self.assertEqual(count, 0)


class TestManualAttendanceAndReports(AcademyTestCase):

    def test_manual_attendance_only_offers_students_in_that_class(self):
        session_id = self.start_session_as_teacher()
        r = self.client.get(f"/teacher/sessions/{session_id}/manual")
        self.assertIn(b"ALI/2026/0001", r.data)

    def test_manual_attendance_and_session_close(self):
        session_id = self.start_session_as_teacher()
        student_id = self.get_student_id()
        token = self.csrf_from(f"/teacher/sessions/{session_id}/manual")
        self.client.post(
            f"/teacher/sessions/{session_id}/manual",
            data={"csrf_token": token, "student_id": student_id},
            follow_redirects=True,
        )
        with self.app_ctx():
            record = self.db().execute(
                "SELECT * FROM attendance_records WHERE session_id = ? AND student_id = ?",
                (session_id, student_id),
            ).fetchone()
        self.assertIsNotNone(record)
        self.assertEqual(record["verification_method"], "manual")

        close_token = self.csrf_from(f"/teacher/scanner/{session_id}")
        self.client.post(f"/teacher/sessions/{session_id}/close", data={"csrf_token": close_token},
                          follow_redirects=True)
        with self.app_ctx():
            session_row = self.db().execute(
                "SELECT status FROM attendance_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        self.assertEqual(session_row["status"], "closed")

    def test_manual_attendance_creates_audit_log_entry(self):
        session_id = self.start_session_as_teacher()
        student_id = self.get_student_id()
        token = self.csrf_from(f"/teacher/sessions/{session_id}/manual")
        self.client.post(
            f"/teacher/sessions/{session_id}/manual",
            data={"csrf_token": token, "student_id": student_id},
            follow_redirects=True,
        )
        with self.app_ctx():
            entry = self.db().execute(
                "SELECT * FROM audit_logs WHERE action LIKE 'attendance%' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(entry)

    def test_class_report_and_csv_export_match_database(self):
        session_id = self.start_session_as_teacher()
        student_id = self.get_student_id()
        token = self.csrf_from(f"/teacher/sessions/{session_id}/manual")
        self.client.post(
            f"/teacher/sessions/{session_id}/manual",
            data={"csrf_token": token, "student_id": student_id},
            follow_redirects=True,
        )
        self.client.get("/auth/logout")

        self.login("admin@dev.al-istianah.local")
        with self.app_ctx():
            class_id = self.db().execute(
                "SELECT class_id FROM subject_assignments LIMIT 1"
            ).fetchone()["class_id"]

        r = self.client.get(f"/reports/class/{class_id}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"ALI/2026/0001", r.data)

        r_csv = self.client.get("/reports/records/export.csv")
        self.assertEqual(r_csv.status_code, 200)
        self.assertIn("csv", r_csv.content_type)
        self.assertIn(b"admission_number", r_csv.data)

        with self.app_ctx():
            db = self.db()
            attended = db.execute(
                "SELECT COUNT(*) c FROM attendance_records WHERE student_id = ?", (student_id,)
            ).fetchone()["c"]
        self.assertEqual(attended, 1)


class TestStudentDashboard(AcademyTestCase):

    def test_student_dashboard_shows_real_attendance_percentage(self):
        session_id = self.start_session_as_teacher()
        student_id = self.get_student_id()
        token = self.csrf_from(f"/teacher/sessions/{session_id}/manual")
        self.client.post(
            f"/teacher/sessions/{session_id}/manual",
            data={"csrf_token": token, "student_id": student_id},
            follow_redirects=True,
        )
        self.client.get("/auth/logout")

        r = self.login("student1@dev.al-istianah.local")
        self.assertIn(b"100.0%", r.data)


if __name__ == "__main__":
    unittest.main()
