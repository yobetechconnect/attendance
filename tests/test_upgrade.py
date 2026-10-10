"""
Tests for the production upgrade: roles/permissions, temporary passwords,
Parent Portal isolation, logo URL, SQL portability, backup/restore, and
concurrency-safe attendance. Runs on SQLite (PostgreSQL is not available in
the build sandbox — see the portability tests and DEPLOY notes).
"""

import html
import os
import re
import sys
import tarfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_app import AcademyTestCase, DEV_PASSWORD, get_csrf  # noqa: E402

ADMIN = "admin@dev.al-istianah.local"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def extract_creds(resp_data):
    """Pull (username, password) pairs out of the credentials dialog HTML."""
    text = resp_data.decode()
    users = [html.unescape(u) for u in re.findall(r'id="credUser\d+">(.*?)</code>', text)]
    pws = [html.unescape(p) for p in re.findall(r'id="credPass\d+">(.*?)</code>', text)]
    return list(zip(users, pws))


class UpgradeCase(AcademyTestCase):
    def post(self, path, data=None, token_from=None, **kw):
        token = self.csrf_from(token_from or path)
        d = dict(data or {})
        d["csrf_token"] = token
        return self.client.post(path, data=d, **kw)

    def logout(self):
        self.client.get("/auth/logout")

    def make_temp_admin(self, slot, email):
        self.login(ADMIN)
        r = self.post(f"/admin/temp-admins/{slot}/create", {"full_name": f"Temp Admin {slot}", "email": email},
                      token_from="/admin/temp-admins")
        creds = extract_creds(r.data)
        self.logout()
        return creds[0]

    def set_password(self, email, current, new):
        self.login(email, current)
        r = self.post("/auth/change_password".replace("_", "-"), {
            "current_password": current, "new_password": new, "confirm_password": new})
        return r


