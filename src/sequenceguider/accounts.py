"""Accounts for the web app: sign-up, log-in, and the login sessions behind the cookie.

Users and their login sessions live in the same SQLite file as the practice
records (records.db), so a home folder is still one folder to back up.

Passwords are stored as scrypt hashes (standard library, no extra package).
A login hands the browser a random token; only its SHA-256 is stored, so a
copied database doesn't let anyone log in.

The first account made is the admin: it can start analyses and add local
video files, which read files on this computer. Later accounts are members:
they learn from the shared library and keep their own records.
"""

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS logins (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""

SESSION_DAYS = 30
_USERNAME = re.compile(r"^[\w.-]{2,32}$")
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


@dataclass
class User:
    id: int
    username: str
    display_name: str
    is_admin: bool
    created_at: str

    def public(self) -> dict:
        return {"id": self.id, "username": self.username, "display_name": self.display_name,
                "is_admin": self.is_admin}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(digest) // 2)
    except ValueError:
        return False
    return hmac.compare_digest(got.hex(), digest)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


class Accounts:
    def __init__(self, path: Path, signup_open: bool = True):
        self.path = path
        self.signup_open = signup_open
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)
        self._failures: dict[str, list[float]] = {}  # "ip|username" -> times of recent failed logins
        self._lock = threading.Lock()

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    @staticmethod
    def _user(row: sqlite3.Row) -> User:
        return User(id=row["id"], username=row["username"], display_name=row["display_name"],
                    is_admin=bool(row["is_admin"]), created_at=row["created_at"])

    # -- accounts ---------------------------------------------------------------

    def count(self) -> int:
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def can_sign_up(self) -> bool:
        """The first account can always be made (it is the admin); later ones only while sign-up is open."""
        return self.signup_open or self.count() == 0

    def sign_up(self, username: str, password: str, display_name: str = "") -> User:
        username = username.strip()
        if not self.can_sign_up():
            raise PermissionError("지금은 가입을 받지 않아요. 관리자에게 계정을 만들어 달라고 해 주세요")
        if not _USERNAME.match(username):
            raise ValueError("아이디는 2~32자의 글자·숫자·밑줄(_)·점(.)·하이픈(-)으로 만들어 주세요")
        if len(password) < 8:
            raise ValueError("비밀번호는 8자 이상이어야 해요")
        if len(password) > 256:
            raise ValueError("비밀번호가 너무 길어요")
        display_name = display_name.strip()[:40] or username
        with self._lock, self._db() as db:  # the lock: two first sign-ups at once can't both be admin
            first = db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
            try:
                cur = db.execute(
                    "INSERT INTO users (username, display_name, password_hash, is_admin, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (username, display_name, hash_password(password), int(first), _now().isoformat()))
            except sqlite3.IntegrityError:
                raise ValueError("이미 있는 아이디예요") from None
            row = db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
        return self._user(row)

    def get(self, username: str) -> User | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
        return self._user(row) if row else None

    def users(self) -> list[User]:
        with self._db() as db:
            return [self._user(r) for r in db.execute("SELECT * FROM users ORDER BY id")]

    def set_password(self, username: str, password: str) -> None:
        if len(password) < 8:
            raise ValueError("비밀번호는 8자 이상이어야 해요")
        with self._db() as db:
            row = db.execute("SELECT id FROM users WHERE username = ?", (username.strip(),)).fetchone()
            if row is None:
                raise ValueError("없는 아이디예요")
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), row["id"]))
            db.execute("DELETE FROM logins WHERE user_id = ?", (row["id"],))  # log out everywhere

    def set_admin(self, username: str, is_admin: bool) -> None:
        with self._db() as db:
            if db.execute("UPDATE users SET is_admin = ? WHERE username = ?",
                          (int(is_admin), username.strip())).rowcount == 0:
                raise ValueError("없는 아이디예요")

    # -- logging in ---------------------------------------------------------------

    def _too_many(self, key: str) -> bool:
        cutoff = time.monotonic() - 15 * 60
        with self._lock:
            recent = [t for t in self._failures.get(key, []) if t > cutoff]
            self._failures[key] = recent
            return len(recent) >= 10

    def log_in(self, username: str, password: str, client: str = "") -> tuple[User, str]:
        """(user, token for the cookie). Ten wrong passwords in 15 minutes lock that name from that address."""
        key = f"{client}|{username.strip().lower()}"
        if self._too_many(key):
            raise PermissionError("비밀번호를 여러 번 틀렸어요. 15분 뒤에 다시 해 주세요")
        with self._db() as db:
            row = db.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
        # hash even for an unknown name, so the response time doesn't tell which names exist
        ok = verify_password(password, row["password_hash"] if row else hash_password("x"))
        if row is None or not ok:
            with self._lock:
                self._failures.setdefault(key, []).append(time.monotonic())
            raise ValueError("아이디나 비밀번호가 맞지 않아요")
        with self._lock:
            self._failures.pop(key, None)
        return self._user(row), self.start_session(row["id"])

    def start_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = _now()
        with self._db() as db:
            db.execute("DELETE FROM logins WHERE expires_at < ?", (now.isoformat(),))
            db.execute("INSERT INTO logins (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                       (_token_hash(token), user_id, now.isoformat(),
                        (now + timedelta(days=SESSION_DAYS)).isoformat()))
        return token

    def user_for(self, token: str | None) -> User | None:
        if not token:
            return None
        with self._db() as db:
            row = db.execute(
                "SELECT users.* FROM logins JOIN users ON users.id = logins.user_id "
                "WHERE logins.token_hash = ? AND logins.expires_at > ?",
                (_token_hash(token), _now().isoformat())).fetchone()
        return self._user(row) if row else None

    def log_out(self, token: str | None) -> None:
        if token:
            with self._db() as db:
                db.execute("DELETE FROM logins WHERE token_hash = ?", (_token_hash(token),))
