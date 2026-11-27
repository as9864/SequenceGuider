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
        r.set_check(1, "ocho_atras.drill.0", True, "lesson1")
        r.set_check(1, "ocho_atras.drill.0", True)  # ticking twice keeps the first time
        assert list(r.checks(1)) == ["ocho_atras.drill.0"]
        assert r.checks(2) == {}  # each user's own
        r.set_check(1, "ocho_atras.drill.0", False)
        assert r.checks(1) == {}

    def test_sessions(self, tmp_path):
        r = Records(tmp_path / "r.db")
        a = r.add_session(1, day="2026-09-28", minutes=45, figures=["giro"], note=" 히로 크기 ")
        b = r.add_session(1, day="2026-09-30", minutes=20, figures=["ocho_atras", "giro"], lesson="l1")
        assert a.note == "히로 크기"
        assert [s.id for s in r.sessions(1)] == [b.id, a.id]  # newest day first
        assert r.sessions(2) == [] and not r.delete_session(2, a.id)  # not someone else's
        assert r.delete_session(1, a.id) and not r.delete_session(1, a.id)
        with pytest.raises(ValueError):
            r.add_session(1, day="2026-13-01", minutes=10, figures=[])
        with pytest.raises(ValueError):
            r.add_session(1, day=None, minutes=0, figures=[])

    def test_records_from_before_accounts(self, tmp_path):
        """An old records.db (no users) keeps its log: the first account gets it."""
        import sqlite3
        db = sqlite3.connect(tmp_path / "records.db")
        db.executescript("""
            CREATE TABLE checks (item_id TEXT PRIMARY KEY, lesson TEXT, checked_at TEXT NOT NULL);
            CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, lesson TEXT, day TEXT NOT NULL,
                minutes INTEGER NOT NULL, figures TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL);
            INSERT INTO checks VALUES ('giro.drill.0', NULL, '2026-10-01T10:00:00');
            INSERT INTO sessions (day, minutes, figures, created_at) VALUES ('2026-10-01', 30, '[]', 'x');
        """)
        db.close()
        from sequenceguider.web import App
        app = App(tmp_path)
        owner = app.sign_up("owner", "password123")
        assert owner.is_admin and list(app.records.checks(owner.id)) == ["giro.drill.0"]
        assert [s.minutes for s in app.records.sessions(owner.id)] == [30]
        second = app.sign_up("second", "password123")
        assert not second.is_admin and app.records.checks(second.id) == {}


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


_COOKIES: dict[str, str] = {}  # base url -> the admin's login cookie, sent by `call`


def login_cookie(headers) -> str:
    return headers["Set-Cookie"].split(";")[0]


def log_in_admin(base: str) -> None:
    """Make the server's first account; `call` then sends its cookie."""
    status, headers, _ = call(base, "/api/auth/signup", "POST", {"username": "admin", "password": "password123"},
                              auth=False)
    assert status == 201
    _COOKIES[base] = login_cookie(headers)


