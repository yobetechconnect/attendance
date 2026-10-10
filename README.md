# AL-ISTI'ANAH ACADEMY DAMATURU — Attendance Management System

Flask + PostgreSQL (SQLite for local development) + OpenCV face recognition.

* Deploy to Railway: **DEPLOY_RAILWAY.md**
* Backups and restore: **BACKUP_RESTORE.md**
* What changed in the production upgrade, decisions, test results and limits: **UPGRADE_NOTES.md**

## Roles
| Role | Access |
|---|---|
| Super Administrator | Everything (the only unrestricted role) |
| Temporary Administrator 1 / 2 | Student management only |
| Teacher / attendance operator | Own classes: start sessions, face/QR/manual attendance, history |
| Parent / Guardian | Parent Portal: only their linked children |
| Student | Own attendance + QR check-in |
| Auditor | Audit log and read-only attendance reports |

## Local development
```
pip install -r requirements.txt
python -m flask --app app db-migrate     # creates database/attendance.db
python seed.py                           # DEV ONLY demo accounts (refuses to run when DATABASE_URL is set)
python -m flask --app app run --debug
python -m unittest discover -s tests     # 55+ tests
```
Or create a real first account instead of seeding: `python -m flask --app app create-super-admin`.

## Taking attendance (face recognition, one student at a time)
Start a session → **Start Camera** → the student looks at the camera → when recorded, scanning **stops** and the
result stays on screen → the teacher presses **Next Student** (or Enter/Space) → the next student steps up.
