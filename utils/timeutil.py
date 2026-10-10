"""School-local time helpers. Set the TZ environment variable (e.g. Africa/Lagos)
on the server so datetime.now() is school time."""

from datetime import datetime


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
