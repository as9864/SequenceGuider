"""`sequenceguider serve`: a local web app to learn from analysed videos and keep practice records.

Everything lives in one home folder:
    <home>/<name>_sequenceguider/   one lesson each (what `analyze` and/or `notes` write)
    <home>/records.db               checklist ticks and practice sessions (records.py)
    <home>/.jobs/<id>.log           output of analyses started from the web page

Standard library only (http.server + sqlite3). It binds to 127.0.0.1 by
default: it runs analyses and serves local files, so it is not meant to be
reachable from other machines.
"""

import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from sequenceguider.records import Records

APP_HTML = Path(__file__).parent / "web" / "app.html"
_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".ogv"}


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def find_lessons(home: Path) -> list[dict]:
    """Every lesson folder directly under `home`, newest first.

    A folder is a lesson if `analyze` wrote its guide there (result.json + index.html),
    `notes` wrote its notes (notes.json), or both.
    """
    lessons = []
    for folder in sorted(p for p in home.iterdir() if p.is_dir() and not p.name.startswith(".")):
        result = folder / "result.json"
        data = _read_json(result) if (folder / "index.html").is_file() else None
        notes = _read_json(folder / "notes.json")
        if data is None and notes is None:
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
        stamps = [p.stat().st_mtime for p in (result, folder / "notes.json") if p.is_file()]
        lessons.append({
            "id": folder.name,
            "title": (notes or {}).get("title") or data.get("title") or folder.name.removesuffix("_sequenceguider"),
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
            "updated_at": datetime.fromtimestamp(max(stamps)).isoformat(timespec="seconds"),
        })
    return sorted(lessons, key=lambda x: x["updated_at"], reverse=True)


@dataclass
class Job:
    id: str
    kind: str  # analyze | notes
    source: str
    sequence: str
    role: str
    view: str
    started_at: str
    status: str = "running"  # running | done | failed
    lesson: str | None = None
    log: str = ""
    _proc: subprocess.Popen | None = field(default=None, repr=False)


class Jobs:
    """Analyses started from the page, run as `sequenceguider analyze` subprocesses in `home`."""

    def __init__(self, home: Path):
        self.home = home
        self.dir = home / ".jobs"
        self._jobs: dict[str, Job] = {}
        self.command = [sys.executable, "-m", "sequenceguider.cli"]  # tests swap in a stand-in
        self._lock = threading.Lock()

    def start(self, source: str, sequence: str = "", role: str = "all", view: str = "side",
              kind: str = "analyze") -> Job:
        source = source.strip().strip('"')
        if not source:
            raise ValueError("유튜브 링크나 영상 파일 경로를 넣어 주세요")
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
            out = self.home / f"{path.stem}_sequenceguider"
        else:
            out = None
        if kind == "notes":
            cmd = [*self.command, "notes", source, "--role", role]
        else:
            cmd = [*self.command, "analyze", source, "--role", role, "--view", view]
            if sequence.strip():
                cmd += ["--sequence", sequence.strip()]
        if out is not None:
            cmd += ["--out", str(out)]
        self.dir.mkdir(parents=True, exist_ok=True)
        job = Job(id=uuid.uuid4().hex[:8], kind=kind, source=source, sequence=sequence.strip(), role=role, view=view,
                  started_at=datetime.now().isoformat(timespec="seconds"))
        log = open(self.dir / f"{job.id}.log", "wb")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        job._proc = subprocess.Popen(cmd, cwd=self.home, stdout=log, stderr=subprocess.STDOUT, env=env)
        log.close()
        with self._lock:
            self._jobs[job.id] = job
        return job

    def _refresh(self, job: Job) -> None:
        try:
            raw = (self.dir / f"{job.id}.log").read_bytes().decode("utf-8", "replace")
        except OSError:
            raw = ""
        # progress bars redraw with \r: keep the last state of each line
        lines = [ln.split("\r")[-1] for ln in raw.split("\n")]
        job.log = "\n".join(ln for ln in lines if ln.strip())[-4000:]
        if job.status == "running" and job._proc is not None and job._proc.poll() is not None:
            job.status = "done" if job._proc.returncode == 0 else "failed"
            if m := re.search(r"(?:리포트|노트): (.+)", raw):
                job.lesson = Path(m.group(1).strip()).parent.name

    def list(self) -> list[dict]:
        with self._lock:
            jobs = list(self._jobs.values())
        out = []
        for job in sorted(jobs, key=lambda j: j.started_at, reverse=True):
            self._refresh(job)
            out.append({k: v for k, v in vars(job).items() if not k.startswith("_")})
        return out


