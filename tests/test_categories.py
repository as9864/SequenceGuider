import json
import sys
import time

from sequenceguider import categories as C
from sequenceguider.web import App, find_lessons
from tests.test_web import call, home, server  # noqa: F401  (fixtures)


def test_ids_and_clean():
    ids = [c.id for c in C.CATEGORIES]
    assert len(ids) == len(set(ids))
    assert {"caminata", "abrazo", "energia", "sacada", "boleo"} <= set(ids)  # the ones asked for first
    assert C.clean(["boleo", "nope", "caminata", "boleo"]) == ["caminata", "boleo"]  # list order, once each


def test_suggest():
    assert C.suggest(figures=["ocho_atras", "salida_cruzada"]) == ["salida", "cruzada", "ocho"]
    assert C.suggest(texts=["볼레오에서 축을 지키기", "아브라소의 에너지"]) == ["abrazo", "energia", "eje", "boleo"]
    assert C.suggest(texts=["Sacada y BOLEO"]) == ["sacada", "boleo"]  # case and language don't matter
    assert C.suggest(texts=["Cambió de energía"]) == ["energia"]  # accents don't either
    assert C.suggest(texts=["turnover", "the hooked rug"]) == []  # English words only as whole words
    assert C.suggest(chosen=["gancho", "made_up"]) == ["gancho"]
    # the raw transcript counts only what keeps coming up
    assert C.suggest(transcript="gancho " * (C.TRANSCRIPT_HITS - 1)) == []
    assert C.suggest(transcript="gancho " * C.TRANSCRIPT_HITS) == ["gancho"]


def test_suggest_for_notes_folder(tmp_path):
    (tmp_path / "notes.json").write_text(json.dumps({
        "summary": "걷기와 안기를 다시 잡았다", "categories": ["energia"],
        "sections": [{"title": "연습", "summary": "", "figures": ["giro"]}],
        "key_points": [{"point": "사까다는 축 이동", "detail": ""}],
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "transcript.json").write_text(json.dumps({"segments": [{"text": "boleo"}] * C.TRANSCRIPT_HITS}),
                                              encoding="utf-8")
    assert C.suggest_for_folder(tmp_path) == ["caminata", "abrazo", "energia", "giro", "sacada", "boleo"]


class TestLessonCategories:
    def test_old_lessons_get_suggestions(self, home):
        (lesson,) = find_lessons(home)  # analysed with "살리다, 오초 아뜨라스" before categories existed
        assert lesson["categories"] == ["salida", "cruzada", "ocho"] and lesson["categories_auto"]
        assert lesson["suggested_categories"] == lesson["categories"]
        assert not (home / "lesson1_sequenceguider" / "lesson.json").exists()

    def test_edit_over_api(self, home, server):
        status, _, body = call(server, "/api/categories")
        assert status == 200 and json.loads(body)[0] == {"id": "caminata", "name_ko": "걷기", "group": "기본기"}
        status, _, body = call(server, "/api/lessons/lesson1_sequenceguider/categories", "PUT",
                               {"categories": ["boleo", "caminata", "unknown"]})
        assert status == 200 and json.loads(body) == {"categories": ["caminata", "boleo"]}
        (lesson,) = json.loads(call(server, "/api/lessons")[2])
        assert lesson["categories"] == ["caminata", "boleo"] and not lesson["categories_auto"]
        assert lesson["suggested_categories"] == ["salida", "cruzada", "ocho"]  # still offered in the editor
        assert lesson["media"][0]["has_guide"]  # saving them kept the lesson's analysis
        # an empty list is the user's choice too, not "suggest again"
        call(server, "/api/lessons/lesson1_sequenceguider/categories", "PUT", {"categories": []})
        assert find_lessons(home)[0]["categories"] == []
        assert call(server, "/api/lessons/lesson1_sequenceguider/categories", "PUT", {"categories": "boleo"})[0] == 400
        assert call(server, "/api/lessons/nope/categories", "PUT", {"categories": []})[0] == 400

    def test_new_lesson_with_categories(self, home, server):
        video = home / "class.mp4"
        video.write_bytes(b"x")
        status, _, body = call(server, "/api/lessons", "POST", {"source": str(video), "categories": ["sacada"]})
        lesson = next(l for l in find_lessons(home) if l["id"] == json.loads(body)["id"])
        assert lesson["categories"] == ["sacada"] and not lesson["categories_auto"]

    def test_picked_when_starting_an_analysis(self, home):
        app = App(home)
        clip = home / "clip.mp4"
        clip.write_bytes(b"")
        # stand-in for `analyze` that ends like the real one, into the existing lesson
        app.jobs.command = [sys.executable, "-c", "print('리포트: lesson1_sequenceguider/index.html')"]
        app.jobs.start(str(clip), "오초", categories_=["boleo", "made_up"])
        for _ in range(100):
            lessons = app.lessons()
            if app.jobs.list()[0]["status"] != "running":
                break
            time.sleep(0.05)
        lessons = app.lessons()
        # the suggestions it had, plus what was picked
        assert lessons[0]["categories"] == ["salida", "cruzada", "ocho", "boleo"]
