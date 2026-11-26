"""`sequenceguider serve`: a local web app to learn from analysed videos and keep practice records.

Everything lives in one home folder:
    <home>/<name>_sequenceguider/   one lesson each (what `analyze` and/or `notes` write)
        media.json                  more videos/recordings on the same lesson (e.g. the final demo)
        media/<id>/                 what `notes`/`analyze` wrote for one of them, if it was analysed
    <home>/records.db               accounts and logins (accounts.py), each user's checklist
                                    ticks and practice sessions (records.py)
    <home>/.jobs/<id>.log           output of analyses started from the web page

Standard library only (http.server + sqlite3). Every page but the app shell
and the sign-up/log-in API needs a login (a cookie). The library is shared by
everyone with an account; records are each user's own. Only admins can start
analyses or add media, since those read video files on this computer and
spend its CPU (and the Claude API key, for notes).
"""

import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from sequenceguider.accounts import SESSION_DAYS, Accounts, User
from sequenceguider.db import connect, enable_wal
from sequenceguider.records import Records

APP_HTML = Path(__file__).parent / "web" / "app.html"
COOKIE = "sg_session"
_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".ogv"}
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".oga", ".opus", ".flac", ".weba"}
# Windows' registry often has no (or an odd) type for these, and <audio> needs a playable one
for _ext, _type in {".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".aac": "audio/aac", ".opus": "audio/ogg",
                    ".flac": "audio/flac", ".weba": "audio/webm", ".wav": "audio/wav"}.items():
    mimetypes.add_type(_type, _ext)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


MEDIA_JSON = "media.json"


def read_media(folder: Path) -> dict:
    """media.json: {"title": ..., "items": [{id, title, source, media, analysis, added_at}]}"""
    data = _read_json(folder / MEDIA_JSON) or {}
    return {"title": data.get("title"), "items": [i for i in data.get("items", []) if isinstance(i, dict) and "id" in i]}


