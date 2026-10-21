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


class TestMedia:
    """A lesson holds several videos/recordings; each one is analysed only if asked."""

    def test_old_lesson_is_a_one_item_list(self, home):
        (lesson,) = find_lessons(home)
        (item,) = lesson["media"]
        assert item["id"] == "main" and item["kind"] == "video" and item["name"] == "practice.mp4"
        assert item["has_guide"] and item["analyze"] and item["dir"] == ""
        assert not (home / "lesson1_sequenceguider" / "lesson.json").exists()  # reading doesn't rewrite it

    def test_add_without_analysis(self, home, server):
        demo = home / "demo.webm"
        demo.write_bytes(b"0123456789" * 10)
        status, _, body = call(server, "/api/lessons/lesson1_sequenceguider/media", "POST",
                               {"source": str(demo), "label": "최종 시연"})
        assert status == 201
        added = json.loads(body)
        assert added["analyze"] is False and added["dir"] == f"media/{added['id']}"
        (lesson,) = find_lessons(home)
        main, item = lesson["media"]
        assert main["id"] == "main" and main["has_guide"]  # the old lesson kept its analysis
        assert item["label"] == "최종 시연" and not item["has_guide"] and item["notes"] is None
        assert [f["figure"] for f in lesson["figures"]] == ["salida_cruzada", "ocho_atras"]
        assert call(server, "/api/jobs")[2] == b"[]"  # nothing started
        status, headers, body = call(server, f"/media/lesson1_sequenceguider/{item['id']}",
                                     headers={"Range": "bytes=0-4"})
        assert status == 206 and body == b"01234"
        assert call(server, "/media/lesson1_sequenceguider/main")[0] == 200  # the copied practice.mp4

    def test_add_with_analysis_writes_into_its_own_folder(self, home):
        from sequenceguider.web import App

        app = App(home)
        audio = home / "lesson.m4a"
        audio.write_bytes(b"")
        app.jobs.command = [sys.executable, "-c", "import sys; print(sys.argv[1:])"]
        item = app.add_media("lesson1_sequenceguider", str(audio), "2부 녹음", {"kind": "notes", "role": "follower"})
        for _ in range(100):
            (job,) = app.jobs.list()
            if job["status"] != "running":
                break
            time.sleep(0.05)
        assert job["lesson"] == "lesson1_sequenceguider" and job["media"] == item["id"]
        out = home / "lesson1_sequenceguider" / "media" / item["id"]
        assert "'notes'" in job["log"] and str(out) in job["log"] and "'follower'" in job["log"]
        # what `notes` would write there shows up under that item only
        out.mkdir(parents=True)
        (out / "notes.json").write_text(json.dumps({"summary": "볼레오", "video": str(audio),
                                                    "practice": [{"id": "notes:x:0", "text": "축"}]}), encoding="utf-8")
        (lesson,) = find_lessons(home)
        main, rec = lesson["media"]
        assert main["notes"] is None and rec["kind"] == "audio" and rec["notes"]["summary"] == "볼레오"
        assert lesson["notes"]["checklist"] == [{"id": "notes:x:0", "text": "축"}]

    def test_analyze_later_and_bad_requests(self, home):
        from sequenceguider.web import App

        app = App(home)
        app.jobs.command = [sys.executable, "-c", "pass"]
        clip = home / "clip.mp4"
        clip.write_bytes(b"")
        item = app.add_media("lesson1_sequenceguider", str(clip))
        with pytest.raises(ValueError, match="시퀀스"):
            app.analyze_media("lesson1_sequenceguider", item["id"], {"kind": "analyze"})
        assert not app.jobs.list()
        app.analyze_media("lesson1_sequenceguider", item["id"], {"kind": "analyze", "sequence": "오초"})
        (job,) = app.jobs.list()
        assert job["media"] == item["id"] and job["source"] == str(clip.resolve())
        assert find_lessons(home)[0]["media"][1]["analyze"]
        with pytest.raises(ValueError, match="오디오 파일이 아닙니다"):
            app.add_media("lesson1_sequenceguider", str(home / "lesson1_sequenceguider" / "result.json"))
        with pytest.raises(ValueError, match="영상에만"):
            (home / "a.mp3").write_bytes(b"")
            app.add_media("lesson1_sequenceguider", str(home / "a.mp3"), analyze={"kind": "analyze", "sequence": "오초"})
        assert len(find_lessons(home)[0]["media"]) == 2  # the rejected ones weren't added
        with pytest.raises(KeyError):
            app.add_media("../x", str(clip))

    def test_remove_keeps_at_least_one(self, home):
        from sequenceguider.web import App

        app = App(home)
        clip = home / "clip.mp4"
        clip.write_bytes(b"")
        item = app.add_media("lesson1_sequenceguider", str(clip))
        app.remove_media("lesson1_sequenceguider", item["id"])
        assert [m["id"] for m in find_lessons(home)[0]["media"]] == ["main"]
        with pytest.raises(ValueError):
            app.remove_media("lesson1_sequenceguider", "main")

    def test_new_lesson_without_analysis(self, home, server):
        video = home / "0920 시연.mp4"
        video.write_bytes(b"x")
        status, _, body = call(server, "/api/lessons", "POST", {"source": str(video)})
        assert status == 201 and json.loads(body)["id"] == "0920_시연_sequenceguider"
        lesson = next(l for l in find_lessons(home) if l["id"] == "0920_시연_sequenceguider")
        assert lesson["title"] == "0920 시연" and lesson["media"][0]["analyze"] is False
        status, _, body = call(server, "/api/lessons", "POST", {"source": str(video)})
        assert json.loads(body)["id"] == "0920_시연_2_sequenceguider"
