#!/usr/bin/env python3
"""
Database tool: safe migration from SQLite, backup, restore and verification.

Run from the project folder. Needs only Python + `pip install -r requirements.txt`
(no database server on your computer).

  python scripts/dbtool.py migrate
        Apply pending schema migrations to the database in DATABASE_URL.

  python scripts/dbtool.py import-sqlite --source database/attendance.db [--to-sqlite rehearsal.db]
        (--to-sqlite rehearses the whole migration into a scratch SQLite file, no server needed.)
        Copy ALL records from your existing SQLite file into the (empty) PostgreSQL
        database in DATABASE_URL. Ids are preserved (face-recognition data stays valid).
        Refuses to run if the target already holds data — it NEVER overwrites or deletes.
        All-or-nothing: one transaction; any problem rolls everything back.

  python scripts/dbtool.py backup --out backup-2026-10-09.tar.gz [--face-dir /path/face_data]
        Portable backup: every table (JSON lines) + optional face-data folder.

  python scripts/dbtool.py restore --file backup.tar.gz --to-sqlite /tmp/restore-test.db
        TEST a backup without any server: restores into a fresh SQLite file and
        prints row counts you can compare. Or restore into an EMPTY PostgreSQL:
  python scripts/dbtool.py --database-url postgresql://... restore --file backup.tar.gz

  python scripts/dbtool.py counts
        Row counts per table for the database in DATABASE_URL (or --sqlite FILE).

DATABASE_URL is read from the environment (or --database-url). Never paste real
credentials into source files.
"""

import argparse
import json
import os
import sqlite3
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from database import db as dbmod          # noqa: E402
from database import migrate               # noqa: E402

# Parents before children (foreign-key order)
TABLES = [
    "academic_sessions", "terms", "system_settings", "users", "sections", "departments",
    "teachers", "classes", "subjects", "subject_assignments", "students", "guardians",
    "class_enrollments", "devices", "attendance_sessions", "attendance_records",
    "attendance_corrections", "verification_events", "qr_tokens", "biometric_profiles",
    "offline_sync_events", "notifications", "audit_logs", "import_batches",
    "parents", "parent_students",
]
BATCH = 1000


# ----------------------------------------------------------------- connections
def open_target(args, create_sqlite=False):
    if getattr(args, "to_sqlite", None):
        path = args.to_sqlite
        if os.path.exists(path):
            sys.exit(f"Refusing to overwrite existing file {path}")
        return dbmod.connect({"DATABASE_PATH": path})
    url = args.database_url or os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("Set DATABASE_URL (or pass --database-url / --to-sqlite).")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return dbmod.connect({"DATABASE_URL": url})


def columns_of(db, table):
    if db.is_postgres:
        rows = db.execute("SELECT column_name FROM information_schema.columns WHERE table_name = ? "
                          "AND table_schema = current_schema() ORDER BY ordinal_position", (table,)).fetchall()
        return [r[0] for r in rows]
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]


def table_exists(db, table):
    return bool(columns_of(db, table))


def count(db, table):
    return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ----------------------------------------------------------------- safety checks
def require_empty_target(dst):
    """Never overwrite: every data table must be empty (an untouched default
    system_settings row is allowed only when there are no users at all)."""
    busy = {t: count(dst, t) for t in TABLES if t != "system_settings"}
    busy = {t: n for t, n in busy.items() if n}
    if busy:
        sys.exit("Target database is NOT empty, refusing to touch it: " +
                 ", ".join(f"{t}={n}" for t, n in busy.items()))
    if table_exists(dst, "system_settings") and count(dst, "system_settings"):
        dst.execute("DELETE FROM system_settings")   # untouched default row (no users exist)


