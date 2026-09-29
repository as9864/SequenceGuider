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
from sequenceguider.youtube import (
    Chapter, Cue, VideoSource, chapters_from_description, is_url, matches_from_text, matches_from_transcript,
    parse_json3, parse_vtt, sequence_from_metadata,
)
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


class TestMentions:
    def test_several_in_order(self):
        found = LIB.mentions("살리다 하고 나서 오초 아뜨라스, 그다음 Fuera de eje")
        assert [m.figure.key for m in found] == ["salida_cruzada", "ocho_atras", "fuera_de_eje"]

    def test_longest_alias_wins_overlap(self):
        # "오초 아델란떼" is one mention, not also a bare "오초" (= ocho atrás)
        assert [m.figure.key for m in LIB.mentions("오초 아델란떼를 연습")] == ["ocho_adelante"]

    def test_span_points_into_original_text(self):
        text = "이제 Ocho Atrás 합니다"
        (m,) = LIB.mentions(text)
        assert text[m.start : m.end] == "Ocho Atrás"

    def test_skip(self):
        assert LIB.mentions("walk and turn", skip=frozenset({"walk", "turn"})) == []


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


def test_parse_json3():
    raw = json.dumps({"events": [
        {"tStartMs": 0, "dDurationMs": 1500, "segs": [{"utf8": "안녕하세요"}]},
        {"tStartMs": 1500, "dDurationMs": 10, "aAppend": 1, "segs": [{"utf8": "\n"}]},  # line-break filler
        {"tStartMs": 2000, "dDurationMs": 2000, "segs": [{"utf8": "오초 "}, {"utf8": "아뜨라스"}]},
        {"tStartMs": 4000},  # no text
    ]})
    assert parse_json3(raw) == [Cue(0.0, 1.5, "안녕하세요"), Cue(2.0, 4.0, "오초 아뜨라스")]


def test_parse_vtt_drops_rolling_repeats():
    raw = """WEBVTT
Kind: captions

00:00:01.000 --> 00:00:03.000 align:start
오늘은 <c>볼레오</c>

00:00:03.000 --> 00:00:03.010
오늘은 볼레오

1
00:01:05.500 --> 00:01:07.000
사까다 해 볼게요
"""
    assert parse_vtt(raw) == [Cue(1.0, 3.0, "오늘은 볼레오"), Cue(65.5, 67.0, "사까다 해 볼게요")]


class TestPickCaption:
    @staticmethod
    def tracks(tag):
        return [{"ext": "vtt", "url": f"{tag}.vtt"}, {"ext": "json3", "url": f"{tag}.json3"}]

    def test_uploaded_subtitles_first(self):
        info = {"language": "es", "subtitles": {"ko": self.tracks("ko"), "es": self.tracks("es")},
                "automatic_captions": {"es-orig": self.tracks("auto")}}
        assert yt._pick_caption(info) == ("es", {"ext": "json3", "url": "es.json3"})

    def test_original_auto_captions_not_translations(self):
        info = {"subtitles": {"live_chat": self.tracks("chat")},
                "automatic_captions": {"en": self.tracks("en-tr"), "ko-orig": self.tracks("ko"),
                                       "ko": self.tracks("ko")}}
        assert yt._pick_caption(info) == ("ko", {"ext": "json3", "url": "ko.json3"})

    def test_only_translations_means_none(self):
        assert yt._pick_caption({"automatic_captions": {"en": self.tracks("en"), "ja": self.tracks("ja")}}) is None
        assert yt._pick_caption({}) is None


def _cues(*lines):
    """(start_s, text) -> 2 s cues"""
    return [Cue(t, t + 2, text) for t, text in lines]


