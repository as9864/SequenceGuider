import json
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from sequenceguider.analyze import analyze
from sequenceguider.figures import FigureLibrary, parse_sequence
from sequenceguider.records import Records
from sequenceguider.report import write_report
from sequenceguider.web import Jobs, find_lessons, make_server
from tests.synthetic import walk
from tests.test_pipeline import _write_video

LIB = FigureLibrary.load()


class TestRecords:
    def test_checks_roundtrip(self, tmp_path):
        r = Records(tmp_path / "r.db")
        r.set_check("ocho_atras.drill.0", True, "lesson1")
        r.set_check("ocho_atras.drill.0", True)  # ticking twice keeps the first time
        assert list(r.checks()) == ["ocho_atras.drill.0"]
        r.set_check("ocho_atras.drill.0", False)
        assert r.checks() == {}

    def test_sessions(self, tmp_path):
        r = Records(tmp_path / "r.db")
        a = r.add_session(day="2026-09-28", minutes=45, figures=["giro"], note=" 히로 크기 ")
        b = r.add_session(day="2026-09-30", minutes=20, figures=["ocho_atras", "giro"], lesson="l1")
        assert a.note == "히로 크기"
        assert [s.id for s in r.sessions()] == [b.id, a.id]  # newest day first
        assert r.delete_session(a.id) and not r.delete_session(a.id)
        with pytest.raises(ValueError):
            r.add_session(day="2026-13-01", minutes=10, figures=[])
        with pytest.raises(ValueError):
            r.add_session(day=None, minutes=0, figures=[])


@pytest.fixture
def home(tmp_path):
    out = tmp_path / "lesson1_sequenceguider"
    out.mkdir()
    tr = walk(6)
    video = out / "practice.mp4"
    _write_video(video, tr)
    write_report(analyze(tr, parse_sequence("살리다, 오초 아뜨라스", LIB)), video, out,
                 video_src="practice.mp4", title="오초 수업")
    return tmp_path


@pytest.fixture
def server(home):
    srv = make_server(home, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def call(base, path, method="GET", body=None, headers=None):
    req = urllib.request.Request(base + path, method=method, headers=headers or {},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, res.headers, res.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


class TestWeb:
    def test_find_lessons(self, home):
        (lesson,) = find_lessons(home)
        assert lesson["id"] == "lesson1_sequenceguider" and lesson["title"] == "오초 수업"
        assert [f["figure"] for f in lesson["figures"]] == ["salida_cruzada", "ocho_atras"]
        assert lesson["figures"][1]["checklist"][0]["id"].startswith("ocho_atras.")

    def test_app_and_lesson_files(self, server):
        status, _, body = call(server, "/")
        assert status == 200 and b"SequenceGuider" in body
        status, _, body = call(server, "/lessons/lesson1_sequenceguider/index.html")
        assert status == 200 and "window.parent.SG".encode() in body

    def test_video_byte_ranges(self, server):
        status, headers, body = call(server, "/lessons/lesson1_sequenceguider/practice.mp4",
                                     headers={"Range": "bytes=10-19"})
        assert status == 206 and len(body) == 10 and headers["Content-Range"].startswith("bytes 10-19/")

    @pytest.mark.parametrize("path", ["/lessons/lesson1_sequenceguider/../records.db",
                                      "/lessons/..%2Frecords.db", "/lessons/nope/index.html"])
    def test_no_files_outside_lessons(self, server, path):
        assert call(server, path)[0] == 404

    def test_records_api(self, server):
        _, _, body = call(server, "/api/checks", "PUT", {"item_id": "ocho_atras.drill.0", "checked": True})
        assert "ocho_atras.drill.0" in json.loads(body)
        status, _, body = call(server, "/api/sessions", "POST",
                               {"lesson": "lesson1_sequenceguider", "minutes": 30, "figures": ["ocho_atras"]})
        assert status == 201
        sid = json.loads(body)["id"]
        assert [s["id"] for s in json.loads(call(server, "/api/sessions")[2])] == [sid]
        assert call(server, "/api/sessions", "POST", {"minutes": 0})[0] == 400
        assert call(server, f"/api/sessions/{sid}", "DELETE")[0] == 200
        assert json.loads(call(server, "/api/export")[2])["sessions"] == []

    def test_job_rejects_missing_file(self, server):
        status, _, body = call(server, "/api/jobs", "POST", {"source": "/no/such.mp4", "sequence": "오초"})
        assert status == 400 and "파일이 없습니다" in json.loads(body)["error"]


def test_job_runs_analyze_and_finds_its_lesson(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"")
    jobs = Jobs(tmp_path)
    # stand-in for `sequenceguider analyze`: prints a progress line and the report path the real one ends with
    jobs.command = [sys.executable, "-c",
                    "import sys; print('포즈 1%\\r포즈 100%'); print('리포트: out_sequenceguider/index.html'); "
                    "sys.exit(0)"]
    job = jobs.start(str(video), "오초")
    for _ in range(100):
        (d,) = jobs.list()
        if d["status"] != "running":
            break
        time.sleep(0.05)
    assert d["status"] == "done" and d["lesson"] == "out_sequenceguider"
    assert "포즈 100%" in d["log"] and "포즈 1%" not in d["log"]
    assert job.id == d["id"]
