"""`sequenceguider serve`: a local web app to learn from analysed videos and keep practice records.

Everything lives in one home folder:
    <home>/<name>_sequenceguider/   one analysed video each (what `analyze` writes)
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


def find_lessons(home: Path) -> list[dict]:
    """Every analysed video directly under `home`, newest first."""
    lessons = []
    for result in home.glob("*/result.json"):
        folder = result.parent
        if not (folder / "index.html").is_file():
            continue
        try:
            data = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        figures = []
        for f in data.get("figures", []):
            lesson = f.get("lesson") or {}
            figures.append({
                "order": f.get("order"), "figure": f.get("figure"), "name_ko": f.get("name_ko"),
                "count": f.get("count", 1), "start_s": f.get("start_s"), "end_s": f.get("end_s"),
                "key_point": lesson.get("key_point", ""),
                "checklist": lesson.get("checklist", []),
            })
        lessons.append({
            "id": folder.name,
            "title": data.get("title") or folder.name.removesuffix("_sequenceguider"),
            "source_url": data.get("source_url"),
            "role": data.get("role", "all"),
            "figures": figures,
            "updated_at": datetime.fromtimestamp(result.stat().st_mtime).isoformat(timespec="seconds"),
        })
    return sorted(lessons, key=lambda x: x["updated_at"], reverse=True)


@dataclass
class Job:
    id: str
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

    def start(self, source: str, sequence: str = "", role: str = "all", view: str = "side") -> Job:
        source = source.strip()
        if not source:
            raise ValueError("유튜브 링크나 영상 파일 경로를 넣어 주세요")
        if role not in ("all", "leader", "follower") or view not in ("side", "front"):
            raise ValueError("역할이나 촬영 방향 값이 올바르지 않습니다")
        if not re.match(r"^https?://", source, re.IGNORECASE):
            path = Path(source).expanduser()
            if not path.is_file():
                raise ValueError(f"파일이 없습니다: {source}")
            if not sequence.strip():
                raise ValueError("파일 영상은 시퀀스 순서를 적어 주세요")
            source = str(path.resolve())
        cmd = [*self.command, "analyze", source, "--role", role, "--view", view]
        if sequence.strip():
            cmd += ["--sequence", sequence.strip()]
        self.dir.mkdir(parents=True, exist_ok=True)
        job = Job(id=uuid.uuid4().hex[:8], source=source, sequence=sequence.strip(), role=role, view=view,
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
            if m := re.search(r"리포트: (.+)", raw):
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
                                         str(b.get("role", "all")), str(b.get("view", "side")))
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
