"""YouTube input: metadata -> sequence, and the CLI path end to end with the
network (yt-dlp) and the pose model replaced by local fakes."""

import json

import pytest
from typer.testing import CliRunner

import sequenceguider.pose.mediapipe_backend as mp_backend
import sequenceguider.youtube as yt
from sequenceguider.align import align_sequence
from sequenceguider.cli import _youtube_id, app
from sequenceguider.figures import FigureLibrary, parse_sequence
from sequenceguider.steps import detect_steps
from sequenceguider.youtube import Chapter, VideoSource, chapters_from_description, is_url, sequence_from_metadata
from tests.synthetic import walk
from tests.test_pipeline import _write_video

LIB = FigureLibrary.load()


def test_is_url():
    assert is_url("https://youtu.be/abc123") and is_url("http://www.youtube.com/watch?v=x")
    assert not is_url("practice.mp4") and not is_url("C:/videos/a.mp4")


@pytest.mark.parametrize("url, vid", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
    ("https://youtube.com/shorts/AbCdEf12345", "AbCdEf12345"),
    ("https://example.com/video", "video"),
])
def test_youtube_id(url, vid):
    assert _youtube_id(url) == vid


class TestFindInText:
    @pytest.mark.parametrize("title, key", [
        ("3. 오초 아뜨라스 (Ocho atrás) 연습", "ocho_atras"),
        ("오초 아델란떼 기초", "ocho_adelante"),  # longest alias beats bare "오초"
        ("Fuera de eje with the leader", "fuera_de_eje"),
        ("뿌에라 에헤 디테일", "fuera_de_eje"),
        ("Sacadas for followers", "sacada"),  # English plural
        ("볼레오를 부드럽게", "boleo"),  # Korean particle attached
    ])
    def test_matches(self, title, key):
        fig = LIB.find_in_text(title)
        assert fig is not None and fig.key == key, title

    @pytest.mark.parametrize("title", ["인트로", "Return to the embrace", "Q&A", "마무리 인사"])
    def test_non_figures(self, title):
        assert LIB.find_in_text(title) is None, title


def test_chapters_from_description():
    desc = """오늘 수업 순서입니다
0:00 인트로
0:45 - 살리다 크루사다
[2:10] 오초 아뜨라스 x3
1:02:03 뿌에라 에헤
링크: https://example.com 10:30에 만나요?"""
    chs = chapters_from_description(desc)
    assert [c.start_s for c in chs] == [0.0, 45.0, 130.0, 630.0, 3723.0]  # the stray "10:30" becomes a
    # junk chapter, which is harmless: it won't match any figure
    titles = [c.title for c in chs]
    assert "살리다 크루사다" in titles and "오초 아뜨라스 x3" in titles and "뿌에라 에헤" in titles
    assert chs[0].end_s == chs[1].start_s
    assert all(a.start_s < b.start_s for a, b in zip(chs, chs[1:]))


def test_single_timestamp_is_not_a_table_of_contents():
    assert chapters_from_description("공연 영상입니다. 2:30부터 볼레오가 멋있어요") == []


def _source(chapters, duration=60.0, description=""):
    return VideoSource(url="https://youtu.be/abcdefghijk", id="abcdefghijk", title="수업 영상",
                       duration_s=duration, description=description, chapters=chapters)


def test_sequence_from_chapters():
    src = _source([
        Chapter(0, 5, "인트로"),
        Chapter(5, 20, "까미나따"),
        Chapter(20, 35, "오초 아뜨라스 3회"),
        Chapter(35, 40, "Q&A"),
        Chapter(40, None, "Fuera de eje"),
    ])
    meta = sequence_from_metadata(src, LIB)
    assert meta.source == "chapters"
    items = meta.items
    assert [i.figure.key for i in items] == ["caminata", "ocho_atras", "fuera_de_eje"]
    assert items[1].count == 3
    assert (items[1].anchor_s, items[1].end_s) == (20, 35)
    assert items[2].end_s == 60.0  # open last chapter closed at the video end
    # the editable -s string parses back to the same thing
    again = parse_sequence(meta.as_sequence_text(), LIB)
    assert [(i.figure.key, i.count, i.anchor_s) for i in again] == [(i.figure.key, i.count, i.anchor_s) for i in items]


