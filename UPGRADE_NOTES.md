# Production upgrade — notes, decisions and honest test status

## What was changed (smallest set that meets the brief)
Kept: Flask, all existing routes/templates/CSS, OpenCV LBPH recognition, QR/manual attendance, reports, devices,
offline sync, audit log, RBAC. Not changed: framework, UI design, recognition algorithm.

| Area | Change |
|---|---|
| Database | `database/db.py` now serves **PostgreSQL via `DATABASE_URL`** (connection pool, transactions, auto-rollback, graceful 503) and SQLite for dev/tests. Route SQL stays portable; `translate()` adapts the few differences. No SQLAlchemy added. |
| Schema | `database/schema.sql` → portable **`database/migrations/0001_initial.sql`** + `database/migrate.py` (ordered, repeatable, checksummed, advisory-locked, never destructive). **Not run on app start**; Railway runs it as the pre-deploy command. |
| Indexes / integrity | Unique case-insensitive login email & admission number; one open session per class-subject; `UNIQUE(session,student)` now enforced with `ON CONFLICT DO NOTHING` (concurrent scans can't duplicate); indexes for student/parent/attendance lookups. |
| Roles | New `temp_admin` (slot 1/2) and `parent`. Only `super_admin` is unrestricted. New `utils/permissions.py` + `permission_required`; every admin route is server-side protected; denied attempts are audit-logged. |
| Temp admins | `/admin/temp-admins` (Super Admin only): create, edit holder, reset, activate/deactivate; policy switch for student-password resets (default **off**). Own login, own audit trail. |
| Students | Edit page, password reset, search/pagination, optional parent login created at registration (same transaction). Bulk import now gives **each student their own random password** (it used to be one shared password). |
| Passwords | `secrets`-based 12-char temporary passwords, hash only, forced change at first login, reset kills old password **and live sessions** (`session_version`), shown once in a copy dialog rendered in the POST response (not flash/cookie, `no-store`). Also for teachers, parents, temp admins, bulk import. |
| Parent Portal | `parent_students` link table, `/parent/*` blueprint with its own layout. Isolation is in the SQL (every child lookup joins the signed-in parent); unlinked = 404 + audit log. Multiple children per parent; Super Admin links/unlinks. |
| Logo | `logo_url` setting (https image URL, validated, never fetched by server), used in login, nav, dashboards, parent portal, favicon; automatic fallback badge; legacy upload kept for old PDFs. |
| Scanner | Fixed a bug: the old script waited for state `ATTENDANCE_RECORDED` but the server sends `RECORDED`, so success never displayed and cameras kept re-scanning. New **one-student-at-a-time** flow with **Next Student**, live counters/table without page reload, single in-flight frame. |
| Face data | Location is `DATA_DIR/face_data` (Railway Volume); model written atomically and cached by file mtime (was re-read from disk on every camera frame); never under `static/`. Super Admin dashboard warns if no persistent storage is configured. |
| Security | Session `sv` check, secure cookies, ProxyFix, no-cache for signed-in pages, security headers, generic login errors, lockout kept, production refuses weak `SECRET_KEY`/missing `DATABASE_URL`, `/auth/setup` first-run flow guarded by `SETUP_TOKEN` (no default admin), seed script refuses to run on production. |
| Deploy | `requirements.txt` fixed (was missing **reportlab/openpyxl**, so PDF/Excel exports would crash; opencv → headless), gunicorn, `railway.toml`, `.gitignore`, `.env.example`, `/healthz`, `/readyz`. |
| Backup | `scripts/dbtool.py`: `import-sqlite`, `backup`, `restore`, `counts`. Refuses to touch non-empty targets. |
| Removed | `seed_test_attendance.py` (referenced tables from an older university version; could not work). |

## Decisions you should confirm
1. **`school_admin` role** (existing): the brief says only the Super Administrator is unrestricted, so this legacy role now has **no access** (login says the account type is unsupported). `dbtool import-sqlite` lists any such accounts. Change them to `super_admin` in the DB if you want them kept.
2. **Student Portal**: kept (students still sign in for QR check-in and their own attendance; Spec §6 also issues student logins). The *family-facing* portal is the new Parent Portal. Say so if you want student logins disabled.
3. **Temp admins and passwords**: can't reset student passwords until the Super Admin ticks the policy box. They can create a parent login only while registering a student (and link siblings to an existing parent by email/phone).
4. **Parents without email** sign in with their phone number (an internal address is generated). Students sign in with email or admission number.
5. **Student status changes, face enrollment, reports** remain Super-Admin-only.
6. Time: set `TZ=Africa/Lagos`; the database stores UTC defaults, attendance times use server-local time.

## Test status (what is and is not proven)
Run: `python -m unittest discover -s tests` → **all pass on SQLite** (your original 20 tests, one assertion reworded for the new generic lockout message, plus new tests for roles, restricted URLs, temp-admin lifecycle, credential dialogs, forced change, resets, parent isolation, logo URL, duplicate/concurrent attendance, migrations, backup→restore round-trip, legacy-schema import, setup flow, SQL-portability guards).

| # | Requirement | Status |
|---|---|---|
| 1 App starts | Verified (SQLite). Production config validation tested |
| 2 PostgreSQL connects | **NOT tested** — no PostgreSQL/psycopg2/network in my build environment |
| 3 Existing data migrates | Verified SQLite→new schema (with ids/hashes kept, duplicate detection, row-count verification). **PostgreSQL target not run**; sequences reset code is untested |
| 4–8 Admin logins/permissions | Verified |
| 9–13 Student/teacher/temp-admin credentials, copy dialog | Server side verified; the **Copy button is JavaScript and was not exercised in a real browser** |
| 14–15 Parent portal isolation | Verified, including URL tampering |
| 16 Attendance & reports | Attendance verified; PDF/Excel export code unchanged and not re-exercised |
| 17 Face recognition | Verified enroll→recognize→duplicate block on the test fixture. **Not tested on Railway or a real classroom camera** |
| 18 Logo + fallback | Server side verified; the `onerror` fallback not exercised in a browser |
| 19 Survives restart/deploy | Migration idempotence verified; Volume behaviour **not tested on Railway** |
| 20 Mobile/desktop | CSS is responsive and untouched in structure, but **I could not view it on real devices** |

**Before calling it production-ready:** run the PostgreSQL steps in DEPLOY_RAILWAY.md on a staging Railway project, do the go-live checklist, and test face recognition on the real URL. PostgreSQL-specific SQL differences were handled by reading every query (`HAVING` aliases, `GROUP BY`, `date('now')`, case-insensitive `LIKE`, `RETURNING`, quoted defaults) and by automatic checks, but a real PostgreSQL run is the only proof.

## Known limits
* Face recognition (LBPH) compares against every enrolled face; fine for hundreds of students, test at your real size. Accuracy depends on lighting/camera.
* Account-lockout message can reveal that an account exists after repeated failures (generic text is used, but timing/behaviour differ slightly).
* Logout is a GET link (unchanged). No email/SMS sending is built in; credentials are handed over via the dialog.
* Railway specifics (pre-deploy command, `$PORT` expansion, volume mount, plan features) follow Railway's documented behaviour at the time of writing; confirm in your dashboard.