def write_media(folder: Path, data: dict) -> None:
    tmp = folder / (MEDIA_JSON + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(folder / MEDIA_JSON)


def new_media_item(source: str, title: str = "") -> dict:
    """A video or recording to keep on a lesson: a local file (left where it is) or a YouTube link."""
    source = source.strip().strip('"')
    if not source:
        raise ValueError("유튜브 링크나 영상·녹음 파일 경로를 넣어 주세요")
    if re.match(r"^https?://", source, re.IGNORECASE):
        media, name = "youtube", "유튜브 영상"
    else:
        path = Path(source).expanduser()
        if not path.is_file():
            raise ValueError(f"파일이 없습니다: {source}")
        ext = path.suffix.lower()
        if ext not in VIDEO_EXTS | AUDIO_EXTS:
            raise ValueError(f"영상·녹음 파일이 아닙니다: {path.name}")
        media, name, source = ("audio" if ext in AUDIO_EXTS else "video"), path.stem, str(path.resolve())
    return {"id": uuid.uuid4().hex[:8], "title": title.strip() or name, "source": source, "media": media,
            "analysis": "none", "added_at": datetime.now().isoformat(timespec="seconds")}


def lesson_media(folder: Path) -> list[dict]:
    """The lesson's extra media, each with what its analysis (if any) has produced so far."""
    out = []
    for item in read_media(folder)["items"]:
        sub = folder / "media" / item["id"]
        notes = _read_json(sub / "notes.json")
        out.append({**item,
                    "has_notes": notes is not None,
                    "has_guide": (sub / "index.html").is_file() and (sub / "result.json").is_file(),
                    "duration_s": (notes or {}).get("duration_s"),
                    "checklist": [{"id": p["id"], "text": p.get("text", "")}
                                  for p in (notes or {}).get("practice", []) if "id" in p]})
    return out


def find_lessons(home: Path) -> list[dict]:
    """Every lesson folder directly under `home`, newest first.

    A folder is a lesson if `analyze` wrote its guide there (result.json + index.html),
    `notes` wrote its notes (notes.json), media was added to it (media.json), or any of these.
    """
    lessons = []
    for folder in sorted(p for p in home.iterdir() if p.is_dir() and not p.name.startswith(".")):
        result = folder / "result.json"
        data = _read_json(result) if (folder / "index.html").is_file() else None
        notes = _read_json(folder / "notes.json")
        media = lesson_media(folder)
        if data is None and notes is None and not media:
            continue
        data = data or {}
        figures = []
        for f in data.get("figures", []):
            lesson = f.get("lesson") or {}
            figures.append({
                "order": f.get("order"), "figure": f.get("figure"), "name_ko": f.get("name_ko"),
                "count": f.get("count", 1), "start_s": f.get("start_s"), "end_s": f.get("end_s"),
                "key_point": lesson.get("key_point", ""),
                "checklist": lesson.get("checklist", []),
            })
        stamps = [p.stat().st_mtime for p in (result, folder / "notes.json", folder / MEDIA_JSON) if p.is_file()]
        lessons.append({
            "id": folder.name,
            "title": ((notes or {}).get("title") or data.get("title") or read_media(folder)["title"]
                      or folder.name.removesuffix("_sequenceguider")),
            "source_url": data.get("source_url") or (notes or {}).get("source_url"),
            "role": data.get("role", "all"),
            "figures": figures,
            "has_guide": bool(data),
            "notes": None if notes is None else {
                "summary": notes.get("summary", ""),
                "duration_s": notes.get("duration_s"),
                "key_points": len(notes.get("key_points", [])),
                "figures": [{"figure": k, "name_ko": (notes.get("figure_names") or {}).get(k, k)}
                            for k in dict.fromkeys(k for s in notes.get("sections", []) for k in s.get("figures", []))],
                "checklist": [{"id": p["id"], "text": p.get("text", "")} for p in notes.get("practice", []) if "id" in p],
            },
            "media": media,
            "updated_at": datetime.fromtimestamp(max(stamps)).isoformat(timespec="seconds"),
        })
    return sorted(lessons, key=lambda x: x["updated_at"], reverse=True)


JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,          -- analyze | notes
    source      TEXT NOT NULL,
    sequence    TEXT NOT NULL DEFAULT '',
    role        TEXT NOT NULL DEFAULT 'all',
    view        TEXT NOT NULL DEFAULT 'side',
    lesson      TEXT,
    media       TEXT,                   -- the lesson's media.json item this job analyses
    command     TEXT NOT NULL,          -- JSON argv
    status      TEXT NOT NULL,          -- queued | running | done | failed
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status, created_at);
"""
INTERRUPTED = "서버가 다시 시작되어 이 작업이 중단됐어요. 다시 분석해 주세요."


@dataclass
class Job:
    id: str
    kind: str  # analyze | notes
    source: str
    sequence: str
    role: str
    view: str
    created_at: str
    status: str = "queued"  # queued | running | done | failed
    lesson: str | None = None
    media: str | None = None
    command: list[str] = field(default_factory=list, repr=False)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Jobs:
    """Analyses started from the page: a queue in records.db, run as `sequenceguider analyze|notes` subprocesses.

    At most `max_parallel` run at once (one by default: pose estimation and speech-to-text each fill the
    PC's GPU/CPU, and two at once only makes both slow). The rest wait their turn, in order, and a queue
    kept in the database survives a restart of the server. A job that was running when the server stopped
    is marked failed by `recover()`, since its process is gone.
    """

    def __init__(self, home: Path, max_parallel: int = 1):
        self.home = home
        self.dir = home / ".jobs"
        self.db_path = home / "records.db"
        self.max_parallel = max(1, int(max_parallel))
        self.command = [sys.executable, "-m", "sequenceguider.cli"]  # tests swap in a stand-in
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.RLock()
        self._pumper: threading.Thread | None = None
        enable_wal(self.db_path)
        with connect(self.db_path) as db:
            db.executescript(JOBS_SCHEMA)

    def _db(self):
        return connect(self.db_path)

    def recover(self) -> None:
        """At server start: jobs left 'running' by the last run lost their process; queued ones carry on."""
        with self._lock, self._db() as db:
            stale = [r["id"] for r in db.execute("SELECT id FROM jobs WHERE status = 'running'")]
            db.execute("UPDATE jobs SET status = 'failed', finished_at = ? WHERE status = 'running'", (_now(),))
        for job_id in stale:
            try:
                with open(self.dir / f"{job_id}.log", "ab") as log:
                    log.write(("\n" + INTERRUPTED + "\n").encode("utf-8"))
            except OSError:
                pass
        self.pump()

    def start(self, source: str, sequence: str = "", role: str = "all", view: str = "side",
              kind: str = "analyze", speakers: str = "", *, out: Path | None = None,
              lesson: str | None = None, media: str | None = None) -> Job:
        """Check the request and put it in the queue (it starts right away if nothing else is running)."""
        source = source.strip().strip('"')
        if not source:
            raise ValueError("유튜브 링크나 영상·녹음 파일 경로를 넣어 주세요")
        if role not in ("all", "leader", "follower") or view not in ("side", "front"):
            raise ValueError("역할이나 촬영 방향 값이 올바르지 않습니다")
        if kind not in ("analyze", "notes"):
            raise ValueError("만들 것이 올바르지 않습니다")
        if not re.match(r"^https?://", source, re.IGNORECASE):
            path = Path(source).expanduser()
            if not path.is_file():
                raise ValueError(f"파일이 없습니다: {source}")
            if kind == "analyze" and not sequence.strip():
                raise ValueError("파일 영상은 시퀀스 순서를 적어 주세요")
            source = str(path.resolve())
            # results go in the home folder, where the library looks, not next to the video
            out = out or self.home / f"{path.stem}_sequenceguider"
        if kind == "notes":
            cmd = [*self.command, "notes", source, "--role", role]
            if speakers.strip():
                cmd += ["--speakers", speakers.strip()]
        else:
            cmd = [*self.command, "analyze", source, "--role", role, "--view", view]
            if sequence.strip():
                cmd += ["--sequence", sequence.strip()]
        if out is not None:
            cmd += ["--out", str(out)]
        job = Job(id=uuid.uuid4().hex[:8], kind=kind, source=source, sequence=sequence.strip(), role=role, view=view,
                  created_at=_now(), lesson=lesson, media=media, command=cmd)
        with self._db() as db:
            db.execute("INSERT INTO jobs (id, kind, source, sequence, role, view, lesson, media, command, status, "
                       "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
                       (job.id, kind, source, job.sequence, role, view, lesson, media,
                        json.dumps(cmd, ensure_ascii=False), job.created_at))
        self.pump()
        return job

    # -- running the queue ------------------------------------------------------

    def _launch(self, db, row) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        log = open(self.dir / f"{row['id']}.log", "wb")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        try:
            proc = subprocess.Popen(json.loads(row["command"]), cwd=self.home, stdout=log,
                                    stderr=subprocess.STDOUT, env=env)
        except OSError as e:
            log.write(f"시작하지 못했어요: {e}\n".encode("utf-8"))
            log.close()
            db.execute("UPDATE jobs SET status = 'failed', started_at = ?, finished_at = ? WHERE id = ?",
                       (_now(), _now(), row["id"]))
            return
        log.close()
        self._procs[row["id"]] = proc
        db.execute("UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?", (_now(), row["id"]))

    def pump(self, _loop: bool = False) -> bool:
        """Note finished processes and start queued jobs while there is room. True while work remains."""
        with self._lock, self._db() as db:
            for job_id, proc in list(self._procs.items()):
                if proc.poll() is None:
                    continue
                del self._procs[job_id]
                lesson = None
                if m := re.search(r"(?:리포트|노트): (.+)", self._raw_log(job_id)):
                    lesson = Path(m.group(1).strip()).parent.name
                db.execute("UPDATE jobs SET status = ?, finished_at = ?, lesson = COALESCE(lesson, ?) WHERE id = ?",
                           ("done" if proc.returncode == 0 else "failed", _now(), lesson, job_id))
            while len(self._procs) < self.max_parallel:
                row = db.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at, rowid LIMIT 1"
                                 ).fetchone()
                if row is None:
                    break
                self._launch(db, row)
            busy = bool(self._procs) or bool(
                db.execute("SELECT 1 FROM jobs WHERE status = 'queued' LIMIT 1").fetchone())
            if _loop and not busy:
                self._pumper = None  # under the lock: a job queued from now on starts a new loop
            elif busy and not _loop and (self._pumper is None or not self._pumper.is_alive()):
                # keeps the queue moving when nobody has the page open
                self._pumper = threading.Thread(target=self._pump_loop, name="sequenceguider-jobs", daemon=True)
                self._pumper.start()
            return busy

    def _pump_loop(self) -> None:
        while True:
            time.sleep(1)
            try:
                if not self.pump(_loop=True):
                    return
            except Exception:  # the home folder went away (tests) or the disk is unhappy: stop quietly
                self._pumper = None
                return

    # -- reading ------------------------------------------------------------------

    def _raw_log(self, job_id: str) -> str:
        try:
            return (self.dir / f"{job_id}.log").read_bytes().decode("utf-8", "replace")
        except OSError:
            return ""

    def _log(self, job_id: str) -> str:
        raw = self._raw_log(job_id)
        # progress bars redraw with \r: keep the last state of each line
        lines = [ln.split("\r")[-1] for ln in raw.replace("\r\n", "\n").split("\n")]  # Windows ends lines in \r\n
        return "\n".join(ln for ln in lines if ln.strip())[-4000:]

    def list(self, limit: int = 50) -> list[dict]:
        self.pump()
        with self._db() as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
            queued = [r["id"] for r in db.execute("SELECT id FROM jobs WHERE status = 'queued' "
                                                  "ORDER BY created_at, rowid")]
        out = []
        for r in rows:
            d = {k: r[k] for k in ("id", "kind", "source", "sequence", "role", "view", "lesson", "media", "status",
                                   "created_at", "started_at", "finished_at")}
            d["position"] = queued.index(r["id"]) + 1 if r["id"] in queued else None
            d["log"] = self._log(r["id"])
            out.append(d)
        return out


class App:
    def __init__(self, home: Path, signup: str | bool = "open", max_jobs: int = 1, *,
                 trust_proxy: bool = False, public_url: str = ""):
        """`trust_proxy`: requests come through a tunnel or proxy on this computer (cloudflared, Caddy), so
        the visitor's address is in its headers. `public_url`: the https address people use, if any."""
        self.home = home.resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.records = Records(self.home / "records.db")
        self.accounts = Accounts(self.home / "records.db", signup)
        self.jobs = Jobs(self.home, max_jobs)
        self.trust_proxy = trust_proxy
        self.public_url = public_url.rstrip("/")

    def sign_up(self, username: str, password: str, display_name: str = "", invite: str = "",
                *, by_admin: bool = False) -> User:
        user = self.accounts.sign_up(username, password, display_name, invite, by_admin=by_admin)
        if user.is_admin and len(self.accounts.users()) == 1:
            self.records.claim_unowned(user.id)  # the practice log kept before there were accounts
        return user

    def delete_account(self, user: User, password: str | None) -> None:
        """The account, its logins and everything it recorded (kept nowhere else). Lessons are shared and stay."""
        self.accounts.delete_user(user.id, password)
        self.records.delete_user_data(user.id)

    def healthy(self) -> bool:
        try:
            with connect(self.home / "records.db") as db:
                db.execute("SELECT 1 FROM users LIMIT 1").fetchall()
            return True
        except Exception:
            return False

    def lesson_file(self, lesson: str, rel: str) -> Path | None:
        base = (self.home / lesson).resolve()
        path = (base / rel).resolve()
        if base.parent != self.home or not path.is_relative_to(base) or not path.is_file():
            return None
        return path


    def notes_video(self, lesson: str) -> Path | None:
        """The lesson video (or audio recording) notes.json points at. It may live outside the
        lesson folder (a 2-hour file isn't copied), so this is the one file served from elsewhere."""
        base = (self.home / lesson).resolve()
        if base.parent != self.home:
            return None
        notes = _read_json(base / "notes.json") or {}
        ref = notes.get("video")
        if not ref:
            return None
        path = (base / ref).resolve()
        if path.suffix.lower() not in VIDEO_EXTS | AUDIO_EXTS or not path.is_file():
            return None
        return path

    # -- more media on one lesson: the class recording, then the final demo, ...

    def _lesson_dir(self, lesson: str) -> Path:
        base = (self.home / lesson).resolve()
        if base.parent != self.home or not base.is_dir():
            raise ValueError("없는 레슨입니다")
        return base

    def add_media(self, lesson: str, source: str, title: str = "", analysis: str = "none", **opts) -> dict:
        """Keep one more video/recording on the lesson; analyse it now only if asked (a demo often needn't be)."""
        base = self._lesson_dir(lesson)
        item = new_media_item(source, title)
        data = read_media(base)
        if analysis != "none":  # start first: a missing sequence etc. then adds nothing
            self._analyze(base, item, analysis, **opts)
        data["items"].append(item)
        write_media(base, data)
        return item

    def analyze_media(self, lesson: str, media_id: str, analysis: str, **opts) -> Job:
        """Analyse a media item later (it was added without analysis, or the analysis failed)."""
        base = self._lesson_dir(lesson)
        data = read_media(base)
        item = next((i for i in data["items"] if i["id"] == media_id), None)
        if item is None:
            raise ValueError("없는 영상입니다")
        job = self._analyze(base, item, analysis, **opts)
        write_media(base, data)
        return job

    def _analyze(self, base: Path, item: dict, analysis: str, role: str = "all", view: str = "side",
                 sequence: str = "", speakers: str = "") -> Job:
        if analysis not in ("notes", "analyze"):
            raise ValueError("분석 종류가 올바르지 않습니다")
        if analysis == "analyze" and item["media"] == "audio":
            raise ValueError("녹음 파일은 동작 분석을 할 수 없어요 (레슨 노트는 돼요)")
        job = self.jobs.start(item["source"], sequence, role, view, analysis, speakers,
                              out=base / "media" / item["id"], lesson=base.name, media=item["id"])
        item["analysis"] = analysis
        return job

    def remove_media(self, lesson: str, media_id: str) -> bool:
        """Off the lesson's list. The file itself, and anything analysed from it, stay on disk."""
        base = self._lesson_dir(lesson)
        data = read_media(base)
        items = [i for i in data["items"] if i["id"] != media_id]
        if len(items) == len(data["items"]):
            return False
        write_media(base, {**data, "items": items})
        return True

    def media_file(self, lesson: str, media_id: str) -> Path | None:
        try:
            base = self._lesson_dir(lesson)
        except ValueError:
            return None
        item = next((i for i in read_media(base)["items"] if i["id"] == media_id), None)
        if item is None or item["media"] == "youtube":
            return None
        path = Path(item["source"])
        if path.suffix.lower() not in VIDEO_EXTS | AUDIO_EXTS or not path.is_file():
            return None
        return path

    def create_lesson(self, source: str, title: str = "") -> str:
        """A lesson with just media, nothing analysed (more can be added, and analysed, later)."""
        item = new_media_item(source, title)
        stem = Path(item["source"]).stem if item["media"] != "youtube" else "youtube_" + item["id"]
        base = self.home / f"{stem}_sequenceguider"
        base.mkdir(exist_ok=True)
        data = read_media(base)
        data["title"] = data["title"] or item["title"]
        data["items"].append(item)
        write_media(base, data)
        return base.name


def _analysis_opts(body: dict) -> dict:
    return {k: str(body.get(k, d)) for k, d in (("role", "all"), ("view", "side"), ("sequence", ""), ("speakers", ""))}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SequenceGuider"

        def log_message(self, fmt, *args):  # quiet: the terminal is for the startup line
            pass

        def end_headers(self):
            # on every response, the API's and the lesson pages' alike
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Frame-Options", "SAMEORIGIN")  # lesson pages show in our own iframe only
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            if self._secure():
                self.send_header("Strict-Transport-Security", "max-age=15552000")
            super().end_headers()

        def _from_proxy(self) -> bool:
            # only a tunnel/proxy on this computer may tell us who the visitor is; anyone else could lie
            return app.trust_proxy and self.client_address[0] in ("127.0.0.1", "::1")

        def _client_ip(self) -> str:
            if self._from_proxy():
                ip = (self.headers.get("CF-Connecting-IP")
                      or (self.headers.get("X-Forwarded-For") or "").split(",")[0]).strip()
                if ip:
                    return ip
            return self.client_address[0]

        def _secure(self) -> bool:
            """Did the visitor use https? Then cookies are Secure and browsers are told to keep using https."""
            if app.public_url.startswith("https://"):
                return True
            if self._from_proxy():
                proto = (self.headers.get("X-Forwarded-Proto") or "").lower()
                return proto == "https" or '"https"' in (self.headers.get("Cf-Visitor") or "")
            return False

        # -- helpers ------------------------------------------------------

        def _json(self, data, status: int = 200, cookie: str | None = None) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if cookie is not None:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, message: str) -> None:
            self._json({"error": message}, status)

        def _token(self) -> str | None:
            try:
                morsel = SimpleCookie(self.headers.get("Cookie") or "").get(COOKIE)
            except CookieError:
                return None
            return morsel.value if morsel else None

        def _cookie(self, token: str, max_age: int) -> str:
            # HttpOnly: page scripts can't read it; SameSite=Lax: other sites can't post with it
            cookie = f"{COOKIE}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"
            return cookie + "; Secure" if self._secure() else cookie

        def _same_origin(self) -> bool:
            """A browser sends Origin on POST/PUT/DELETE; it must be this server (no cross-site requests)."""
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            netloc = urlparse(origin).netloc
            return netloc == self.headers.get("Host") or (bool(app.public_url) and netloc == urlparse(app.public_url).netloc)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            if n > 1 << 20:
                raise ValueError("요청이 너무 큽니다")
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("JSON 객체가 필요합니다")
            return data

        def _file(self, path: Path) -> None:
            size = path.stat().st_size
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype == "application/json":
                ctype += "; charset=utf-8"
            start, end = 0, size - 1
            status = HTTPStatus.OK
            # <video> needs byte ranges to seek (the timeline and ▶ buttons jump around)
            if (rng := self.headers.get("Range")) and (m := _RANGE.match(rng.strip())) and size:
                a, b = m.groups()
                if a:
                    start, end = int(a), min(int(b), size - 1) if b else size - 1
                elif b:
                    start = max(0, size - int(b))
                if start > end or start >= size:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                try:
                    while left > 0 and (chunk := f.read(min(1 << 16, left))):
                        self.wfile.write(chunk)
                        left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the browser cancels range requests all the time while seeking

        def _route(self, method: str) -> None:
            path = unquote(urlparse(self.path).path)
            try:
                if method != "GET" and not self._same_origin():
                    return self._error(403, "다른 사이트에서 온 요청은 받지 않아요")
                if method == "GET" and path == "/healthz":  # for an uptime monitor: no login, no details
                    ok = app.healthy()
                    return self._json({"ok": ok}, 200 if ok else 503)
                if method == "GET" and path in ("/", "/index.html"):
                    return self._file(APP_HTML)  # the shell is public: it shows the log-in screen itself
                if path.startswith("/api/auth/"):
                    return self._auth(method, path[len("/api/auth/"):])
                user = app.accounts.user_for(self._token())
                if user is None:
                    return self._error(401, "로그인이 필요해요")
                if not user.is_admin and method != "GET" and path.startswith(("/api/jobs", "/api/lessons/")):
                    return self._error(403, "분석을 시작하거나 영상을 추가·삭제하는 건 관리자만 할 수 있어요")
                if path == "/api/invites":
                    if not user.is_admin:
                        return self._error(403, "초대 코드는 관리자만 만들 수 있어요")
                    if method == "POST":
                        b = self._body()
                        code = app.accounts.create_invite(user.id, str(b.get("note", "")))
                        return self._json({"code": code}, 201)
                    if method == "GET":
                        return self._json(app.accounts.invites())
                if method == "GET" and (m := re.fullmatch(r"/lessons/([^/]+)/notes-video", path)):
                    f = app.notes_video(m.group(1))
                    return self._file(f) if f else self._error(404, "영상 파일을 찾지 못했습니다")
                if method == "GET" and (m := re.fullmatch(r"/lessons/([^/]+)/media-file/([^/]+)", path)):
                    f = app.media_file(*m.groups())
                    return self._file(f) if f else self._error(404, "영상 파일을 찾지 못했습니다")
                if method == "GET" and path.startswith("/lessons/"):
                    lesson, _, rel = path[len("/lessons/"):].partition("/")
                    f = app.lesson_file(lesson, rel or "index.html")
                    return self._file(f) if f else self._error(404, "없는 파일입니다")
                if path == "/api/lessons" and method == "GET":
                    return self._json(find_lessons(app.home))
                if path == "/api/checks" and method == "GET":
                    return self._json(app.records.checks(user.id))
                if path == "/api/checks" and method == "PUT":
                    b = self._body()
                    app.records.set_check(user.id, str(b["item_id"]), bool(b.get("checked")), b.get("lesson"))
                    return self._json(app.records.checks(user.id))
                if path == "/api/sessions" and method == "GET":
                    return self._json([asdict(s) for s in app.records.sessions(user.id)])
                if path == "/api/sessions" and method == "POST":
                    b = self._body()
                    s = app.records.add_session(user.id, day=b.get("day"), minutes=int(b.get("minutes") or 0),
                                                figures=[str(x) for x in b.get("figures", [])],
                                                note=str(b.get("note", "")), lesson=b.get("lesson"))
                    return self._json(asdict(s), 201)
                if (m := re.fullmatch(r"/api/sessions/(\d+)", path)) and method == "DELETE":
                    ok = app.records.delete_session(user.id, int(m.group(1)))
                    return self._json({"deleted": ok}, 200 if ok else 404)
                if path == "/api/export" and method == "GET":
                    return self._json(app.records.export(user.id))
                if path == "/api/jobs" and method == "GET":  # sources are paths on this computer: admins only
                    return self._json(app.jobs.list() if user.is_admin else [])
                if (m := re.fullmatch(r"/api/lessons/([^/]+)/media", path)) and method == "POST":
                    b = self._body()
                    item = app.add_media(m.group(1), str(b.get("source", "")), str(b.get("title", "")),
                                         str(b.get("analysis", "none")), **_analysis_opts(b))
                    return self._json(item, 201)
                if (m := re.fullmatch(r"/api/lessons/([^/]+)/media/([^/]+)/analyze", path)) and method == "POST":
                    b = self._body()
                    job = app.analyze_media(*m.groups(), str(b.get("analysis", "notes")), **_analysis_opts(b))
                    return self._json({"id": job.id}, 201)
                if (m := re.fullmatch(r"/api/lessons/([^/]+)/media/([^/]+)", path)) and method == "DELETE":
                    ok = app.remove_media(*m.groups())
                    return self._json({"deleted": ok}, 200 if ok else 404)
                if path == "/api/jobs" and method == "POST":
                    b = self._body()
                    if b.get("kind") == "none":  # just keep the video on a new lesson, analyse nothing
                        lesson = app.create_lesson(str(b.get("source", "")), str(b.get("title", "")))
                        return self._json({"lesson": lesson}, 201)
                    job = app.jobs.start(str(b.get("source", "")), str(b.get("sequence", "")),
                                         str(b.get("role", "all")), str(b.get("view", "side")),
                                         str(b.get("kind", "analyze")), str(b.get("speakers", "")))
                    return self._json({"id": job.id}, 201)
                return self._error(404, "없는 주소입니다")
            except PermissionError as e:
                return self._error(403, str(e))
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                return self._error(400, str(e))

        def _auth(self, method: str, action: str) -> None:
            if action == "me" and method == "GET":
                user = app.accounts.user_for(self._token())
                info = {"user": user.public() if user else None, "can_sign_up": app.accounts.can_sign_up(),
                        "needs_invite": app.accounts.needs_invite(), "first_account": app.accounts.count() == 0}
                return self._json(info, 200 if user else 401)
            if action == "me" and method == "DELETE":  # leave: the account and its records go
                user = app.accounts.user_for(self._token())
                if user is None:
                    return self._error(401, "로그인이 필요해요")
                app.delete_account(user, str(self._body().get("password", "")))
                return self._json({"deleted": True}, 200, self._cookie("", 0))
            if action == "signup" and method == "POST":
                b = self._body()
                user = app.sign_up(str(b.get("username", "")), str(b.get("password", "")),
                                   str(b.get("display_name", "")), str(b.get("invite", "")))
                token = app.accounts.start_session(user.id)
                return self._json({"user": user.public()}, 201, self._cookie(token, SESSION_DAYS * 86400))
            if action == "login" and method == "POST":
                b = self._body()
                user, token = app.accounts.log_in(str(b.get("username", "")), str(b.get("password", "")),
                                                  self._client_ip())
                return self._json({"user": user.public()}, 200, self._cookie(token, SESSION_DAYS * 86400))
            if action == "logout" and method == "POST":
                app.accounts.log_out(self._token())
                return self._json({"ok": True}, 200, self._cookie("", 0))
            return self._error(404, "없는 주소입니다")

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

        def do_DELETE(self):
            self._route("DELETE")

    return Handler


def make_server(home: Path, host: str = "127.0.0.1", port: int = 8765, signup: str | bool = "open",
                max_jobs: int = 1, *, trust_proxy: bool = False, public_url: str = "") -> ThreadingHTTPServer:
    app = App(home, signup, max_jobs, trust_proxy=trust_proxy, public_url=public_url)
    app.jobs.recover()  # this is the one server for the folder: jobs it left running are gone
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    server.app = app
    return server