class TestTranscript:
    def test_sections_start_at_first_mention(self):
        cues = _cues(
            (0, "안녕하세요 오늘은"),
            (5, "먼저 살리다부터 해 볼게요"),
            (9, "살리다는 뒤로 열고"),
            (30, "이제 오초"),  # the name is split over two caption lines
            (32, "아뜨라스 할게요"),
            (40, "오초 아뜨라스는 골반이"),
            (60, "마지막으로 뿌에라 에헤"),
        )
        ms = matches_from_transcript(cues, LIB, 90.0)
        assert [m.figure.key for m in ms] == ["salida_cruzada", "ocho_atras", "fuera_de_eje"]
        assert [(m.chapter.start_s, m.chapter.end_s) for m in ms] == [(5, 30), (30, 60), (60, 90.0)]
        assert "살리다" in ms[0].chapter.title

    def test_passing_reference_is_not_a_section(self):
        cues = _cues((0, "볼레오 할게요"), (10, "아까 살리다에서처럼 골반을"), (20, "볼레오는 반동으로"),
                     (40, "사까다"), (60, "다시 볼레오"))
        ms = matches_from_transcript(cues, LIB, None)
        # the lone 살리다 inside the 볼레오 part is dropped; 볼레오 coming back later is a new section
        assert [m.figure.key for m in ms] == ["boleo", "sacada", "boleo"]
        assert [m.chapter.start_s for m in ms] == [0, 40, 60]

    def test_everyday_words_ignored(self):
        cues = _cues((0, "천천히 걷기부터, stop 하지 말고 turn"), (5, "잠깐 정지"))
        assert matches_from_transcript(cues, LIB, 10.0) == []

    def test_used_only_without_chapters(self):
        cues = _cues((0, "사까다"))
        src = _source([Chapter(0, None, "볼레오")])
        src.transcript = cues
        assert sequence_from_metadata(src, LIB).source == "chapters"
        src = _source([], description="0:00 까미나따\n0:10 볼레오")
        src.transcript = cues
        assert sequence_from_metadata(src, LIB).source == "description"
        src = _source([])
        src.transcript = cues
        meta = sequence_from_metadata(src, LIB)
        assert meta.source == "transcript" and [i.figure.key for i in meta.items] == ["sacada"]
        assert meta.items[0].end_s == 60.0
        src.transcript = []
        assert sequence_from_metadata(src, LIB).source == "none"


AI_SUMMARY = """이 영상은 탱고 수업으로, 기본 동작을 순서대로 보여줍니다.
- 0:30 살리다 크루사다로 시작해 기본 8박을 익힙니다.
- 1:45 오초 아뜨라스를 세 번 반복하며, 살리다처럼 골반을 먼저 돌리라고 강조합니다.
- 오초 아뜨라스에서는 축을 유지하는 것이 중요합니다.
- 3:10 마지막으로 뿌에라 데 에헤(Fuera de eje)로 마무리합니다. 두 번째 시도에서는 더 깊게 기울입니다.
요약: 살리다 → 오초 아뜨라스 → 푸에라 데 에헤"""


class TestSummaryText:
    def test_ai_summary(self):
        ms = matches_from_text(AI_SUMMARY, LIB)
        # 살리다처럼 = comparison, the closing recap isn't counted twice, "두 번째" isn't a count
        assert [(m.figure.key, m.count, m.chapter.start_s) for m in ms] == [
            ("salida_cruzada", 1, 30.0), ("ocho_atras", 3, 105.0), ("fuera_de_eje", 1, 190.0)]
        assert ms[1].chapter.title.startswith("- 1:45 오초 아뜨라스를 세 번")

    def test_plain_list_without_times(self):
        ms = matches_from_text("살리다, 오초 아뜨라스 x3, 사까다(2회) 그리고 볼레오 순서로 연습합니다", LIB)
        assert [(m.figure.key, m.count, m.chapter.start_s) for m in ms] == [
            ("salida_cruzada", 1, None), ("ocho_atras", 3, None), ("sacada", 2, None), ("boleo", 1, None)]
        meta = yt.MetadataSequence("text", ms)
        assert meta.as_sequence_text() == "살리다 크루사다, 오초 아뜨라스 x3, 사까다 x2, 볼레오"
        assert [(i.figure.key, i.count, i.anchor_s) for i in meta.items] == \
            [(i.figure.key, i.count, i.anchor_s) for i in parse_sequence(meta.as_sequence_text(), LIB)]

    def test_topic_of_the_sentence_is_kept(self):
        # the sentence is about 볼레오; 사까다 later in it is a comparison
        ms = matches_from_text("먼저 오초. 볼레오를 배우는데 오초 대신 사까다처럼 쓰는 점이 특징. 오초로 돌아와 마무리", LIB)
        assert [m.figure.key for m in ms] == ["ocho_atras", "boleo", "ocho_atras"]

    def test_times_must_go_forward(self):
        ms = matches_from_text("2:00 볼레오\n1:00 사까다\n3:00 간초", LIB)
        assert [m.chapter.start_s for m in ms] == [120.0, None, 180.0]

    def test_nothing(self):
        assert matches_from_text("오늘 수업 즐거웠습니다. 다음 주에 만나요!", LIB) == []