@pytest.fixture
def server(home):
    srv = make_server(home, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    log_in_admin(base)
    yield base
    srv.shutdown()
    srv.server_close()


def call(base, path, method="GET", body=None, headers=None, auth=True):
    headers = dict(headers or {})
    if auth and base in _COOKIES:
        headers.setdefault("Cookie", _COOKIES[base])
    req = urllib.request.Request(base + path, method=method, headers=headers,
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


class TestLessonMedia:
    """One lesson, several videos/recordings: e.g. the class, then the final demo (not analysed)."""

    def test_add_without_analysis_and_serve(self, server, home):
        demo = home / "final_demo.mp4"
        demo.write_bytes(b"0123456789" * 10)
        status, _, body = call(server, "/api/lessons/lesson1_sequenceguider/media", "POST",
                               {"source": str(demo), "title": "최종 시연", "analysis": "none"})
        assert status == 201
        item = json.loads(body)
        assert item["title"] == "최종 시연" and item["media"] == "video" and item["analysis"] == "none"
        (lesson,) = json.loads(call(server, "/api/lessons")[2])
        assert [(m["id"], m["has_notes"], m["has_guide"]) for m in lesson["media"]] == [(item["id"], False, False)]
        assert lesson["has_guide"]  # the lesson's own analysis is untouched
        assert json.loads(call(server, "/api/jobs")[2]) == []  # nothing was analysed
        status, headers, body = call(server, f"/lessons/lesson1_sequenceguider/media-file/{item['id']}",
                                     headers={"Range": "bytes=0-9"})
        assert status == 206 and body == b"0123456789"
        assert call(server, "/api/lessons/lesson1_sequenceguider/media/nope", "DELETE")[0] == 404
        assert call(server, f"/api/lessons/lesson1_sequenceguider/media/{item['id']}", "DELETE")[0] == 200
        assert json.loads(call(server, "/api/lessons")[2])[0]["media"] == []
        assert demo.is_file()  # taken off the lesson, not deleted

    def test_add_rejects_bad_input(self, server, home):
        (home / "notes.txt").write_text("x")
        for body, msg in [({"source": "/no/such.mp4"}, "파일이 없습니다"),
                          ({"source": str(home / "notes.txt")}, "영상·녹음 파일이 아닙니다")]:
            status, _, out = call(server, "/api/lessons/lesson1_sequenceguider/media", "POST", body)
            assert status == 400 and msg in json.loads(out)["error"]
        rec = home / "talk.m4a"
        rec.write_bytes(b"")
        status, _, out = call(server, "/api/lessons/lesson1_sequenceguider/media", "POST",
                              {"source": str(rec), "analysis": "analyze", "sequence": "오초"})
        assert status == 400 and "동작 분석" in json.loads(out)["error"]
        assert call(server, "/api/lessons/nope_sequenceguider/media", "POST", {"source": str(rec)})[0] == 400
        assert json.loads(call(server, "/api/lessons")[2])[0]["media"] == []  # nothing half-added

    def test_media_only_lesson(self, server, home):
        rec = home / "audio" / "lesson2.m4a"
        rec.parent.mkdir()
        rec.write_bytes(b"")
        status, _, body = call(server, "/api/jobs", "POST", {"kind": "none", "source": str(rec), "title": "2부"})
        assert status == 201 and json.loads(body)["lesson"] == "lesson2_sequenceguider"
        lesson = next(l for l in json.loads(call(server, "/api/lessons")[2]) if l["id"] == "lesson2_sequenceguider")
        assert lesson["title"] == "2부" and not lesson["has_guide"] and lesson["notes"] is None
        assert [m["media"] for m in lesson["media"]] == ["audio"]


def test_media_analysis_goes_to_its_own_folder(tmp_path):
    from sequenceguider.web import App

    (tmp_path / "l_sequenceguider").mkdir()
    rec = tmp_path / "talk.m4a"
    rec.write_bytes(b"")
    app = App(tmp_path)
    app.jobs.command = [sys.executable, "-c", "import sys; print(' '.join(sys.argv[1:])); print('노트: x/notes.json')"]
    item = app.add_media("l_sequenceguider", str(rec), "", "none")
    job = app.analyze_media("l_sequenceguider", item["id"], "notes", speakers="A=남/en")
    for _ in range(100):
        (d,) = app.jobs.list()
        if d["status"] != "running":
            break
        time.sleep(0.05)
    out = tmp_path.resolve() / "l_sequenceguider" / "media" / item["id"]
    assert d["status"] == "done" and d["lesson"] == "l_sequenceguider" and d["media"] == item["id"] == job.media
    assert str(out) in d["log"] and "--speakers" in d["log"]
    (m,) = find_lessons(tmp_path)[0]["media"]
    assert m["analysis"] == "notes" and not m["has_notes"]
    out.mkdir(parents=True)
    (out / "notes.json").write_text(json.dumps({"duration_s": 60, "practice": [{"id": "p1", "text": "축"}]}), "utf-8")
    (m,) = find_lessons(tmp_path)[0]["media"]
    assert m["has_notes"] and m["checklist"] == [{"id": "p1", "text": "축"}]


class TestAccounts:
    def test_passwords_are_hashed(self):
        from sequenceguider.accounts import hash_password, verify_password
        h = hash_password("비밀번호1234")
        assert "비밀번호" not in h and verify_password("비밀번호1234", h) and not verify_password("x", h)
        assert hash_password("same") != hash_password("same")  # salted

    def test_sign_up_rules(self, tmp_path):
        from sequenceguider.accounts import Accounts
        acc = Accounts(tmp_path / "r.db", signup_open=False)
        assert acc.can_sign_up()  # the first account, even with sign-up closed
        assert acc.sign_up("Han", "password123").is_admin
        assert not acc.can_sign_up()
        with pytest.raises(PermissionError):
            acc.sign_up("other", "password123")
        acc.signup_open = True
        for name, pw, msg in [("han", "password123", "이미 있는"), ("a", "password123", "아이디는"),
                              ("ok_name", "short", "8자")]:
            with pytest.raises(ValueError, match=msg):
                acc.sign_up(name, pw)
        assert not acc.sign_up("second", "password123").is_admin

    def test_login_and_logout(self, tmp_path):
        from sequenceguider.accounts import Accounts
        acc = Accounts(tmp_path / "r.db")
        acc.sign_up("han", "password123", "한승엽")
        with pytest.raises(ValueError):
            acc.log_in("han", "wrong")
        user, token = acc.log_in("HAN", "password123")  # names ignore case
        assert user.display_name == "한승엽" and acc.user_for(token).id == user.id
        assert acc.user_for("made-up") is None
        acc.log_out(token)
        assert acc.user_for(token) is None
        _, token = acc.log_in("han", "password123")
        acc.set_password("han", "newpassword1")  # logs out everywhere
        assert acc.user_for(token) is None

    def test_too_many_wrong_passwords(self, tmp_path):
        from sequenceguider.accounts import Accounts
        acc = Accounts(tmp_path / "r.db")
        acc.sign_up("han", "password123")
        for _ in range(10):
            with pytest.raises(ValueError):
                acc.log_in("han", "wrong", "1.2.3.4")
        with pytest.raises(PermissionError):
            acc.log_in("han", "password123", "1.2.3.4")
        assert acc.log_in("han", "password123", "5.6.7.8")  # another address isn't locked


class TestWebAuth:
    def test_everything_but_the_shell_needs_login(self, server):
        assert call(server, "/", auth=False)[0] == 200
        for path in ["/api/lessons", "/api/checks", "/api/sessions", "/api/jobs",
                     "/lessons/lesson1_sequenceguider/index.html"]:
            assert call(server, path, auth=False)[0] == 401, path
        status, _, body = call(server, "/api/auth/me", auth=False)
        assert status == 401 and json.loads(body)["user"] is None and not json.loads(body)["first_account"]
        assert json.loads(call(server, "/api/auth/me")[2])["user"]["is_admin"]

    def test_members_keep_their_own_records(self, server):
        status, headers, body = call(server, "/api/auth/signup", "POST",
                                     {"username": "member", "password": "password123", "display_name": "회원"},
                                     auth=False)
        assert status == 201 and not json.loads(body)["user"]["is_admin"]
        member = {"Cookie": login_cookie(headers)}
        call(server, "/api/checks", "PUT", {"item_id": "giro.drill.0", "checked": True})  # the admin's tick
        assert call(server, "/api/checks", headers=member)[2] == b"{}"
        status, _, body = call(server, "/api/sessions", "POST", {"minutes": 20, "figures": []}, headers=member)
        sid = json.loads(body)["id"]
        assert json.loads(call(server, "/api/sessions")[2]) == []  # the admin doesn't see it
        assert call(server, f"/api/sessions/{sid}", "DELETE")[0] == 404  # nor delete it
        assert call(server, "/api/lessons", headers=member)[0] == 200  # the library is shared
        # analysing / adding media reads files on the server: admins only
        assert call(server, "/api/jobs", headers=member)[2] == b"[]"
        assert call(server, "/api/jobs", "POST", {"source": "https://youtu.be/x"}, headers=member)[0] == 403
        assert call(server, "/api/lessons/lesson1_sequenceguider/media", "POST", {"source": "C:/x.mp4"},
                    headers=member)[0] == 403

    def test_login_logout_cookie(self, server):
        status, headers, _ = call(server, "/api/auth/login", "POST", {"username": "admin", "password": "nope"},
                                  auth=False)
        assert status == 400 and "Set-Cookie" not in headers
        status, headers, _ = call(server, "/api/auth/login", "POST",
                                  {"username": "admin", "password": "password123"}, auth=False)
        assert status == 200 and "HttpOnly" in headers["Set-Cookie"] and "SameSite=Lax" in headers["Set-Cookie"]
        cookie = {"Cookie": login_cookie(headers)}
        assert call(server, "/api/lessons", headers=cookie)[0] == 200
        call(server, "/api/auth/logout", "POST", headers=cookie)
        assert call(server, "/api/lessons", headers=cookie)[0] == 401

    def test_no_cross_site_writes(self, server):
        body = {"minutes": 20, "figures": []}
        assert call(server, "/api/sessions", "POST", body, headers={"Origin": "https://evil.example"})[0] == 403
        assert call(server, "/api/sessions", "POST", body, headers={"Origin": server})[0] == 201


class TestPublic:
    """`serve --public`: behind a Cloudflare Tunnel, which adds CF-Connecting-IP to every request."""

    @pytest.fixture
    def public(self, home):
        srv = make_server(home, port=0, public=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        log_in_admin(base)
        yield base
        srv.shutdown()
        srv.server_close()

    def test_secure_cookie_and_headers(self, public):
        status, headers, _ = call(public, "/api/auth/login", "POST",
                                  {"username": "admin", "password": "password123"}, auth=False)
        assert status == 200 and "Secure" in headers["Set-Cookie"]
        assert headers["Strict-Transport-Security"] and headers["X-Content-Type-Options"] == "nosniff"

    def test_not_public_has_no_secure_cookie(self, server):
        status, headers, _ = call(server, "/api/auth/login", "POST",
                                  {"username": "admin", "password": "password123"}, auth=False)
        assert "Secure" not in headers["Set-Cookie"] and "Strict-Transport-Security" not in headers

    def test_no_local_paths_from_the_internet(self, public, home):
        tunnel = {"CF-Connecting-IP": "203.0.113.5"}
        video = str(home / "lesson1_sequenceguider" / "notes.json")
        assert call(public, "/api/jobs", "POST", {"source": video, "kind": "none"}, headers=tunnel)[0] == 403
        assert call(public, "/api/lessons/lesson1_sequenceguider/media", "POST", {"source": video},
                    headers=tunnel)[0] == 403

    def test_wrong_passwords_counted_per_visitor(self, public):
        bad = {"username": "admin", "password": "nope"}
        for _ in range(10):
            call(public, "/api/auth/login", "POST", bad, headers={"CF-Connecting-IP": "203.0.113.5"}, auth=False)
        locked = call(public, "/api/auth/login", "POST", bad, headers={"CF-Connecting-IP": "203.0.113.5"}, auth=False)
        assert locked[0] == 403
        other = call(public, "/api/auth/login", "POST", {"username": "admin", "password": "password123"},
                     headers={"CF-Connecting-IP": "198.51.100.7"}, auth=False)
        assert other[0] == 200
