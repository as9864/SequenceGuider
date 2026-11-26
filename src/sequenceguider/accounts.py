"""Accounts for the web app: sign-up, log-in, and the login sessions behind the cookie.

Users and their login sessions live in the same SQLite file as the practice
records (records.db), so a home folder is still one folder to back up.

Passwords are stored as scrypt hashes (standard library, no extra package).
A login hands the browser a random token; only its SHA-256 is stored, so a
copied database doesn't let anyone log in.

The first account made is the admin: it can start analyses and add local
video files, which read files on this computer. Later accounts are members:
they learn from the shared library and keep their own records.

Sign-up is "open" (anyone who can reach the page), "invite" (needs a one-time
code an admin made: the setting for a server on the internet) or "closed"
(admins make accounts with `sequenceguider users add`).
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

from sequenceguider.db import connect, enable_wal

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
CREATE TABLE IF NOT EXISTS invites (
    code_hash  TEXT PRIMARY KEY,
    note       TEXT NOT NULL DEFAULT '',
    created_by INTEGER,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    used_at    TEXT
);
"""

SIGNUP_MODES = ("open", "invite", "closed")
INVITE_DAYS = 14

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


def _norm_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", code.upper())


class Accounts:
    def __init__(self, path: Path, signup: str | bool = "open"):
        self.path = path
        if isinstance(signup, bool):  # the old --signup/--no-signup switch
            signup = "open" if signup else "closed"
        if signup not in SIGNUP_MODES:
            raise ValueError(f"가입 방식은 {', '.join(SIGNUP_MODES)} 중 하나예요")
        self.signup = signup
        enable_wal(path)
        with self._db() as db:
            db.executescript(SCHEMA)
        self._failures: dict[str, list[float]] = {}  # "ip|username" -> times of recent failed logins
        self._lock = threading.Lock()

    @property
    def signup_open(self) -> bool:
        return self.signup == "open"

    def _db(self) -> sqlite3.Connection:
        return connect(self.path)

    @staticmethod
    def _user(row: sqlite3.Row) -> User:
        return User(id=row["id"], username=row["username"], display_name=row["display_name"],
                    is_admin=bool(row["is_admin"]), created_at=row["created_at"])

    # -- accounts ---------------------------------------------------------------

    def count(self) -> int:
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def can_sign_up(self) -> bool:
        """The first account can always be made (it is the admin); later ones unless sign-up is closed."""
        return self.signup != "closed" or self.count() == 0

    def needs_invite(self) -> bool:
        return self.signup == "invite" and self.count() > 0

    def sign_up(self, username: str, password: str, display_name: str = "", invite: str = "",
                *, by_admin: bool = False) -> User:
        """`by_admin`: an admin making the account (`users add`), which needs no invite and works while closed."""
        username = username.strip()
        if not by_admin and not self.can_sign_up():
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
            code = None
            if self.signup == "invite" and not first and not by_admin:
                code = db.execute("SELECT code_hash FROM invites WHERE code_hash = ? AND used_by IS NULL "
                                  "AND expires_at > ?", (_token_hash(_norm_code(invite)), _now().isoformat())).fetchone()
                if code is None:
                    raise ValueError("초대 코드가 맞지 않거나 이미 쓰였어요")
            try:
                cur = db.execute(
                    "INSERT INTO users (username, display_name, password_hash, is_admin, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (username, display_name, hash_password(password), int(first), _now().isoformat()))
            except sqlite3.IntegrityError:
                raise ValueError("이미 있는 아이디예요") from None
            if code is not None:
                db.execute("UPDATE invites SET used_by = ?, used_at = ? WHERE code_hash = ?",
                           (cur.lastrowid, _now().isoformat(), code["code_hash"]))
            row = db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
        return self._user(row)

    # -- invites ----------------------------------------------------------------

    def create_invite(self, created_by: int | None = None, note: str = "", days: int = INVITE_DAYS) -> str:
        """A one-time sign-up code, like "K7QM-2XPA-W9RT". Only its hash is kept, so it is shown once."""
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I to misread
        raw = "".join(secrets.choice(alphabet) for _ in range(12))
        now = _now()
        with self._db() as db:
            db.execute("INSERT INTO invites (code_hash, note, created_by, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                       (_token_hash(raw), note.strip()[:80], created_by, now.isoformat(),
                        (now + timedelta(days=days)).isoformat()))
        return "-".join(raw[i:i + 4] for i in range(0, 12, 4))

    def invites(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT invites.note, invites.created_at, invites.expires_at, invites.used_at, "
                              "users.username AS used_by FROM invites LEFT JOIN users ON users.id = invites.used_by "
                              "ORDER BY invites.created_at DESC").fetchall()
        return [dict(r) for r in rows]

    # -- leaving ----------------------------------------------------------------

    def delete_user(self, user_id: int, password: str | None = None) -> None:
        """Remove the account and its logins. With `password`, it must match (a person deleting themselves).
        The last admin can't leave while there are other accounts: nobody could run the server then."""
        with self._lock, self._db() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                raise ValueError("없는 계정이에요")
            if password is not None and not verify_password(password, row["password_hash"]):
                raise ValueError("비밀번호가 맞지 않아요")
            if row["is_admin"]:
                admins = db.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0]
                others = db.execute("SELECT COUNT(*) FROM users WHERE id != ?", (user_id,)).fetchone()[0]
                if admins == 1 and others:
                    raise PermissionError("마지막 관리자예요. 다른 사람을 관리자로 정한 뒤에 탈퇴할 수 있어요")
            db.execute("DELETE FROM users WHERE id = ?", (user_id,))

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