def preflight_source(src_path):
    """Problems that would make PostgreSQL reject the data, found BEFORE writing anything."""
    src = sqlite3.connect(src_path)
    problems, warnings = [], []
    try:
        dup = src.execute("SELECT lower(email), COUNT(*) FROM users GROUP BY lower(email) HAVING COUNT(*) > 1").fetchall()
        problems += [f"Duplicate login email (ignoring case): {e} x{n}" for e, n in dup]
        dup = src.execute("SELECT upper(admission_number), COUNT(*) FROM students GROUP BY upper(admission_number) HAVING COUNT(*) > 1").fetchall()
        problems += [f"Duplicate admission number (ignoring case): {a} x{n}" for a, n in dup]
        legacy = src.execute("SELECT email FROM users WHERE role = 'school_admin'").fetchall()
        if legacy:
            warnings.append("These 'school_admin' accounts will keep existing but have NO access in the new "
                            "version (only the Super Administrator is unrestricted): " + ", ".join(r[0] for r in legacy))
        if not src.execute("SELECT 1 FROM users WHERE role = 'super_admin' LIMIT 1").fetchone():
            warnings.append("The source has no super_admin user; create one with `flask --app app create-super-admin`.")
        bad = src.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            problems.append(f"Source has {len(bad)} foreign-key violations (orphaned rows); fix them first.")
    finally:
        src.close()
    return problems, warnings


# ----------------------------------------------------------------- readers
def sqlite_reader(path):
    src = sqlite3.connect(path)
    try:
        have = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in TABLES:
            if t not in have:
                continue
            cols = [r[1] for r in src.execute(f"PRAGMA table_info({t})")]
            cur = src.execute(f"SELECT {', '.join(cols)} FROM {t} ORDER BY id")
            yield t, cols, iter(lambda c=cur: c.fetchmany(BATCH) or None, None)
    finally:
        src.close()


def jsonl_reader(path):
    with tarfile.open(path, "r:gz") as tar:
        member = tar.extractfile("database.jsonl")
        header = json.loads(member.readline())
        if header.get("format") != "attendance-backup-1":
            sys.exit("Unrecognised backup format.")
        table = cols = None
        pending = []
        for line in member:
            obj = json.loads(line)
            if isinstance(obj, dict):                    # start of a table
                if table and pending:
                    yield table, cols, iter([pending])
                table, cols, pending = obj["t"], obj["cols"], []
            else:
                pending.append(tuple(obj))
                if len(pending) >= BATCH:
                    yield table, cols, iter([pending])
                    pending = []
        if table and pending:
            yield table, cols, iter([pending])


def stream_rows(db, sql):
    """Yield row tuples without loading a whole table into memory (server-side cursor on PostgreSQL)."""
    if db.is_postgres:
        cur = db.conn.cursor(name="backup_stream")
        cur.itersize = BATCH
        cur.execute(sql)
        try:
            while True:
                rows = cur.fetchmany(BATCH)
                if not rows:
                    break
                yield from rows
        finally:
            cur.close()
    else:
        cur = db.conn.execute(sql)
        while True:
            rows = cur.fetchmany(BATCH)
            if not rows:
                break
            yield from rows


# ----------------------------------------------------------------- writing
def insert_batch(dst, table, cols, rows):
    if not rows:
        return
    sql_cols = ", ".join(cols)
    if dst.is_postgres:
        from psycopg2.extras import execute_values
        execute_values(dst.conn.cursor(), f"INSERT INTO {table} ({sql_cols}) VALUES %s", rows, page_size=BATCH)
    else:
        ph = ", ".join("?" for _ in cols)
        dst.conn.executemany(f"INSERT INTO {table} ({sql_cols}) VALUES ({ph})", rows)


def load(dst, reader):
    """Copy everything from `reader` into dst inside ONE transaction."""
    col_cache = {}
    try:
        for table, cols, chunks in reader:
            if table not in col_cache:
                col_cache[table] = columns_of(dst, table)
            dest_cols = col_cache[table]
            keep = [i for i, c in enumerate(cols) if c in dest_cols]
            use_cols = [cols[i] for i in keep]
            for chunk in chunks:
                rows = [tuple(r[i] for i in keep) for r in chunk]
                insert_batch(dst, table, use_cols, rows)
        if dst.is_postgres:
            for t in TABLES:
                if t == "system_settings":
                    continue
                dst.execute(
                    f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {t}), 0) + 1, false)")
        dst.commit()
    except Exception:
        dst.rollback()
        raise


def print_counts(dst, title):
    print(f"\n{title}")
    for t in TABLES:
        if table_exists(dst, t):
            print(f"  {t:26s} {count(dst, t):>10d}")