class TestTemporaryAdmins(UpgradeCase):

    def test_super_admin_creates_both_and_dialog_shows_copyable_password(self):
        self.login(ADMIN)
        r = self.post("/admin/temp-admins/1/create", {"full_name": "Hauwa A", "email": "ta1@school.test"},
                      token_from="/admin/temp-admins")
        self.assertIn(b"Copy Password", r.data)
        self.assertIn(b"created successfully", r.data)
        ((user, pw),) = extract_creds(r.data)
        self.assertEqual(user, "ta1@school.test")
        self.assertGreaterEqual(len(pw), 12)
        # second slot works, third does not exist
        r = self.post("/admin/temp-admins/2/create", {"full_name": "Idris B", "email": "ta2@school.test"},
                      token_from="/admin/temp-admins")
        self.assertEqual(len(extract_creds(r.data)), 1)
        self.assertEqual(self.client.post("/admin/temp-admins/3/create",
                                          data={"csrf_token": self.csrf_from("/admin/temp-admins"),
                                                "full_name": "x", "email": "x@x.test"}).status_code, 404)

    def test_password_is_hashed_never_stored_plain_and_not_in_audit_log(self):
        user, pw = self.make_temp_admin(1, "ta1@school.test")
        with self.app_ctx():
            db = self.db()
            row = db.execute("SELECT password_hash FROM users WHERE email = ?", (user,)).fetchone()
            self.assertNotIn(pw, row["password_hash"])
            self.assertTrue(row["password_hash"].startswith(("scrypt:", "pbkdf2:")))
            dump = " ".join(str(dict(r)) for r in db.execute("SELECT * FROM audit_logs").fetchall())
            self.assertNotIn(pw, dump)

    def test_first_login_forces_password_change(self):
        user, pw = self.make_temp_admin(1, "ta1@school.test")
        r = self.login(user, pw)
        self.assertIn(b"Change Password", r.data)
        # everything else is blocked until the change is made
        r = self.client.get("/admin/students", follow_redirects=True)
        self.assertIn(b"Current password", r.data)
        r = self.post("/auth/change-password", {"current_password": pw, "new_password": "BrandNew-Pass7",
                                                 "confirm_password": "BrandNew-Pass7"}, follow_redirects=True)
        self.assertIn(b"Add Student", r.data)

    def test_temp_admin_can_manage_students_but_not_restricted_areas(self):
        user, pw = self.make_temp_admin(1, "ta1@school.test")
        self.login(user, pw)
        self.post("/auth/change-password", {"current_password": pw, "new_password": "BrandNew-Pass7",
                                            "confirm_password": "BrandNew-Pass7"})
        self.assertEqual(self.client.get("/admin/students").status_code, 200)
        sid = self.get_student_id()
        self.assertEqual(self.client.get(f"/admin/students/{sid}").status_code, 200)
        self.assertEqual(self.client.get(f"/admin/students/{sid}/edit").status_code, 200)
        self.assertEqual(self.client.get("/admin/students/import").status_code, 200)
        forbidden = ["/admin/", "/admin/teachers", "/admin/settings", "/admin/audit-log", "/admin/classes",
                     "/admin/temp-admins", "/admin/parents", "/admin/biometrics/", "/admin/devices/",
                     "/admin/backup", f"/admin/biometrics/{sid}", f"/reports/student/{sid}", "/reports/sessions",
                     "/admin/subjects", "/admin/sections", "/admin/sync", "/admin/notifications"]
        for path in forbidden:
            code = self.client.get(path).status_code
            self.assertIn(code, (403, 404), f"{path} -> {code}")
            self.assertNotEqual(code, 200, path)
        # state-changing restricted routes are blocked too
        tok = self.csrf_from("/admin/students")
        for path in ("/admin/teachers", "/admin/temp-admins/1/reset-password", "/admin/temp-admins/policy",
                     "/admin/parents", f"/admin/students/{sid}/status", "/admin/settings"):
            self.assertEqual(self.client.post(path, data={"csrf_token": tok}).status_code, 403, path)
        # the attempts were audit-logged under the temp admin's own account
        with self.app_ctx():
            n = self.db().execute(
                "SELECT COUNT(*) c FROM audit_logs al JOIN users u ON u.id = al.user_id "
                "WHERE al.action = 'security.access_denied' AND u.email = ?", (user,)).fetchone()["c"]
        self.assertGreater(n, 5)

    def test_temp_admin_cannot_create_administrators_or_change_roles(self):
        user, pw = self.make_temp_admin(1, "ta1@school.test")
        self.login(user, pw)
        self.post("/auth/change-password", {"current_password": pw, "new_password": "BrandNew-Pass7",
                                            "confirm_password": "BrandNew-Pass7"})
        tok = self.csrf_from("/admin/students")
        r = self.client.post("/admin/temp-admins/2/create", data={"csrf_token": tok, "full_name": "Evil", "email": "evil@x.test"})
        self.assertEqual(r.status_code, 403)
        with self.app_ctx():
            self.assertIsNone(self.db().execute("SELECT 1 FROM users WHERE email = 'evil@x.test'").fetchone())

    def test_temp_admin_registers_student_with_copyable_credentials_and_student_must_change(self):
        user, pw = self.make_temp_admin(1, "ta1@school.test")
        self.login(user, pw)
        self.post("/auth/change-password", {"current_password": pw, "new_password": "BrandNew-Pass7",
                                            "confirm_password": "BrandNew-Pass7"})
        with self.app_ctx():
            class_id = self.db().execute("SELECT id FROM classes LIMIT 1").fetchone()["id"]
        r = self.post("/admin/students", {"first_name": "Zainab", "surname": "Lawal", "admission_number": "ALI/2026/0100",
                                          "class_id": class_id}, token_from="/admin/students")
        self.assertIn(b"Student account created successfully", r.data)
        self.assertIn(b"Copy Password", r.data)
        self.assertIn(b"Zainab Lawal", r.data)
        self.assertIn(b"ALI/2026/0100", r.data)
        ((suser, spw),) = extract_creds(r.data)
        # no plaintext anywhere durable: a fresh GET of the list does not show the password again
        self.assertNotIn(spw.encode(), self.client.get("/admin/students").data)
        self.assertNotIn(spw.encode(), self.client.get("/admin/students").headers.get("Set-Cookie", "").encode())
        # actions are attributable
        with self.app_ctx():
            row = self.db().execute(
                "SELECT u.email FROM audit_logs al JOIN users u ON u.id = al.user_id WHERE al.action = 'student.create'").fetchone()
            self.assertEqual(row["email"], user)
        self.logout()
        r = self.login(suser, spw)
        self.assertIn(b"Current password", r.data)            # forced change
        # students can also sign in with their admission number
        self.logout()
        r = self.login("ALI/2026/0100", spw)
        self.assertIn(b"Current password", r.data)

    def test_second_registration_does_not_reuse_or_leak_previous_password(self):
        self.login(ADMIN)
        with self.app_ctx():
            class_id = self.db().execute("SELECT id FROM classes LIMIT 1").fetchone()["id"]
        pws = []
        for i in (1, 2):
            r = self.post("/admin/students", {"first_name": f"Kid{i}", "surname": "Test", "admission_number": f"ALI/2026/02{i}",
                                              "class_id": class_id}, token_from="/admin/students")
            pws.append(extract_creds(r.data)[0][1])
        self.assertNotEqual(pws[0], pws[1])
        self.assertEqual(len(extract_creds(r.data)), 1)          # only the new one is shown

    def test_reset_by_temp_admin_requires_super_admin_policy_and_invalidates_old_password(self):
        user, pw = self.make_temp_admin(1, "ta1@school.test")
        sid = self.get_student_id()
        self.login(user, pw)
        self.post("/auth/change-password", {"current_password": pw, "new_password": "BrandNew-Pass7",
                                            "confirm_password": "BrandNew-Pass7"})
        tok = self.csrf_from("/admin/students")
        self.assertEqual(self.client.post(f"/admin/students/{sid}/reset-password", data={"csrf_token": tok}).status_code, 403)
        self.logout()
        self.login(ADMIN)
        self.post("/admin/temp-admins/policy", {"temp_admin_can_reset_passwords": "on"}, token_from="/admin/temp-admins")
        self.logout()
        self.login(user, "BrandNew-Pass7")
        r = self.post(f"/admin/students/{sid}/reset-password", token_from="/admin/students")
        ((suser, new_pw),) = extract_creds(r.data)
        self.assertEqual(suser, "student1@dev.al-istianah.local")
        self.logout()
        self.assertIn(b"Incorrect email or password", self.login(suser, DEV_PASSWORD).data)   # old password dead
        self.logout()
        self.assertIn(b"Current password", self.login(suser, new_pw).data)

    def test_super_admin_resets_and_deactivates_temp_admin_and_live_session_dies(self):
        user, pw = self.make_temp_admin(2, "ta2@school.test")
        # temp admin signs in (session 1)
        temp_client = self.flask_app.test_client()
        t = get_csrf(temp_client.get("/auth/login").data)
        temp_client.post("/auth/login", data={"email": user, "password": pw, "csrf_token": t})
        self.assertEqual(temp_client.get("/auth/change-password").status_code, 200)
        # super admin resets credentials
        self.login(ADMIN)
        r = self.post("/admin/temp-admins/2/reset-password", token_from="/admin/temp-admins")
        ((_, new_pw),) = extract_creds(r.data)
        self.assertNotEqual(new_pw, pw)
        # the temp admin's existing session is ended immediately
        self.assertEqual(temp_client.get("/auth/change-password").status_code, 302)
        self.post("/admin/temp-admins/2/toggle-active", token_from="/admin/temp-admins")
        self.logout()
        self.assertIn(b"deactivated", self.login(user, new_pw).data)

    def test_super_admin_retains_full_access(self):
        self.login(ADMIN)
        for path in ("/admin/", "/admin/students", "/admin/teachers", "/admin/settings", "/admin/audit-log",
                     "/admin/classes", "/admin/temp-admins", "/admin/parents", "/admin/biometrics/", "/admin/backup",
                     "/admin/devices/", "/reports/sessions"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_legacy_school_admin_role_has_no_privileges(self):
        with self.app_ctx():
            from werkzeug.security import generate_password_hash
            db = self.db()
            db.execute("INSERT INTO users (email, password_hash, role, full_name) VALUES ('old@school.test', ?, 'school_admin', 'Old')",
                       (generate_password_hash("Whatever123"),))
            db.commit()
        r = self.login("old@school.test", "Whatever123")
        self.assertIn(b"no longer supported", r.data)


class TestTeacherCredentials(UpgradeCase):
    def test_teacher_creation_dialog_and_forced_change_and_reset(self):
        self.login(ADMIN)
        r = self.post("/admin/teachers", {"full_name": "Malam Sani", "email": "sani@school.test", "staff_id": "T-900"},
                      token_from="/admin/teachers")
        self.assertIn(b"Teacher account created successfully", r.data)
        self.assertIn(b"Copy Password", r.data)
        self.assertIn(b"T-900", r.data)
        ((tuser, tpw),) = extract_creds(r.data)
        # teacher record stays in the list afterwards
        self.assertIn(b"Malam Sani", self.client.get("/admin/teachers").data)
        # reset issues a new password; old one is invalid
        with self.app_ctx():
            tid = self.db().execute("SELECT id FROM teachers WHERE staff_id = 'T-900'").fetchone()["id"]
        r = self.post(f"/admin/teachers/{tid}/reset-password", token_from="/admin/teachers")
        ((_, tpw2),) = extract_creds(r.data)
        self.logout()
        self.assertIn(b"Incorrect email or password", self.login(tuser, tpw).data)
        self.logout()
        self.assertIn(b"Current password", self.login(tuser, tpw2).data)


class TestParentPortal(UpgradeCase):

    def make_parent(self, name, email, admissions):
        self.login(ADMIN)
        r = self.post("/admin/parents", {"full_name": name, "email": email, "admission_numbers": admissions},
                      token_from="/admin/parents")
        ((_, pw),) = extract_creds(r.data)
        self.logout()
        return pw

    def parent_login(self, email, pw, new="Parent-Pass-77"):
        self.login(email, pw)
        self.post("/auth/change-password", {"current_password": pw, "new_password": new, "confirm_password": new})
        return new

    def record(self, student_id, status="present"):
        with self.app_ctx():
            db = self.db()
            sess = self.start_session_id(db)
            db.execute("INSERT INTO attendance_records (session_id, student_id, status, verification_method) VALUES (?, ?, ?, 'manual')",
                       (sess, student_id, status))
            db.commit()

    def start_session_id(self, db):
        row = db.execute("SELECT id FROM attendance_sessions LIMIT 1").fetchone()
        if row:
            return row["id"]
        aid = db.execute("SELECT id FROM subject_assignments LIMIT 1").fetchone()["id"]
        return db.execute("INSERT INTO attendance_sessions (subject_assignment_id, session_code, session_date, scheduled_start, status) VALUES (?, 'TEST-1', '2026-10-05', '2026-10-05 08:00:00', 'closed')",
                          (aid,)).lastrowid

    def test_parent_sees_only_linked_children_and_other_families_are_isolated(self):
        s1, s2, s3 = (self.get_student_id(f"ALI/2026/000{i}") for i in (1, 2, 3))
        self.record(s1, "present"); self.record(s2, "absent")
        pw_a = self.make_parent("Parent A", "a@family.test", "ALI/2026/0001, ALI/2026/0003")   # two children
        pw_b = self.make_parent("Parent B", "b@family.test", "ALI/2026/0002")
        self.parent_login("a@family.test", pw_a)
        r = self.client.get("/parent/")
        self.assertIn(b"Ahmed Bello", r.data)
        self.assertIn(b"Yusuf Garba", r.data)
        self.assertNotIn(b"Fatima Usman", r.data)
        self.assertEqual(self.client.get(f"/parent/children/{s1}").status_code, 200)
        self.assertEqual(self.client.get(f"/parent/children/{s3}").status_code, 200)
        # URL / id tampering: another family's child (and a nonexistent one) look identical
        self.assertEqual(self.client.get(f"/parent/children/{s2}").status_code, 404)
        self.assertEqual(self.client.get("/parent/children/99999").status_code, 404)
        self.assertEqual(self.client.get(f"/parent/children/{s2}?status=absent&page=1").status_code, 404)
        # parents have no admin access at all
        for path in ("/admin/students", f"/admin/students/{s2}", "/admin/parents", "/reports/sessions", f"/reports/student/{s2}"):
            self.assertIn(self.client.get(path).status_code, (403, 404), path)
        # the tampering attempt is audit-logged
        with self.app_ctx():
            n = self.db().execute("SELECT COUNT(*) c FROM audit_logs WHERE action = 'security.parent_access_denied'").fetchone()["c"]
        self.assertGreaterEqual(n, 1)

    def test_parent_b_cannot_see_parent_a_child_and_sees_own_attendance(self):
        s1, s2 = self.get_student_id("ALI/2026/0001"), self.get_student_id("ALI/2026/0002")
        self.record(s2, "absent")
        self.make_parent("Parent A", "a@family.test", "ALI/2026/0001")
        pw_b = self.make_parent("Parent B", "b@family.test", "ALI/2026/0002")
        self.parent_login("b@family.test", pw_b)
        self.assertEqual(self.client.get(f"/parent/children/{s1}").status_code, 404)
        r = self.client.get(f"/parent/children/{s2}")
        self.assertIn(b"Absent", r.data)
        self.assertNotIn(b"Ahmed", r.data)

    def test_unlinking_removes_access_immediately(self):
        s1 = self.get_student_id()
        pw = self.make_parent("Parent A", "a@family.test", "ALI/2026/0001")
        self.parent_login("a@family.test", pw)
        self.assertEqual(self.client.get(f"/parent/children/{s1}").status_code, 200)
        other = self.flask_app.test_client()
        t = get_csrf(other.get("/auth/login").data)
        other.post("/auth/login", data={"email": ADMIN, "password": DEV_PASSWORD, "csrf_token": t})
        with self.app_ctx():
            pid = self.db().execute("SELECT id FROM parents").fetchone()["id"]
        t = get_csrf(other.get(f"/admin/parents/{pid}").data)
        other.post(f"/admin/parents/{pid}/unlink/{s1}", data={"csrf_token": t})
        self.assertEqual(self.client.get(f"/parent/children/{s1}").status_code, 404)

    def test_registration_creates_parent_and_links_sibling_to_same_parent(self):
        self.login(ADMIN)
        with self.app_ctx():
            class_id = self.db().execute("SELECT id FROM classes LIMIT 1").fetchone()["id"]
        base = {"class_id": class_id, "parent_name": "Alhaji Musa", "parent_email": "musa@family.test"}
        r = self.post("/admin/students", {**base, "first_name": "A", "surname": "Musa", "admission_number": "ALI/2026/0301"},
                      token_from="/admin/students")
        self.assertEqual(len(extract_creds(r.data)), 2)       # student + new parent login
        r = self.post("/admin/students", {**base, "first_name": "B", "surname": "Musa", "admission_number": "ALI/2026/0302"},
                      token_from="/admin/students")
        self.assertEqual(len(extract_creds(r.data)), 1)       # sibling: parent reused, no new login
        with self.app_ctx():
            db = self.db()
            self.assertEqual(db.execute("SELECT COUNT(*) c FROM parents").fetchone()["c"], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) c FROM parent_students").fetchone()["c"], 2)

    def test_failed_registration_rolls_back_everything(self):
        self.login(ADMIN)
        with self.app_ctx():
            class_id = self.db().execute("SELECT id FROM classes LIMIT 1").fetchone()["id"]
            before = self.db().execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        # duplicate admission number
        self.post("/admin/students", {"first_name": "Dup", "surname": "Licate", "admission_number": "ALI/2026/0001",
                                      "class_id": class_id, "parent_name": "P", "parent_email": "p@dup.test"},
                  token_from="/admin/students", follow_redirects=True)
        with self.app_ctx():
            after = self.db().execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
            self.assertEqual(before, after)    # no orphan student user, no orphan parent

    def test_parent_by_phone_login(self):
        self.login(ADMIN)
        r = self.post("/admin/parents", {"full_name": "Mama Zara", "phone": "0803 111 2222"}, token_from="/admin/parents")
        ((user, pw),) = extract_creds(r.data)
        self.logout()
        self.assertIn(b"Current password", self.login("08031112222", pw).data)


class TestLogoUrl(UpgradeCase):
    def save_settings(self, logo):
        self.login(ADMIN)
        with self.app_ctx():
            row = dict(self.db().execute("SELECT * FROM system_settings WHERE id = 1").fetchone())
        data = {"school_name": row["school_name"], "school_name_arabic": row["school_name_arabic"] or "",
                "tagline": row["tagline"] or "", "address": row["address"] or "", "logo_url": logo}
        return self.post("/admin/settings", data, token_from="/admin/settings", follow_redirects=True)

    def test_valid_logo_is_used_everywhere_with_fallback(self):
        self.save_settings("https://cdn.example.com/logo.png")
        self.logout()
        login = self.client.get("/auth/login").data
        self.assertIn(b"https://cdn.example.com/logo.png", login)
        self.assertIn(b"onerror", login)                    # fallback to the default badge
        self.assertIn(b'rel="icon"', login)
        self.login(ADMIN)
        self.assertIn(b"https://cdn.example.com/logo.png", self.client.get("/admin/").data)

    def test_invalid_logo_urls_rejected(self):
        for bad in ("http://cdn.example.com/logo.png", "javascript:alert(1)", "https://x.com/logo.php",
                    "https://user:pw@cdn.example.com/logo.png", "https://cdn.example.com/a b.png",
                    "data:image/png;base64,AAAA"):
            self.save_settings(bad)
            with self.app_ctx():
                self.assertIsNone(self.db().execute("SELECT logo_url FROM system_settings WHERE id = 1").fetchone()["logo_url"], bad)
            self.logout()

    def test_no_logo_uses_default_badge(self):
        self.assertIn(b"AIA", self.client.get("/auth/login").data)


class TestAttendanceReliability(UpgradeCase):
    def test_duplicate_submission_inserts_one_record(self):
        sid = self.get_student_id()
        session_id = self.start_session_as_teacher()
        from services.attendance_service import record_attendance
        with self.app_ctx():
            db = self.db()
            results = [record_attendance(db, session_id, sid, "manual", marked_by_user_id=None) for _ in range(3)]
            self.assertEqual([r.code for r in results], ["RECORDED", "ALREADY_RECORDED", "ALREADY_RECORDED"])
            n = db.execute("SELECT COUNT(*) c FROM attendance_records WHERE session_id = ? AND student_id = ?",
                           (session_id, sid)).fetchone()["c"]
            self.assertEqual(n, 1)

    def test_only_one_open_session_per_assignment(self):
        self.start_session_as_teacher()
        with self.app_ctx():
            db = self.db()
            aid = db.execute("SELECT id FROM subject_assignments LIMIT 1").fetchone()["id"]
            with self.assertRaises(Exception):
                db.execute("INSERT INTO attendance_sessions (subject_assignment_id, session_code, session_date, scheduled_start, status) VALUES (?, 'TEST-2', '2026-10-06', '2026-10-06 08:00:00', 'active')", (aid,))

    def test_scanner_page_has_next_student_flow(self):
        session_id = self.start_session_as_teacher()
        page = self.client.get(f"/teacher/scanner/{session_id}").data
        self.assertIn(b"Next Student", page)
        self.assertIn(b"one student at a time", page)
        self.assertIn(b"holdResult", page)
        self.assertIn(b"case 'RECORDED'", page)               # old script waited for a state the server never sent


class TestMigrationsAndBackup(unittest.TestCase):
    def make_db(self, path):
        from database import db as dbmod, migrate
        d = dbmod.connect({"DATABASE_PATH": path})
        migrate.apply_pending(d, log=lambda *_: None)
        return d

    def test_migrations_are_repeatable_and_never_destructive(self):
        import tempfile
        from database import migrate
        with tempfile.TemporaryDirectory() as tmp:
            d = self.make_db(os.path.join(tmp, "a.db"))
            d.execute("INSERT INTO users (email, password_hash, role, full_name) VALUES ('x@y.test', 'h', 'teacher', 'X')")
            d.commit()
            self.assertEqual(migrate.apply_pending(d, log=lambda *_: None), [])      # second run: nothing to do
            self.assertEqual(d.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)  # data untouched

    def test_postgres_schema_renders_without_sqlite_syntax(self):
        from database import migrate
        sql = migrate.render(open(os.path.join(ROOT, "database/migrations/0001_initial.sql"), encoding="utf-8").read(), "postgres")
        for bad in ("AUTOINCREMENT", "datetime('now')", " REAL", "PRAGMA", '{{'):
            self.assertNotIn(bad, sql)
        self.assertIn("GENERATED BY DEFAULT AS IDENTITY", sql)
        self.assertIn("DOUBLE PRECISION", sql)

    def test_sqlite_import_backup_restore_roundtrip(self):
        import subprocess, tempfile, sqlite3
        tool = os.path.join(ROOT, "scripts", "dbtool.py")
        with tempfile.TemporaryDirectory() as tmp:
            src_path = os.path.join(tmp, "src.db")
            d = self.make_db(src_path)
            d.execute("INSERT INTO users (email, password_hash, role, full_name) VALUES ('t@y.test', 'h', 'teacher', 'T')")
            d.execute("INSERT INTO users (email, password_hash, role, full_name) VALUES ('s@y.test', 'h', 'super_admin', 'S')")
            d.commit(); d.close()
            face = os.path.join(tmp, "face_data"); os.makedirs(face)
            open(os.path.join(face, "x.txt"), "w").write("sample")
            out = os.path.join(tmp, "b.tar.gz")
            r = subprocess.run([sys.executable, tool, "backup", "--sqlite", src_path, "--out", out, "--face-dir", face],
                               capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != "DATABASE_URL"})
            self.assertEqual(r.returncode, 0, r.stderr)
            with tarfile.open(out) as tar:
                self.assertIn("database.jsonl", tar.getnames())
            restored = os.path.join(tmp, "restored.db")
            r = subprocess.run([sys.executable, tool, "restore", "--file", out, "--to-sqlite", restored, "--face-dir", os.path.join(tmp, "face2")],
                               capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != "DATABASE_URL"})
            self.assertEqual(r.returncode, 0, r.stderr)
            rows = sqlite3.connect(restored).execute("SELECT email FROM users ORDER BY email").fetchall()
            self.assertEqual(rows, [("s@y.test",), ("t@y.test",)])
            self.assertTrue(os.path.exists(os.path.join(tmp, "face2", "x.txt")))
            # restoring onto an existing file / non-empty DB is refused (never overwrites)
            r = subprocess.run([sys.executable, tool, "restore", "--file", out, "--to-sqlite", restored],
                               capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != "DATABASE_URL"})
            self.assertNotEqual(r.returncode, 0)


