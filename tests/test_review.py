import json
import threading

import pytest

from sequenceguider import review
from sequenceguider.records import Records
from sequenceguider.web import App, make_server
from tests.test_web import call

NOTES = {
    "title": "히로 개인레슨", "summary": "히로와 오초", "duration_s": 600, "video": None,
    "figure_names": {"giro": "히로", "ocho_atras": "오초 아뜨라스"},
    "sections": [{"start": 0, "end": 200, "title": "오초 설명", "figures": ["ocho_atras"]},
                 {"start": 200, "end": 600, "title": "히로", "figures": ["giro"]}],
    "key_points": [
        {"t": 30, "point": "골반을 먼저 돌린다", "detail": "상체는 파트너 쪽", "kind": "technique", "role": "follower", "quote": ""},
        {"t": 250, "point": "히로는 원의 중심을 지킨다", "detail": "", "kind": "principle", "role": "all", "quote": "center"},
        {"t": 590, "point": "끝에서 콜렉트", "detail": "", "kind": "correction", "role": "all", "quote": ""},
    ],
    "practice": [],
}


@pytest.fixture
def home(tmp_path):
    folder = tmp_path / "giro_sequenceguider"
    folder.mkdir()
    (folder / "notes.json").write_text(json.dumps(NOTES, ensure_ascii=False), encoding="utf-8")
    (folder / "lesson.json").write_text(json.dumps({"title": "히로 개인레슨", "media": [
        {"id": "main", "label": "", "src": "https://youtu.be/abcdefghijk", "dir": "", "analyze": True,
         "added_at": "2026-11-20T10:00:00"}]}, ensure_ascii=False), encoding="utf-8")
    return tmp_path


class TestSchedule:
    def test_intervals(self):
        assert review.next_state(None, True, "2026-11-01") == (1, "2026-11-04")   # new, remembered: 3 days
        assert review.next_state(1, True, "2026-11-04") == (2, "2026-11-11")      # then 7
        assert review.next_state(2, True, "2026-11-11") == (3, "2026-12-02")      # then 21
        assert review.next_state(3, True, "2026-12-02") == (4, "2027-01-31")      # then every 60
        assert review.next_state(4, True, "2027-01-31") == (4, "2027-04-01")
        assert review.next_state(3, False, "2026-12-02") == (0, "2026-12-03")     # forgot: tomorrow

    def test_streak(self):
        days = {"2026-11-25", "2026-11-26", "2026-11-27"}
        assert review.streak(days, "2026-11-27") == 3
        assert review.streak(days, "2026-11-28") == 3  # not yet today
        assert review.streak(days, "2026-11-29") == 0


class TestCards:
    def test_cards_from_notes(self, home):
        app = App(home)
        cards = app.review_cards()
        assert [c["point"] for c in cards] == [p["point"] for p in NOTES["key_points"]]
        first, second, last = cards
        assert first["figures"] == ["ocho_atras"] and first["section"] == "오초 설명"
        assert first["start"] == 27 and first["end"] == 60  # a 30 s clip from 3 s before
        assert second["figure_names"] == {"giro": "히로"}
        assert last["end"] == 600  # never past the end of the lesson
        assert first["learned"] == "2026-11-20"
        assert len({c["id"] for c in cards}) == 3

    def test_new_cards_are_let_in_a_few_a_day(self, home):
        app = App(home)
        plan = review.plan(app.review_cards(), {}, [], [], "2026-11-28", new_per_day=2)
        assert plan["new"] == 2 and plan["waiting_new"] == 1
        # a lesson learned today is first reviewed tomorrow
        assert review.plan(app.review_cards(), {}, [], [], "2026-11-20")["queue"] == []

    def test_answer_moves_the_card(self, home):
        app = App(home)
        day = "2026-11-28"
        card = app.review(day)["queue"][0]
        assert app.answer_review(card["id"], True, day) == {"id": card["id"], "step": 1, "due": "2026-12-01"}
        plan = app.review(day)
        assert card["id"] not in [c["id"] for c in plan["queue"]] and plan["done_today"] == 1 and plan["streak"] == 1
        assert app.review("2026-12-01")["queue"][0]["id"] == card["id"]  # due again in 3 days
        app.answer_review(card["id"], False, "2026-12-01")
        assert app.records.reviews()[card["id"]]["lapses"] == 1
        with pytest.raises(KeyError):
            app.answer_review("kp:nope", True, day)

    def test_today_stays_the_same_all_day(self, home):
        app = App(home)
        day = "2026-11-28"
        t = app.review(day)["today"]
        assert t["start"] is not None and not t["reviewed_today"]
        app.answer_review(t["id"], True, day)
        again = app.review(day)["today"]
        assert again["id"] == t["id"] and again["reviewed_today"]