class App:
    def __init__(self, home: Path):
        self.home = home.resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.records = Records(self.home / "records.db")
        self.jobs = Jobs(self.home)

    def lesson_file(self, lesson: str, rel: str) -> Path | None:
        base = (self.home / lesson).resolve()
        path = (base / rel).resolve()
        if base.parent != self.home or not path.is_relative_to(base) or not path.is_file():
            return None
        return path


    def notes_video(self, lesson: str) -> Path | None:
        """The lesson video notes.json points at. It may live outside the lesson folder
        (a 2-hour file isn't copied), so this is the one file served from elsewhere."""
        base = (self.home / lesson).resolve()
        if base.parent != self.home:
            return None
        notes = _read_json(base / "notes.json") or {}
        ref = notes.get("video")
        if not ref:
            return None
        path = (base / ref).resolve()
        if path.suffix.lower() not in VIDEO_EXTS or not path.is_file():
            return None
        return path


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SequenceGuider"

        def log_message(self, fmt, *args):  # quiet: the terminal is for the startup line
            pass

        # -- helpers ------------------------------------------------------

        def _json(self, data, status: int = 200) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, message: str) -> None:
            self._json({"error": message}, status)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
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
                if method == "GET" and path in ("/", "/index.html"):
                    return self._file(APP_HTML)
                if method == "GET" and (m := re.fullmatch(r"/lessons/([^/]+)/notes-video", path)):
                    f = app.notes_video(m.group(1))
                    return self._file(f) if f else self._error(404, "영상 파일을 찾지 못했습니다")
                if method == "GET" and path.startswith("/lessons/"):
                    lesson, _, rel = path[len("/lessons/"):].partition("/")
                    f = app.lesson_file(lesson, rel or "index.html")
                    return self._file(f) if f else self._error(404, "없는 파일입니다")
                if path == "/api/lessons" and method == "GET":
                    return self._json(find_lessons(app.home))
                if path == "/api/checks" and method == "GET":
                    return self._json(app.records.checks())
                if path == "/api/checks" and method == "PUT":
                    b = self._body()
                    app.records.set_check(str(b["item_id"]), bool(b.get("checked")), b.get("lesson"))
                    return self._json(app.records.checks())
                if path == "/api/sessions" and method == "GET":
                    return self._json([asdict(s) for s in app.records.sessions()])
                if path == "/api/sessions" and method == "POST":
                    b = self._body()
                    s = app.records.add_session(day=b.get("day"), minutes=int(b.get("minutes") or 0),
                                                figures=[str(x) for x in b.get("figures", [])],
                                                note=str(b.get("note", "")), lesson=b.get("lesson"))
                    return self._json(asdict(s), 201)
                if (m := re.fullmatch(r"/api/sessions/(\d+)", path)) and method == "DELETE":
                    ok = app.records.delete_session(int(m.group(1)))
                    return self._json({"deleted": ok}, 200 if ok else 404)
                if path == "/api/export" and method == "GET":
                    return self._json(app.records.export())
                if path == "/api/jobs" and method == "GET":
                    return self._json(app.jobs.list())
                if path == "/api/jobs" and method == "POST":
                    b = self._body()
                    job = app.jobs.start(str(b.get("source", "")), str(b.get("sequence", "")),
                                         str(b.get("role", "all")), str(b.get("view", "side")),
                                         str(b.get("kind", "analyze")))
                    return self._json({"id": job.id}, 201)
                return self._error(404, "없는 주소입니다")
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                return self._error(400, str(e))

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

        def do_DELETE(self):
            self._route("DELETE")

    return Handler


def make_server(home: Path, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(App(home)))
