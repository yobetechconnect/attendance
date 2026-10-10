# Backup and recovery

What must be protected: (1) the **PostgreSQL database** (students, teachers, parents, attendance, audit log)
and (2) the **face-data folder** on the Volume (enrolled face samples + trained model).
Nothing in this project ever deletes or resets production data on deploy.

## A. Database backup (from your own computer — no database server needed)
```
pip install -r requirements.txt
export DATABASE_URL="<Railway Postgres → Variables → DATABASE_PUBLIC_URL>"
python scripts/dbtool.py backup --out backup-$(date +%F).tar.gz
```
The file holds every table (student records, attendance records, accounts) in a portable format and prints row counts.
Keep copies **off Railway** (school computer + cloud drive), at least weekly and before big changes.
The file contains personal data and password hashes: store it privately.
Railway also offers its own database/volume backups depending on your plan — enabling them is a good second layer, but verify what your plan includes; this tool does not depend on it.

## B. Face-data backup
Signed in as Super Administrator → **Backup** → *Download face-data archive (.zip)*. Do this after enrolling many students.
(Face samples can also be recaptured by re-enrolling, so this is convenience, not the only safeguard.)

## C. Test that a backup really restores (do this once now, then monthly)
Needs no server — restores into a throw-away SQLite file:
```
python scripts/dbtool.py restore --file backup-2026-10-09.tar.gz --to-sqlite /tmp/restore-test.db
python scripts/dbtool.py counts --sqlite /tmp/restore-test.db
```
Compare the counts with those printed by the backup (students, attendance_records, users …). They must match.

## D. Real restore (disaster recovery)
1. Create a **new, empty** PostgreSQL in Railway (+ New → Database → PostgreSQL).
2. `export DATABASE_URL=<its DATABASE_PUBLIC_URL>`
3. `python scripts/dbtool.py restore --file backup-….tar.gz` (add `--face-dir <folder>` to unpack face data too)
4. Point the web service's `DATABASE_URL` reference at the new database and redeploy.

The restore **refuses to write into a database that already has data**, so you can never overwrite live records by accident.