class TestLegacyDataMigration(unittest.TestCase):
    """Rehearse moving a database created by the ORIGINAL (pre-upgrade) schema into the new schema."""

    def build_legacy(self, path, duplicate_email=False):
        import sqlite3
        con = sqlite3.connect(path)
        con.executescript(open(os.path.join(ROOT, "tests", "fixtures", "legacy_schema.sql"), encoding="utf-8").read())
        con.execute("INSERT INTO users (email, password_hash, role, full_name) VALUES ('boss@old.test','HASH1','super_admin','Boss')")
        con.execute("INSERT INTO users (email, password_hash, role, full_name) VALUES ('adm@old.test','HASH2','school_admin','Legacy Admin')")
        con.execute("INSERT INTO users (email, password_hash, role, full_name) VALUES ('stu@old.test','HASH3','student','Stu Dent')")
        if duplicate_email:
            con.execute("INSERT INTO users (email, password_hash, role, full_name) VALUES ('BOSS@old.test','HASHX','teacher','Dup')")
        con.execute("INSERT INTO academic_sessions (name, start_date, end_date, is_active) VALUES ('2026/2027','2026-09-01','2027-07-31',1)")
        con.execute("INSERT INTO sections (name) VALUES ('Secondary')")
        con.execute("INSERT INTO classes (name, section_id, academic_session_id) VALUES ('JSS 1', 1, 1)")
        con.execute("INSERT INTO students (user_id, admission_number, first_name, surname, class_id) VALUES (3,'ALI/1','Stu','Dent',1)")
        con.commit(); con.close()

    def run_tool(self, *args):
        import subprocess
        env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
        return subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "dbtool.py"), *args], capture_output=True, text=True, env=env)

    def test_legacy_database_migrates_with_ids_hashes_and_defaults_preserved(self):
        import sqlite3, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = os.path.join(tmp, "old.db"), os.path.join(tmp, "new.db")
            self.build_legacy(src)
            r = self.run_tool("import-sqlite", "--source", src, "--to-sqlite", dst)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("school_admin", r.stdout)                           # legacy role flagged
            con = sqlite3.connect(dst)
            self.assertEqual(con.execute("SELECT id, password_hash, must_change_password FROM users WHERE email='stu@old.test'").fetchone(), (3, "HASH3", 0))
            self.assertEqual(con.execute("SELECT user_id, class_id FROM students WHERE admission_number='ALI/1'").fetchone(), (3, 1))
            self.assertEqual(con.execute("SELECT COUNT(*) FROM parents").fetchone()[0], 0)

    def test_duplicate_logins_are_caught_before_anything_is_written(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = os.path.join(tmp, "old.db"), os.path.join(tmp, "new.db")
            self.build_legacy(src, duplicate_email=True)
            r = self.run_tool("import-sqlite", "--source", src, "--to-sqlite", dst)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("Duplicate login email", r.stdout + r.stderr)
            self.assertFalse(os.path.exists(dst))


class TestPortableSQL(unittest.TestCase):
    """PostgreSQL is not available where this was built, so guard against SQLite-only SQL by inspection."""

    def sources(self):
        for folder in ("routes", "services", "utils"):
            for name in os.listdir(os.path.join(ROOT, folder)):
                if name.endswith(".py"):
                    yield f"{folder}/{name}", open(os.path.join(ROOT, folder, name), encoding="utf-8").read()
        yield "seed.py", open(os.path.join(ROOT, "seed.py"), encoding="utf-8").read()

    def test_no_sqlite_only_sql(self):
        banned = [r"INSERT\s+OR\s+(IGNORE|REPLACE)", r"date\('now'", r"(?<!\.)strftime\(", r"GROUP_CONCAT", r"julianday",
                  r"\bIFNULL\(", r"AUTOINCREMENT", r"PRAGMA", r"executescript", r"sqlite_master"]
        for name, text in self.sources():
            for pat in banned:
                self.assertIsNone(re.search(pat, text, re.I), f"{name} contains SQLite-only SQL: {pat}")

    def test_no_having_on_select_alias(self):
        for name, text in self.sources():
            self.assertIsNone(re.search(r"HAVING\s+(pct|total|c)\b", text), f"{name}: HAVING on an alias fails on PostgreSQL")

    def test_translation(self):
        from database.db import translate
        self.assertEqual(translate("SELECT * FROM t WHERE a = ? AND b LIKE ?"),
                         "SELECT * FROM t WHERE a = %s AND b ILIKE %s")
        self.assertTrue(translate("INSERT INTO t (a) VALUES (?)").endswith("RETURNING id"))
        self.assertNotIn("datetime", translate("UPDATE t SET x = datetime('now')"))
        self.assertIn("100%%", translate("SELECT '100%' FROM t"))

    def test_production_refuses_unsafe_config(self):
        import importlib
        os.environ["FLASK_ENV"] = "production"
        os.environ["SECRET_KEY"] = "short"
        os.environ["DATABASE_URL"] = "postgresql://u:p@h/db"
        try:
            import config
            importlib.reload(config)
            with self.assertRaises(RuntimeError):
                config.get_config().validate()
        finally:
            for k in ("FLASK_ENV", "SECRET_KEY", "DATABASE_URL"):
                os.environ.pop(k, None)
            importlib.reload(config)


class TestSetupFlow(UpgradeCase):
    def test_setup_disabled_without_token_and_after_super_admin_exists(self):
        self.assertEqual(self.client.get("/auth/setup").status_code, 404)

    def test_setup_creates_first_super_admin_only_with_token(self):
        with self.app_ctx():
            db = self.db()
            db.execute("DELETE FROM users WHERE role = 'super_admin'"); db.commit()
        self.flask_app.config["SETUP_TOKEN"] = "correct-horse-battery"
        tok = get_csrf(self.client.get("/auth/setup").data)
        bad = self.client.post("/auth/setup", data={"csrf_token": tok, "setup_token": "nope", "full_name": "Boss",
                               "email": "boss@school.test", "password": "Str0ng-Passw0rd", "confirm_password": "Str0ng-Passw0rd"})
        self.assertIn(b"incorrect", bad.data)
        ok = self.client.post("/auth/setup", data={"csrf_token": tok, "setup_token": "correct-horse-battery", "full_name": "Boss",
                              "email": "boss@school.test", "password": "Str0ng-Passw0rd", "confirm_password": "Str0ng-Passw0rd"},
                              follow_redirects=True)
        self.assertIn(b"Super Administrator created", ok.data)
        self.assertEqual(self.client.get("/auth/setup").status_code, 404)             # gone once an account exists
        self.assertIn(b"Total Students", self.login("boss@school.test", "Str0ng-Passw0rd").data)


if __name__ == "__main__":
    unittest.main()