# ----------------------------------------------------------------- commands
def cmd_migrate(args):
    dst = open_target(args)
    migrate.apply_pending(dst)


def cmd_import_sqlite(args):
    if not os.path.isfile(args.source):
        sys.exit(f"Source file not found: {args.source}")
    problems, warnings = preflight_source(args.source)
    for w in warnings:
        print("WARNING:", w)
    if problems:
        sys.exit("Cannot import:\n  - " + "\n  - ".join(problems))
    dst = open_target(args)
    migrate.apply_pending(dst)
    require_empty_target(dst)
    load(dst, sqlite_reader(args.source))

    # Verify against the source
    src = sqlite3.connect(args.source)
    ok = True
    print("\nVerification (source -> target):")
    for t in TABLES:
        if not src.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (t,)).fetchone():
            continue
        a, b = src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0], count(dst, t)
        flag = "OK" if a == b else "MISMATCH"
        ok &= a == b
        print(f"  {t:26s} {a:>10d} -> {b:>10d}  {flag}")
    src.close()
    if not ok:
        sys.exit("Row counts differ — investigate before using the new database.")
    print("\nImport complete. All row counts match. Copy your storage/face_data folder to the "
          "server's persistent volume (see DEPLOY_RAILWAY.md).")


def cmd_backup(args):
    if args.sqlite:
        src = dbmod.connect({"DATABASE_PATH": args.sqlite})
    elif args.database_url or os.environ.get("DATABASE_URL"):
        src = open_target(args)
    else:
        sys.exit("Set DATABASE_URL (or --database-url), or pass --sqlite FILE.")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "database.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"format": "attendance-backup-1", "tables": TABLES}) + "\n")
            for t in TABLES:
                if not table_exists(src, t):
                    continue
                cols = columns_of(src, t)
                f.write(json.dumps({"t": t, "cols": cols}) + "\n")
                for r in stream_rows(src, f"SELECT {', '.join(cols)} FROM {t} ORDER BY id"):
                    f.write(json.dumps(list(r), default=str) + "\n")
        with tarfile.open(args.out, "w:gz") as tar:
            tar.add(path, arcname="database.jsonl")
            if args.face_dir and os.path.isdir(args.face_dir):
                tar.add(args.face_dir, arcname="face_data")
    print(f"Backup written: {args.out} ({os.path.getsize(args.out):,} bytes)")
    print_counts(src, "Rows backed up:")


def cmd_restore(args):
    dst = open_target(args)
    migrate.apply_pending(dst)
    require_empty_target(dst)
    load(dst, jsonl_reader(args.file))
    print_counts(dst, "Rows restored:")
    if args.face_dir:
        with tarfile.open(args.file, "r:gz") as tar:
            members = [m for m in tar.getmembers() if m.name.startswith("face_data/")]
            for m in members:
                m.name = m.name[len("face_data/"):]
            os.makedirs(args.face_dir, exist_ok=True)
            tar.extractall(args.face_dir, members=[m for m in members if m.name])
        print(f"Face data restored to {args.face_dir}")
    print("\nRestore complete. Compare these counts with the backup output.")


def cmd_counts(args):
    if args.sqlite:
        dst = dbmod.connect({"DATABASE_PATH": args.sqlite})
    else:
        dst = open_target(args)
    print_counts(dst, "Row counts:")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", help="PostgreSQL URL (default: $DATABASE_URL)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    p = sub.add_parser("import-sqlite"); p.add_argument("--source", required=True); p.add_argument("--to-sqlite")
    p = sub.add_parser("backup"); p.add_argument("--out", required=True)
    p.add_argument("--face-dir"); p.add_argument("--sqlite", help="back up a SQLite file instead of DATABASE_URL")
    p = sub.add_parser("restore"); p.add_argument("--file", required=True)
    p.add_argument("--to-sqlite"); p.add_argument("--face-dir")
    p = sub.add_parser("counts"); p.add_argument("--sqlite")
    args = ap.parse_args()
    {"migrate": cmd_migrate, "import-sqlite": cmd_import_sqlite, "backup": cmd_backup,
     "restore": cmd_restore, "counts": cmd_counts}[args.cmd](args)


if __name__ == "__main__":
    main()
