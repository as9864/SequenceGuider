"""Practice records: checklist ticks, practice sessions, review schedule and practica logs, in one SQLite file.

Ticks are keyed by checklist item id ("ocho_atras.drill.0"), not by video, so
a drill ticked while learning one video shows as done in every video with the
same figure. Sessions are what the learner logs after practising: when, how
long, which figures, a note.

Reviews are the spaced-repetition state of each review card (review.py makes the
cards from lesson notes and guides; only their schedule is stored here). Practicas
are logs of practica (쁘락) nights: where, with whom, what was danced and how it went.
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
CREATE TABLE IF NOT EXISTS reviews (
    card_id  TEXT PRIMARY KEY,     -- review.py card id
    lesson   TEXT,
    step     INTEGER NOT NULL,     -- remembered this many times in a row (index into review.INTERVALS)
    due      TEXT NOT NULL,        -- YYYY-MM-DD
    first_at TEXT NOT NULL,        -- YYYY-MM-DD it was first reviewed
    last_at  TEXT NOT NULL,        -- YYYY-MM-DD it was last reviewed
    lapses   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS review_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id    TEXT NOT NULL,
    day        TEXT NOT NULL,
    remembered INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS daily (
    day     TEXT PRIMARY KEY,      -- the "today's one sequence" picked for that day
    card_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS practicas (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    day        TEXT NOT NULL,
    place      TEXT NOT NULL DEFAULT '',
    minutes    INTEGER,
    partners   TEXT NOT NULL DEFAULT '',
    items      TEXT NOT NULL,      -- JSON list of {label, figure, lesson, feel, note}
    note       TEXT NOT NULL DEFAULT '',
    next_time  TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""

FEELS = ("good", "ok", "confusing")  # 잘 됐어요 / 해 봤어요 / 헷갈려요


@dataclass
class Session:
    id: int
    lesson: str | None
    day: str
    minutes: int
    figures: list[str]
    note: str
    created_at: str


@dataclass
class Practica:
    id: int
    day: str
    place: str
    minutes: int | None
    partners: str
    items: list[dict]
    note: str
    next_time: str
    created_at: str


def _clean_items(items) -> list[dict]:
    if not isinstance(items, list):
        raise ValueError("연습한 것은 목록으로 보내 주세요")
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        label = str(it.get("label") or "").strip()[:120]
        figure = str(it.get("figure") or "").strip() or None
        if not label and not figure:
            continue
        feel = it.get("feel") if it.get("feel") in FEELS else "ok"
        out.append({"label": label or figure, "figure": figure, "lesson": (str(it.get("lesson") or "").strip() or None),
                    "feel": feel, "note": str(it.get("note") or "").strip()[:500]})
    return out


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

    # -- review schedule ------------------------------------------------------

    def reviews(self) -> dict[str, dict]:
        """card id -> {step, due, first_at, last_at, lapses}."""
        with self._db() as db:
            return {r["card_id"]: {k: r[k] for k in ("lesson", "step", "due", "first_at", "last_at", "lapses")}
                    for r in db.execute("SELECT * FROM reviews")}

    def save_review(self, card_id: str, *, lesson: str | None, step: int, due: str, day: str, remembered: bool) -> None:
        with self._db() as db:
            db.execute("INSERT INTO reviews (card_id, lesson, step, due, first_at, last_at, lapses) VALUES (?, ?, ?, ?, ?, ?, ?) "
                       "ON CONFLICT(card_id) DO UPDATE SET step = excluded.step, due = excluded.due, last_at = excluded.last_at, "
                       "lapses = reviews.lapses + excluded.lapses",
                       (card_id, lesson, step, due, day, day, 0 if remembered else 1))
            db.execute("INSERT INTO review_log (card_id, day, remembered) VALUES (?, ?, ?)", (card_id, day, int(remembered)))

    def review_log(self) -> list[dict]:
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT card_id, day, remembered FROM review_log ORDER BY id")]

    def daily_pick(self, day: str) -> str | None:
        with self._db() as db:
            row = db.execute("SELECT card_id FROM daily WHERE day = ?", (day,)).fetchone()
        return row["card_id"] if row else None

    def set_daily_pick(self, day: str, card_id: str) -> None:
        with self._db() as db:
            db.execute("INSERT INTO daily (day, card_id) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET card_id = excluded.card_id",
                       (day, card_id))

    # -- practicas --------------------------------------------------------------

    def practicas(self) -> list[Practica]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM practicas ORDER BY day DESC, id DESC").fetchall()
        return [Practica(id=r["id"], day=r["day"], place=r["place"], minutes=r["minutes"], partners=r["partners"],
                         items=json.loads(r["items"]), note=r["note"], next_time=r["next_time"], created_at=r["created_at"])
                for r in rows]

    def _practica_values(self, day, place, minutes, partners, items, note, next_time) -> tuple:
        day = day or date.today().isoformat()
        date.fromisoformat(day)  # ValueError on a malformed day
        if minutes in ("", None):
            minutes = None
        elif not 1 <= int(minutes) <= 24 * 60:
            raise ValueError("시간은 1분에서 24시간 사이여야 합니다")
        items = _clean_items(items)
        note, next_time = str(note or "").strip(), str(next_time or "").strip()
        if not items and not note and not next_time:
            raise ValueError("연습한 것이나 메모를 하나는 적어 주세요")
        return (day, str(place or "").strip()[:80], None if minutes is None else int(minutes),
                str(partners or "").strip()[:200], json.dumps(items, ensure_ascii=False), note, next_time)

    def add_practica(self, *, day: str | None = None, place: str = "", minutes=None, partners: str = "",
                     items=(), note: str = "", next_time: str = "") -> Practica:
        values = self._practica_values(day, place, minutes, partners, list(items), note, next_time)
        with self._db() as db:
            pid = db.execute("INSERT INTO practicas (day, place, minutes, partners, items, note, next_time, created_at) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (*values, _now())).lastrowid
            db.execute("DELETE FROM daily WHERE day >= ?", (values[0],))  # a new confusion may change today's pick
        return next(p for p in self.practicas() if p.id == pid)

    def update_practica(self, practica_id: int, *, day: str | None = None, place: str = "", minutes=None,
                        partners: str = "", items=(), note: str = "", next_time: str = "") -> Practica:
        values = self._practica_values(day, place, minutes, partners, list(items), note, next_time)
        with self._db() as db:
            n = db.execute("UPDATE practicas SET day = ?, place = ?, minutes = ?, partners = ?, items = ?, note = ?, "
                           "next_time = ? WHERE id = ?", (*values, practica_id)).rowcount
            db.execute("DELETE FROM daily WHERE day >= ?", (values[0],))
        if not n:
            raise KeyError("없는 쁘락 기록입니다")
        return next(p for p in self.practicas() if p.id == practica_id)

    def delete_practica(self, practica_id: int) -> bool:
        with self._db() as db:
            return db.execute("DELETE FROM practicas WHERE id = ?", (practica_id,)).rowcount > 0

    def export(self) -> dict:
        return {"checks": self.checks(), "sessions": [asdict(s) for s in self.sessions()],
                "reviews": self.reviews(), "review_log": self.review_log(),
                "practicas": [asdict(p) for p in self.practicas()]}
