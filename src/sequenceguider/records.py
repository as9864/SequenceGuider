"""Practice records: checklist ticks and practice sessions, in one SQLite file.

Ticks are keyed by checklist item id ("ocho_atras.drill.0"), not by video, so
a drill ticked while learning one video shows as done in every video with the
same figure. Sessions are what the learner logs after practising: when, how
long, which figures, a note.
"""

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    item_id    TEXT PRIMARY KEY,
    lesson     TEXT,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson   TEXT,
    day      TEXT NOT NULL,        -- YYYY-MM-DD
    minutes  INTEGER NOT NULL,
    figures  TEXT NOT NULL,        -- JSON list of figure keys
    note     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
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


class Records:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)

    def _db(self) -> sqlite3.Connection:
        # one connection per call: the web server handles requests on several threads
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    # -- checklist ------------------------------------------------------------

    def checks(self) -> dict[str, str]:
        """item id -> when it was ticked."""
        with self._db() as db:
            return {r["item_id"]: r["checked_at"] for r in db.execute("SELECT item_id, checked_at FROM checks")}

    def set_check(self, item_id: str, checked: bool, lesson: str | None = None) -> None:
        with self._db() as db:
            if checked:
                db.execute("INSERT INTO checks (item_id, lesson, checked_at) VALUES (?, ?, ?) "
                           "ON CONFLICT(item_id) DO NOTHING", (item_id, lesson, _now()))
            else:
                db.execute("DELETE FROM checks WHERE item_id = ?", (item_id,))

    # -- sessions -------------------------------------------------------------

    def sessions(self) -> list[Session]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM sessions ORDER BY day DESC, id DESC").fetchall()
        return [Session(id=r["id"], lesson=r["lesson"], day=r["day"], minutes=r["minutes"],
                        figures=json.loads(r["figures"]), note=r["note"], created_at=r["created_at"])
                for r in rows]

    def add_session(self, *, day: str | None, minutes: int, figures: list[str], note: str = "",
                    lesson: str | None = None) -> Session:
        day = day or date.today().isoformat()
        date.fromisoformat(day)  # ValueError on a malformed day
        if not 1 <= int(minutes) <= 24 * 60:
            raise ValueError("연습 시간은 1분에서 24시간 사이여야 합니다")
        with self._db() as db:
            cur = db.execute(
                "INSERT INTO sessions (lesson, day, minutes, figures, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (lesson, day, int(minutes), json.dumps(list(figures), ensure_ascii=False), note.strip(), _now()))
            sid = cur.lastrowid
        return next(s for s in self.sessions() if s.id == sid)

    def delete_session(self, session_id: int) -> bool:
        with self._db() as db:
            return db.execute("DELETE FROM sessions WHERE id = ?", (session_id,)).rowcount > 0

    def export(self) -> dict:
        return {"checks": self.checks(), "sessions": [asdict(s) for s in self.sessions()]}