class FakeYDL:
    """yt_dlp.YoutubeDL stand-in: fixed info dict, captions served from a dict."""
    downloads = 0

    def __init__(self, info, captions):
        self.info, self.captions = info, captions

    def __call__(self, opts):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download):
        FakeYDL.downloads += bool(download)
        return self.info

    def urlopen(self, url):
        import io

        if url not in self.captions:
            raise OSError("HTTP Error 429")
        return io.BytesIO(self.captions[url].encode())


CAPTION_INFO = {
    "id": "abcdefghijk", "title": "수업", "duration": 60.0, "description": "", "chapters": None,
    "webpage_url": "https://youtu.be/abcdefghijk",
    "automatic_captions": {"ko-orig": [{"ext": "json3", "url": "cap"}]},
}
CAPTION = json.dumps({"events": [{"tStartMs": 3000, "dDurationMs": 1000, "segs": [{"utf8": "볼레오 할게요"}]}]})


class TestFetchTranscript:
    def test_fetch_info_reads_captions(self, monkeypatch):
        import yt_dlp

        monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL(CAPTION_INFO, {"cap": CAPTION}))
        src = yt.fetch_info("https://youtu.be/abcdefghijk")
        assert src.transcript == [Cue(3.0, 4.0, "볼레오 할게요")] and src.transcript_lang == "ko"

    def test_caption_error_is_just_no_transcript(self, monkeypatch):
        import yt_dlp

        monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL(CAPTION_INFO, {}))
        src = yt.fetch_info("https://youtu.be/abcdefghijk")
        assert src.transcript == [] and sequence_from_metadata(src, LIB).source == "none"

    def test_captions_not_fetched_when_chapters_exist(self, monkeypatch):
        import yt_dlp

        info = {**CAPTION_INFO, "chapters": [{"start_time": 0, "end_time": 60, "title": "사까다"}]}
        monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL(info, {}))  # a caption fetch would raise
        assert yt.fetch_info("https://youtu.be/abcdefghijk").transcript == []

    def test_old_cache_without_transcript_is_upgraded(self, tmp_path, monkeypatch):
        import yt_dlp

        video = tmp_path / "abcdefghijk.mp4"
        video.write_bytes(b"x")
        old = _source([])
        old.path = str(video)
        old.save(tmp_path / "source.json")  # transcript None: written before captions were read
        fake = FakeYDL({**CAPTION_INFO, "requested_downloads": [{"filepath": str(video)}]}, {"cap": CAPTION})
        monkeypatch.setattr(yt_dlp, "YoutubeDL", fake)
        src = yt.fetch_video("https://youtu.be/abcdefghijk", tmp_path)
        assert src.transcript and src.path == str(video)
        # second run: served from source.json, no yt-dlp call at all
        monkeypatch.setattr(yt_dlp, "YoutubeDL", None)
        again = yt.fetch_video("https://youtu.be/abcdefghijk", tmp_path)
        assert again.transcript == src.transcript and again.transcript_lang == "ko"


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

    def test_analyze_url_reads_sequence_from_captions(self, tmp_path, fake_youtube, monkeypatch):
        src = yt.fetch_video("x", tmp_path)  # the fixture's source
        src.chapters = []
        src.transcript = _cues((0.5, "살리다 크루사다 먼저"), (4.5, "이제 오초 아뜨라스"))
        src.transcript_lang = "ko"
        out = tmp_path / "out"
        res = CliRunner().invoke(app, ["analyze", "https://youtu.be/abcdefghijk", "--out", str(out), "--no-overlay"])
        assert res.exit_code == 0, res.output
        assert "영상 자막 (ko)" in res.output
        data = json.loads((out / "result.json").read_text(encoding="utf-8"))
        assert [f["figure"] for f in data["figures"]] == ["salida_cruzada", "ocho_atras"]
        assert data["sequence_source"] == "영상 자막"

    def test_preview_nothing_found(self, fake_youtube):
        src = yt.fetch_info("x")
        src.chapters, src.transcript = [], []
        res = CliRunner().invoke(app, ["preview", "https://youtu.be/abcdefghijk"])
        assert res.exit_code == 0 and "-s로 순서를 직접" in res.output

    def test_preview_summary_from_file_and_stdin(self, tmp_path):
        f = tmp_path / "요약.txt"
        f.write_text(AI_SUMMARY, encoding="utf-8")
        for args, stdin in ((["preview", "--summary", str(f)], None), (["preview", "--summary", "-"], AI_SUMMARY)):
            res = CliRunner().invoke(app, args, input=stdin)
            assert res.exit_code == 0, res.output
            assert "붙여넣은 요약글에서 3개 항목" in res.output
            assert '-s "살리다 크루사다 @0:30, 오초 아뜨라스 x3 @1:45, 푸에라 데 에헤 @3:10"' in res.output

    def test_summary_as_literal_text(self):
        res = CliRunner().invoke(app, ["preview", "--summary", "살리다 다음 볼레오"])
        assert res.exit_code == 0 and '-s "살리다 크루사다, 볼레오"' in res.output

    def test_summary_without_figures(self):
        res = CliRunner().invoke(app, ["preview", "--summary", "좋은 수업이었어요"])
        assert res.exit_code == 2 and "찾지 못했습니다" in res.output

    def test_preview_needs_url_or_summary(self):
        assert CliRunner().invoke(app, ["preview"]).exit_code == 2

    def test_analyze_url_with_summary_beats_chapters(self, tmp_path, fake_youtube):
        out = tmp_path / "out"
        res = CliRunner().invoke(app, ["analyze", "https://youtu.be/abcdefghijk", "--summary", "까미나따 하고 볼레오",
                                       "--out", str(out), "--no-overlay"])
        assert res.exit_code == 0, res.output
        data = json.loads((out / "result.json").read_text(encoding="utf-8"))
        assert [f["figure"] for f in data["figures"]] == ["caminata", "boleo"]
        assert data["sequence_source"] == "붙여넣은 요약글"

    def test_analyze_file_with_summary(self, tmp_path, monkeypatch):
        track = walk(9)
        video = tmp_path / "연습.mp4"
        _write_video(video, track)
        monkeypatch.setattr(mp_backend, "extract_pose", lambda *a, **kw: track)
        res = CliRunner().invoke(app, ["analyze", str(video), "--summary", "-", "--no-overlay"],
                                 input="영상 요약: 살리다 크루사다로 시작하고 오초 아뜨라스로 끝납니다.")
        assert res.exit_code == 0, res.output
        data = json.loads((tmp_path / "연습_sequenceguider" / "result.json").read_text(encoding="utf-8"))
        assert [f["figure"] for f in data["figures"]] == ["salida_cruzada", "ocho_atras"]

    def test_sequence_and_summary_conflict(self, tmp_path):
        res = CliRunner().invoke(app, ["analyze", "https://youtu.be/abcdefghijk", "-s", "볼레오", "--summary", "볼레오"])
        assert res.exit_code == 2 and "하나만" in res.output

    def test_file_without_sequence_is_an_error(self, tmp_path):
        f = tmp_path / "a.mp4"
        f.write_bytes(b"x")
        res = CliRunner().invoke(app, ["analyze", str(f)])
        assert res.exit_code == 2
