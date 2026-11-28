import json
import zipfile

import pytest

from sequenceguider import markdown, notion
from sequenceguider.web import App, find_lessons
from tests.notion_fixture import write_zip


@pytest.fixture
def export(tmp_path):
    return write_zip(tmp_path / "Export-6f1e2d3c.zip")


def lessons_by_title(home):
    return {l["title"]: l for l in find_lessons(home)}


class TestImport:
    def test_rows_become_lessons(self, tmp_path, export):
        home = tmp_path / "home"
        home.mkdir()
        res = notion.import_export(home, export)
        assert len(res.created) == 7  # the database page itself is not a lesson
        ls = lessons_by_title(home)
        assert set(ls) == {"소똥 개인레슨", "10. 26. 쁘락 오답 체크", "피드백 정리 노트", "커넥션에 대한 개념",
                           "Brenno y fati class", "성공 노트", "깔리토스"}
        assert not (home / ".imports").exists() or not any((home / ".imports").iterdir())

    def test_properties_kinds_categories(self, tmp_path, export):
        home = tmp_path / "home"
        home.mkdir()
        notion.import_export(home, export)
        ls = lessons_by_title(home)
        fb = ls["피드백 정리 노트"]
        assert fb["doc"]["kind"] == "feedback" and fb["doc"]["date"] == "2025-12-13"
        assert fb["doc"]["people"] == ["한승엽", "[이약사의 세계여행] Minalee"]
        assert fb["categories"] == ["caminata", "conexion", "eje"]  # 걷기, 커넥션, 자세교정
        assert fb["doc"]["tags"] == ["디아고날"]  # keywords that are no category stay as tags
        assert ls["10. 26. 쁘락 오답 체크"]["doc"]["kind"] == "practica"
        assert ls["소똥 개인레슨"]["doc"]["kind"] == "lesson"
        assert ls["커넥션에 대한 개념"]["doc"]["kind"] == "note"
        assert ls["성공 노트"]["categories"] == ["caminata", "conexion", "ocho"]  # 오쵸
        assert ls["Brenno y fati class"]["doc"]["date"] == "2025-11-07"  # from the CSV's 날짜 too

    def test_media_from_video_column_and_text(self, tmp_path, export):
        home = tmp_path / "home"
        home.mkdir()
        notion.import_export(home, export)
        ls = lessons_by_title(home)
        assert [m["src"] for m in ls["Brenno y fati class"]["media"]] == ["https://youtu.be/AAAAAAAAAAA"]
        assert [m["kind"] for m in ls["소똥 개인레슨"]["media"]] == ["youtube"]
        assert ls["깔리토스"]["media"] == []

    def test_subpages_images_and_links(self, tmp_path, export):
        home = tmp_path / "home"
        home.mkdir()
        notion.import_export(home, export)
        lid = lessons_by_title(home)["소똥 개인레슨"]["id"]
        doc = notion.render_doc(home / lid, lid)
        assert [(p["title"], p["depth"]) for p in doc["pages"]] == [("소똥 개인레슨", 0), ("히로 연습 메모", 1), ("발 모양", 2)]
        main = doc["pages"][0]["html"]
        img = next(f for f in (home / lid / "notion" / "files").iterdir())
        assert img.read_bytes().startswith(b"\x89PNG")
        assert f'src="/lessons/{lid}/notion/files/{img.name}"' in main
        assert '<a href="#page-1" data-page-link>히로 연습 메모</a>' in main
        assert "<script>" not in main and "&lt;script&gt;" in main
        assert '<div class="callout">' in main and "<table>" in main and 'class="todos"' in main
        fb = lessons_by_title(home)["피드백 정리 노트"]["id"]
        html = notion.render_doc(home / fb, fb)["pages"][0]["html"]
        assert f'href="#/lesson/{lid}/doc"' in html  # a link to another imported page opens that lesson
        assert "<details><summary>걷기 피드백</summary>" in html

    def test_reimport_is_idempotent_and_keeps_user_edits(self, tmp_path, export):
        home = tmp_path / "home"
        home.mkdir()
        notion.import_export(home, export)
        app = App(home)
        lid = lessons_by_title(home)["성공 노트"]["id"]
        app.set_categories(lid, ["giro"])  # edited in the app
        res = notion.import_export(home, export)
        assert len(res.unchanged) == 7 and not res.created and not res.updated
        assert len(find_lessons(home)) == 7
        # a newer export: the text changed, a link was added
        newer = write_zip(tmp_path / "newer.zip", body_extra="\n추가 메모 https://youtu.be/CCCCCCCCCCC\n")
        res = notion.import_export(home, newer)
        assert len(res.updated) == 1 and len(res.unchanged) == 6
        ls = lessons_by_title(home)
        sotong = ls["소똥 개인레슨"]
        assert [m["src"] for m in sotong["media"]][-1] == "https://youtu.be/CCCCCCCCCCC"
        assert len(sotong["media"]) == 2
        assert "추가 메모" in notion.render_doc(home / sotong["id"], sotong["id"])["pages"][0]["html"]
        assert ls["성공 노트"]["categories"] == ["giro"]

    def test_plain_zip_and_unzipped_folder(self, tmp_path):
        z = write_zip(tmp_path / "flat.zip", nested=False)
        home = tmp_path / "home"
        home.mkdir()
        assert len(notion.import_export(home, z).created) == 7
        folder = tmp_path / "unzipped"
        with zipfile.ZipFile(z) as zf:
            notion._extract(zf, folder)
        assert len(notion.import_export(home, folder).unchanged) == 7

    def test_not_a_notion_export(self, tmp_path):
        bad = tmp_path / "x.zip"
        bad.write_bytes(b"not a zip")
        with pytest.raises(ValueError):
            notion.import_export(tmp_path, bad)
        empty = tmp_path / "empty.zip"
        with zipfile.ZipFile(empty, "w") as zf:
            zf.writestr("readme.txt", "hi")
        with pytest.raises(ValueError):
            notion.import_export(tmp_path, empty)

    def test_table_only_export_says_what_to_do(self, tmp_path):
        z = tmp_path / "Tango Note.zip"  # exported without "Include subpages": just the CSVs
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("Tango Note 211f7c600996800cb6d2c650c34337e8.csv", "\ufeff이름,사람\n깔리토스,\n")
            zf.writestr("Tango Note 211f7c600996800cb6d2c650c34337e8_all.csv", "\ufeff이름,사람\n깔리토스,\n")
        with pytest.raises(ValueError, match="하위 페이지 포함"):
            notion.import_export(tmp_path, z)

    def test_dry_run_writes_nothing(self, tmp_path, export):
        home = tmp_path / "home"
        home.mkdir()
        assert len(notion.import_export(home, export, dry_run=True).created) == 7
        assert find_lessons(home) == []