def test_description_fallback():
    meta = sequence_from_metadata(_source([], description="0:05 까미나따\n0:20 볼레오"), LIB)
    assert meta.source == "description" and [i.figure.key for i in meta.items] == ["caminata", "boleo"]


def test_chapter_end_excludes_non_figure_chapter():
    """A Q&A chapter between figures must not be absorbed into the figure before it."""
    tr = walk(10)  # steps from 1.0s to ~8.6s
    src = _source([Chapter(0, 4, "까미나따"), Chapter(4, 6, "Q&A"), Chapter(6, None, "볼레오")],
                  duration=float(tr.t[-1]))
    segs = align_sequence(sequence_from_metadata(src, LIB).items, detect_steps(tr), float(tr.t[-1]))
    assert segs[0].end_t == pytest.approx(4.0)
    assert segs[1].start_t == pytest.approx(6.0)


class TestCli:
    @pytest.fixture
    def fake_youtube(self, tmp_path, monkeypatch):
        track = walk(9)
        video = tmp_path / "dl" / "abcdefghijk.mp4"
        video.parent.mkdir()
        _write_video(video, track)
        src = _source([Chapter(0, 4.5, "인트로 & 살리다 크루사다"), Chapter(4.5, None, "오초 아뜨라스")],
                      duration=float(track.t[-1]))
        src.path = str(video)
        calls = {}

        def fake_fetch(url, dest_dir, **kw):
            calls["url"] = url
            return src

        monkeypatch.setattr(yt, "fetch_video", fake_fetch)
        monkeypatch.setattr(yt, "fetch_info", lambda url: src)
        monkeypatch.setattr(mp_backend, "extract_pose", lambda *a, **kw: track)
        return calls

    def test_analyze_url_reads_sequence_from_chapters(self, tmp_path, fake_youtube):
        out = tmp_path / "out"
        res = CliRunner().invoke(app, ["analyze", "https://youtu.be/abcdefghijk", "--out", str(out), "--no-overlay"])
        assert res.exit_code == 0, res.output
        assert fake_youtube["url"] == "https://youtu.be/abcdefghijk"
        assert "살리다 크루사다" in res.output and "오초 아뜨라스" in res.output
        data = json.loads((out / "result.json").read_text(encoding="utf-8"))
        assert [f["figure"] for f in data["figures"]] == ["salida_cruzada", "ocho_atras"]
        assert data["figures"][1]["start_s"] == pytest.approx(4.5)
        assert data["sequence_source"] == "영상 챕터"
        page = (out / "index.html").read_text(encoding="utf-8")
        assert "https://youtu.be/abcdefghijk" in page and "수업 영상" in page

    def test_explicit_sequence_wins(self, tmp_path, fake_youtube):
        out = tmp_path / "out"
        res = CliRunner().invoke(app, ["analyze", "https://youtu.be/abcdefghijk", "-s", "까미나따", "--out", str(out),
                                       "--no-overlay"])
        assert res.exit_code == 0, res.output
        data = json.loads((out / "result.json").read_text(encoding="utf-8"))
        assert [f["figure"] for f in data["figures"]] == ["caminata"]

    def test_preview(self, fake_youtube):
        res = CliRunner().invoke(app, ["preview", "https://youtu.be/abcdefghijk"])
        assert res.exit_code == 0, res.output
        assert '-s "살리다 크루사다 @0:00, 오초 아뜨라스 @0:04"' in res.output

    def test_file_without_sequence_is_an_error(self, tmp_path):
        f = tmp_path / "a.mp4"
        f.write_bytes(b"x")
        res = CliRunner().invoke(app, ["analyze", str(f)])
        assert res.exit_code == 2
