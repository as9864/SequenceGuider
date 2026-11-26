"""Practice records: checklist ticks and practice sessions, in one SQLite file.

Every row belongs to one user (accounts.py). Ticks are keyed by checklist item
id ("ocho_atras.drill.0"), not by video, so a drill ticked while learning one
video shows as done in every video with the same figure. Sessions are what
the learner logs after practising: when, how long, which figures, a note.

Records made before there were accounts have no user; the first account
made claims them (`claim_unowned`).
"""

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    user_id    INTEGER,
    item_id    TEXT NOT NULL,
    lesson     TEXT,
    checked_at TEXT NOT NULL,
    PRIMARY KEY (user_id, item_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson   TEXT,
    day      TEXT NOT NULL,        -- YYYY-MM-DD
    minutes  INTEGER NOT NULL,
    figures  TEXT NOT NULL,        -- JSON list of figure keys
    note     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    user_id  INTEGER
);
"""


@dataclass
class Session:
    id: int
    lesson: str | None
    day: str
    minutes: int
    figures: list[str]
    note: str
    created_at: str


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def _migrate(db: sqlite3.Connection) -> None:
    """records.db from before accounts: add the user column (left empty until claimed)."""
    if (cols := _columns(db, "checks")) and "user_id" not in cols:
        # item_id was the primary key on its own: rebuild the table with (user_id, item_id)
        db.execute("ALTER TABLE checks RENAME TO checks_v1")
    if (cols := _columns(db, "sessions")) and "user_id" not in cols:
        db.execute("ALTER TABLE sessions ADD COLUMN user_id INTEGER")
    db.executescript(SCHEMA)
    if _columns(db, "checks_v1"):
        db.execute("INSERT OR IGNORE INTO checks (user_id, item_id, lesson, checked_at) "
                   "SELECT NULL, item_id, lesson, checked_at FROM checks_v1")
        db.execute("DROP TABLE checks_v1")


class Records:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            _migrate(db)

    def _db(self) -> sqlite3.Connection:
        # one connection per call: the web server handles requests on several threads
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def claim_unowned(self, user: int) -> None:
        """Give records from before accounts existed to `user` (the first account)."""
        with self._db() as db:
            db.execute("UPDATE checks SET user_id = ? WHERE user_id IS NULL", (user,))
            db.execute("UPDATE sessions SET user_id = ? WHERE user_id IS NULL", (user,))

    # -- checklist ------------------------------------------------------------

    def checks(self, user: int) -> dict[str, str]:
        """item id -> when it was ticked."""
        with self._db() as db:
            return {r["item_id"]: r["checked_at"]
                    for r in db.execute("SELECT item_id, checked_at FROM checks WHERE user_id = ?", (user,))}

    def set_check(self, user: int, item_id: str, checked: bool, lesson: str | None = None) -> None:
        with self._db() as db:
            if checked:
                db.execute("INSERT INTO checks (user_id, item_id, lesson, checked_at) VALUES (?, ?, ?, ?) "
                           "ON CONFLICT(user_id, item_id) DO NOTHING", (user, item_id, lesson, _now()))
            else:
                db.execute("DELETE FROM checks WHERE user_id = ? AND item_id = ?", (user, item_id))

    # -- sessions -------------------------------------------------------------

    def sessions(self, user: int) -> list[Session]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM sessions WHERE user_id = ? ORDER BY day DESC, id DESC",
                              (user,)).fetchall()
        return [Session(id=r["id"], lesson=r["lesson"], day=r["day"], minutes=r["minutes"],
                        figures=json.loads(r["figures"]), note=r["note"], created_at=r["created_at"])
                for r in rows]

    def add_session(self, user: int, *, day: str | None, minutes: int, figures: list[str], note: str = "",
                    lesson: str | None = None) -> Session:
        day = day or date.today().isoformat()
        date.fromisoformat(day)  # ValueError on a malformed day
        if not 1 <= int(minutes) <= 24 * 60:
            raise ValueError("연습 시간은 1분에서 24시간 사이여야 합니다")
        with self._db() as db:
            cur = db.execute(
                "INSERT INTO sessions (user_id, lesson, day, minutes, figures, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user, lesson, day, int(minutes), json.dumps(list(figures), ensure_ascii=False), note.strip(), _now()))
            sid = cur.lastrowid
        return next(s for s in self.sessions(user) if s.id == sid)

    def delete_session(self, user: int, session_id: int) -> bool:
        """Only the user's own sessions: someone else's id deletes nothing."""
        with self._db() as db:
            return db.execute("DELETE FROM sessions WHERE id = ? AND user_id = ?", (session_id, user)).rowcount > 0

    def export(self, user: int) -> dict:
        return {"checks": self.checks(user), "sessions": [asdict(s) for s in self.sessions(user)]}
