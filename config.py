"""
Configuration for the Attendance Management System.

Everything secret comes from environment variables (see .env.example and
DEPLOY_RAILWAY.md). Nothing secret is stored in the source code.

Environment selection:
  FLASK_ENV=production|development|testing
  (if unset: "production" when DATABASE_URL is set, else "development")
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
_DEV_SECRET = "dev-secret-key-change-me"


def _data_root() -> str:
    """Root folder for files that must survive redeploys (face data, backups).
    On Railway: attach a Volume and set DATA_DIR to its mount path (e.g. /data)."""
    return os.environ.get("DATA_DIR") or str(BASE_DIR / "storage")


class BaseConfig:
    SECRET_KEY = os.environ.get("SECRET_KEY", _DEV_SECRET)

    # PostgreSQL (production). Railway injects DATABASE_URL for its Postgres service.
    DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip() or None
    if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
    DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", 8))

    # SQLite is for local development and automated tests only.
    DATABASE_PATH = os.environ.get("DATABASE_PATH", str(BASE_DIR / "database" / "attendance.db"))

    DATA_DIR = _data_root()
    STORAGE_PERSISTENT = bool(os.environ.get("DATA_DIR"))
    FACE_DATA_DIR = os.environ.get("FACE_DATA_DIR") or os.path.join(DATA_DIR, "face_data")
    LOGO_UPLOAD_DIR = os.path.join(DATA_DIR, "branding")   # legacy uploaded logos only
    BACKUP_DIR = os.path.join(DATA_DIR, "backups")

    # Face recognition tuning
    FACE_SAMPLES_REQUIRED = int(os.environ.get("FACE_SAMPLES_REQUIRED", 20))
    # LBPH: LOWER distance = better match. Anything above this is rejected.
    FACE_RECOGNITION_THRESHOLD = float(os.environ.get("FACE_RECOGNITION_THRESHOLD", 65.0))

    # First-time Super Administrator setup (see /auth/setup). Unset = web setup disabled.
    SETUP_TOKEN = os.environ.get("SETUP_TOKEN") or None

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = int(os.environ.get("SESSION_HOURS", 12)) * 3600
    MAX_CONTENT_LENGTH = 8 * 1024 * 1024   # reject oversized uploads (camera frames are small)

    WTF_CSRF_TIME_LIMIT = None

    @classmethod
    def validate(cls):
        """Called at startup. Production must not boot with unsafe settings."""


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    SESSION_COOKIE_SECURE = False


class ProductionConfig(BaseConfig):
    DEBUG = False
    SESSION_COOKIE_SECURE = True

    @classmethod
    def validate(cls):
        if cls.SECRET_KEY == _DEV_SECRET or len(cls.SECRET_KEY) < 32:
            raise RuntimeError(
                "SECRET_KEY must be set to a long random value (32+ characters) in production. "
                "Generate one with:  python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        if not cls.DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. In Railway, add a PostgreSQL service and reference its "
                "DATABASE_URL variable from this service."
            )


class TestingConfig(BaseConfig):
    DEBUG = True
    TESTING = True
    SESSION_COOKIE_SECURE = False
    DATABASE_URL = None


CONFIG_MAP = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}


def get_config():
    default_env = "production" if os.environ.get("DATABASE_URL") else "development"
    env = os.environ.get("FLASK_ENV", default_env)
    # Unknown values fall back to the SAFE (production) settings, never to debug mode.
    return CONFIG_MAP.get(env, ProductionConfig)