@pytest.mark.parametrize("text,want", [
    ("2025년 12월 13일", "2025-12-13"), ("2026년 9월 26일 오후 3:00", "2026-09-26"), ("December 13, 2025", "2025-12-13"),
    ("Dec 3, 2025 → Dec 4, 2025", "2025-12-03"), ("2025/12/13", "2025-12-13"), ("@2025-01-02", "2025-01-02"),
    ("", None), ("언젠가", None),
])
def test_parse_date(text, want):
    assert notion.parse_date(text) == want


@pytest.mark.parametrize("title,kind", [
    ("소똥 개인레슨", "lesson"), ("Brenno y fati class", "lesson"), ("10. 26. 쁘락 오답 체크", "practica"),
    ("쁘락 노트 총정리", "practica"), ("로빈 저녁 피드백", "feedback"), ("음악  정리", "note"), ("10 22", "note"),
])
def test_kind_of(title, kind):
    assert notion.kind_of(title) == kind


class TestMarkdown:
    def test_escapes_everything_else(self):
        html = markdown.render('<img src=x onerror=alert(1)> [x](javascript:alert(1)) ![a](javascript:x)')
        assert "<img src=x" not in html and "javascript:" not in html

    def test_nested_lists(self):
        html = markdown.render("- a\n    - b\n        - c\n- d\n\n1. one\n2. two")
        assert html == "<ul><li>a<ul><li>b<ul><li>c</li></ul></li></ul></li><li>d</li></ul>\n<ol><li>one</li><li>two</li></ol>"

    def test_inline(self):
        html = markdown.render("**굵게** *기울임* ~~취소~~ `코드` https://example.com/a.")
        assert "<strong>굵게</strong>" in html and "<em>기울임</em>" in html and "<del>취소</del>" in html
        assert "<code>코드</code>" in html and 'href="https://example.com/a"' in html

    def test_code_fence_and_quote(self):
        html = markdown.render("```\n<b>x</b>\n```\n\n> 인용\n> 둘째")
        assert "<pre><code>&lt;b&gt;x&lt;/b&gt;</code></pre>" in html and "<blockquote>" in html


def test_web_upload_and_doc(tmp_path, export):
    import threading
    import urllib.request

    from sequenceguider.web import make_server

    home = tmp_path / "home"
    srv = make_server(home, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        req = urllib.request.Request(base + "/api/import/notion", data=export.read_bytes(), method="POST",
                                     headers={"Content-Type": "application/zip"})
        with urllib.request.urlopen(req) as r:
            assert r.status == 201 and json.loads(r.read())["created"] == 7
        with urllib.request.urlopen(base + "/api/lessons") as r:
            ls = {l["title"]: l for l in json.loads(r.read())}
        lid = ls["소똥 개인레슨"]["id"]
        from urllib.parse import quote
        with urllib.request.urlopen(f"{base}/api/lessons/{quote(lid)}/doc") as r:
            doc = json.loads(r.read())
        assert doc["title"] == "소똥 개인레슨" and len(doc["pages"]) == 3
        img = doc["pages"][0]["html"].split('src="')[1].split('"')[0]
        with urllib.request.urlopen(base + quote(img)) as r:
            assert r.read().startswith(b"\x89PNG")
        assert not any((home / ".imports").iterdir())
    finally:
        srv.shutdown()
        srv.server_close()


def test_table_cells_with_line_breaks():
    html = markdown.render("| 동작 | 주의 |\n| --- | --- |\n| 안기 | 1. 상체\n  2. 손가락 |\n| 걷기 | 축 |")
    assert html.count("<tr>") == 3 and "1. 상체<br>2. 손가락" in html
