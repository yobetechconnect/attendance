"""
Database access layer.

Production: PostgreSQL, selected by the DATABASE_URL environment
variable (Railway sets this for you). Development/tests: SQLite file,
selected when DATABASE_URL is not set.

The application's SQL is written once, in a SQLite-compatible portable
dialect with `?` placeholders. When running on PostgreSQL, `translate()`
rewrites the handful of differences (placeholders, `datetime('now')`,
case-insensitive LIKE, `lastrowid`) so route code did not have to be
rewritten. Anything that cannot be expressed portably was changed in the
route itself (see tests/test_upgrade.py::TestPortableSQL).

Guarantees:
- One connection per request, returned to the pool (PostgreSQL) or
  closed (SQLite) at teardown; anything uncommitted is rolled back.
- A failed statement rolls the whole transaction back, so a request can
  never leave half-written data behind or reuse an aborted transaction.
- Connection problems raise DatabaseUnavailable -> friendly 503 page.
- Credentials are only ever read from the environment, never logged.
"""

import re
import sqlite3
import threading
from contextlib import contextmanager

from flask import current_app, g


class DatabaseUnavailable(Exception):
    """The database could not be reached (shown to users as a 503 page)."""


# ----------------------------------------------------------------------------
# Row object: supports row["col"], row[0], row.col, dict(row), row.keys()
# (the same access styles sqlite3.Row gave the existing routes/templates)
# ----------------------------------------------------------------------------
class Row:
    __slots__ = ("_names", "_values", "_index")

    def __init__(self, names, values):
        object.__setattr__(self, "_names", names)
        object.__setattr__(self, "_values", values)
        object.__setattr__(self, "_index", {n: i for i, n in enumerate(names)})

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        try:
            return self._values[self._index[key]]
        except KeyError:
            raise KeyError(key) from None

    def __getattr__(self, name):
        try:
            return self._values[self._index[name]]
        except KeyError:
            raise AttributeError(name) from None

    def keys(self):
        return list(self._names)

    def get(self, key, default=None):
        return self._values[self._index[key]] if key in self._index else default

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __contains__(self, item):  # `"col" in row`
        return item in self._index

    def __repr__(self):
        return f"Row({dict(zip(self._names, self._values))})"


# ----------------------------------------------------------------------------
# SQLite -> PostgreSQL statement translation
# ----------------------------------------------------------------------------
PG_NOW_SQL = "to_char(timezone('utc', now()), 'YYYY-MM-DD HH24:MI:SS')"
_LIKE_RE = re.compile(r"\bLIKE\b", re.IGNORECASE)
_NOW_RE = re.compile(r"datetime\(\s*'now'\s*\)", re.IGNORECASE)


def translate(sql: str, returning: bool = True) -> str:
    """Return the PostgreSQL form of a portable SQL statement."""
    out = sql.replace("%", "%%")              # literal % must be doubled for psycopg2
    out = out.replace("?", "%s")
    out = _NOW_RE.sub(PG_NOW_SQL, out)
    out = _LIKE_RE.sub("ILIKE", out)          # SQLite LIKE is case-insensitive; match that
    stripped = out.lstrip().upper()
    if returning and stripped.startswith("INSERT") and "RETURNING" not in stripped:
        out = out.rstrip().rstrip(";") + " RETURNING id"
    return out