class TestPractica:
    def test_crud_and_validation(self, tmp_path):
        r = Records(tmp_path / "r.db")
        p = r.add_practica(day="2026-11-27", place="라 비루타", minutes=120, partners="민지, 지수",
                           items=[{"label": "히로", "figure": "giro", "feel": "confusing", "note": "축이 무너짐"},
                                  {"label": "  ", "figure": ""},  # dropped: empty
                                  {"label": "까미나따", "feel": "weird"}],  # unknown feel -> ok
                           note="음악이 빨랐다", next_time="히로 크기 줄이기")
        assert [i["label"] for i in p.items] == ["히로", "까미나따"] and p.items[1]["feel"] == "ok"
        q = r.update_practica(p.id, day="2026-11-27", items=[{"label": "히로", "figure": "giro", "feel": "good"}])
        assert q.place == "" and q.items[0]["feel"] == "good"
        with pytest.raises(ValueError):
            r.add_practica(day="2026-11-27")  # nothing written
        with pytest.raises(ValueError):
            r.add_practica(day="2026-11-27", minutes=0, note="x")
        with pytest.raises(KeyError):
            r.update_practica(999, note="x")
        assert r.delete_practica(p.id) and r.practicas() == []

    def test_confusion_until_it_goes_well(self):
        p1 = {"id": 1, "day": "2026-11-20", "items": [{"label": "히로", "figure": "giro", "feel": "confusing", "note": "축"},
                                                       {"label": "아브라소가 셈", "figure": None, "feel": "confusing", "note": ""}]}
        p2 = {"id": 2, "day": "2026-11-27", "items": [{"label": "히로", "figure": "giro", "feel": "good", "note": ""}]}
        assert [c["label"] for c in review.open_confusions([p1])] == ["히로", "아브라소가 셈"]
        assert [c["label"] for c in review.open_confusions([p2, p1])] == ["아브라소가 셈"]

    def test_confusion_pushes_related_cards_first(self, home):
        app = App(home)
        app.records.add_practica(day="2026-11-27", items=[
            {"label": "히로", "figure": "giro", "feel": "confusing", "note": "축이 무너짐"}])
        plan = app.review("2026-11-28")
        kinds = [(c["kind"], c["figures"]) for c in plan["queue"]]
        assert kinds[0] == ("practica", ["giro"])  # the confusion itself is a card
        assert kinds[1] == ("point", ["giro"])     # then the lesson's 히로 key point
        assert plan["today"]["figures"] == ["giro"] and plan["today"]["kind"] == "point"  # today's clip is about it
        assert plan["confusions"][0]["note"] == "축이 무너짐"


def test_api(home):
    srv = make_server(home, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        status, _, body = call(base, "/api/practicas", "POST", {"day": "2026-11-27", "place": "쁘락", "items": [
            {"label": "히로", "figure": "giro", "feel": "confusing"}], "next_time": "히로 작게"})
        assert status == 201
        pid = json.loads(body)["id"]
        assert call(base, "/api/practicas", "POST", {"day": "x", "note": "a"})[0] == 400
        plan = json.loads(call(base, "/api/review?day=2026-11-28")[2])
        assert plan["queue"][0]["kind"] == "practica"
        status, _, body = call(base, "/api/review", "POST", {"card_id": plan["queue"][0]["id"], "remembered": True, "day": "2026-11-28"})
        assert status == 200 and json.loads(body)["step"] == 1
        assert call(base, "/api/practicas/" + str(pid), "PUT", {"day": "2026-11-27", "items": [
            {"label": "히로", "figure": "giro", "feel": "good"}]})[0] == 200
        plan = json.loads(call(base, "/api/review?day=2026-11-28")[2])
        assert plan["confusions"] == []
        figs = json.loads(call(base, "/api/figures")[2])
        assert any(f["figure"] == "giro" for f in figs)
        export = json.loads(call(base, "/api/export")[2])
        assert export["practicas"] and export["review_log"]
        assert call(base, "/api/practicas/" + str(pid), "DELETE")[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()
