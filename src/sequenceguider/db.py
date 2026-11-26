"""One way to open records.db, shared by accounts, records and the job queue.

WAL lets the many readers (every page load) carry on while one request writes,
and busy_timeout makes a second writer wait its turn instead of failing with
"database is locked". With ~200 people that is all SQLite needs.
"""

import sqlite3
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    # one connection per call: the web server handles requests on several threads
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 10000")
    db.execute("PRAGMA foreign_keys = ON")
    return db


def enable_wal(path: Path) -> None:
    """Persistent: once set, the file stays in WAL mode (also for the CLI and backups)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    try:
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = NORMAL")
    finally:
        db.close()


def backup(path: Path, dest: Path) -> Path:
    """A consistent copy of a live database (sqlite's online backup: safe while the server runs)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    src, out = sqlite3.connect(path, timeout=10), sqlite3.connect(dest)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return dest