class Result:
    """Minimal cursor facade: fetchone/fetchall/iteration/lastrowid/rowcount."""

    def __init__(self, cursor, lastrowid=None, names=None):
        self._cur = cursor
        self.lastrowid = lastrowid
        self._names = names
        self.rowcount = cursor.rowcount

    def _wrap(self, values):
        return None if values is None else Row(self._names, tuple(values))

    def fetchone(self):
        if not self._names:
            return None
        return self._wrap(self._cur.fetchone())

    def fetchall(self):
        if not self._names:
            return []
        return [self._wrap(v) for v in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class Database:
    """One request-scoped connection with a uniform API for both engines."""

    def __init__(self, conn, dialect, pool=None):
        self.conn = conn
        self.dialect = dialect          # "sqlite" | "postgres"
        self._pool = pool
        self._savepoint_depth = 0

    @property
    def is_postgres(self):
        return self.dialect == "postgres"

    def execute(self, sql, params=(), returning=True):
        """Run one statement. `returning=False` skips the automatic
        `RETURNING id` on INSERT (only for tables without an `id` column)."""
        try:
            if self.is_postgres:
                cur = self.conn.cursor()
                cur.execute(translate(sql, returning), tuple(params) if params else None)
                is_insert = returning and sql.lstrip().upper().startswith("INSERT")
                lastrowid = None
                names = None
                if is_insert:
                    row = cur.fetchone() if cur.description else None
                    lastrowid = row[0] if row else None
                elif cur.description:
                    names = [d[0] for d in cur.description]
                return Result(cur, lastrowid, names)
            cur = self.conn.execute(sql, tuple(params))
            names = [d[0] for d in cur.description] if cur.description else None
            return Result(cur, cur.lastrowid, names)
        except DatabaseUnavailable:
            raise
        except Exception as exc:
            if self._savepoint_depth == 0:
                self._safe_rollback()
            if _is_connection_error(exc):
                raise DatabaseUnavailable(str(exc)) from exc
            raise

    def commit(self):
        try:
            self.conn.commit()
        except Exception as exc:
            self._safe_rollback()
            if _is_connection_error(exc):
                raise DatabaseUnavailable(str(exc)) from exc
            raise

    def rollback(self):
        self._safe_rollback()

    def _safe_rollback(self):
        try:
            self.conn.rollback()
        except Exception:
            pass

    @contextmanager
    def savepoint(self, name="sp"):
        """Row-level isolation inside a bigger transaction (bulk import)."""
        self._savepoint_depth += 1
        try:
            self.execute(f"SAVEPOINT {name}")
            try:
                yield
            except Exception:
                self.execute(f"ROLLBACK TO SAVEPOINT {name}")
                raise
            else:
                self.execute(f"RELEASE SAVEPOINT {name}")
        finally:
            self._savepoint_depth -= 1

    def close(self):
        if self.is_postgres:
            self._safe_rollback()  # never hand a dirty connection back to the pool
            if self._pool is not None:
                self._pool.putconn(self.conn, close=bool(self.conn.closed))
        else:
            self.conn.close()


def _is_connection_error(exc) -> bool:
    name = type(exc).__name__
    return name in ("OperationalError", "InterfaceError") and type(exc).__module__.startswith("psycopg2")


# ----------------------------------------------------------------------------
# Connection creation
# ----------------------------------------------------------------------------
_pool_lock = threading.Lock()
_pools = {}


def _pg_pool(app_config):
    url = app_config["DATABASE_URL"]
    with _pool_lock:
        pool = _pools.get(url)
        if pool is None:
            try:
                import psycopg2
                import psycopg2.extensions
                import psycopg2.pool
            except ImportError as exc:  # pragma: no cover - deployment error
                raise RuntimeError("DATABASE_URL is set but psycopg2 is not installed "
                                   "(pip install -r requirements.txt).") from exc
            # NUMERIC (from ROUND(...)) -> float so templates/comparisons behave like SQLite
            dec2float = psycopg2.extensions.new_type(
                psycopg2.extensions.DECIMAL.values, "DEC2FLOAT",
                lambda v, cur: float(v) if v is not None else None)
            psycopg2.extensions.register_type(dec2float)
            try:
                pool = psycopg2.pool.ThreadedConnectionPool(
                    1, int(app_config.get("DB_POOL_MAX", 8)), dsn=url,
                    connect_timeout=int(app_config.get("DB_CONNECT_TIMEOUT", 8)),
                    keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
                )
            except psycopg2.Error as exc:
                raise DatabaseUnavailable("Could not connect to the database.") from exc
            _pools[url] = pool
        return pool


def connect(app_config) -> Database:
    """Open a Database (used by get_db and by the command-line tools)."""
    if app_config.get("DATABASE_URL"):
        pool = _pg_pool(app_config)
        try:
            conn = pool.getconn()
            if conn.closed:
                pool.putconn(conn, close=True)
                conn = pool.getconn()
            conn.autocommit = False
            return Database(conn, "postgres", pool)
        except DatabaseUnavailable:
            raise
        except Exception as exc:
            raise DatabaseUnavailable("Could not connect to the database.") from exc

    conn = sqlite3.connect(app_config["DATABASE_PATH"], timeout=15)
    conn.execute("PRAGMA foreign_keys = ON")
    return Database(conn, "sqlite")


def get_db() -> Database:
    """Return the request-scoped connection."""
    if "db" not in g:
        g.db = connect(current_app.config)
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        try:
            db.close()
        except Exception:
            pass


def init_db():
    """Apply pending schema migrations (safe to run repeatedly; never drops data)."""
    from database import migrate
    migrate.apply_pending(get_db())


def init_app(app):
    app.teardown_appcontext(close_db)
