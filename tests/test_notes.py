"""`sequenceguider notes` with the speech model and the Claude API replaced by local fakes."""

import json
import threading
import wave
from contextlib import nullcontext
from types import SimpleNamespace

import numpy as np
import pytest
from typer.testing import CliRunner

import sequenceguider.notes as N
from sequenceguider.cli import app
from sequenceguider.figures import FigureLibrary
from sequenceguider.web import Jobs, find_lessons, make_server
from tests.test_web import call

LIB = FigureLibrary.load()
pytest.importorskip("faster_whisper")


def _wav(path, seconds: float):
    """A tone, so the decoder has real audio to read (the fake whisper ignores it)."""
    t = np.arange(int(seconds * 16000)) / 16000
    pcm = (0.2 * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm.tobytes())


class FakeWhisper:
    """Two lines per chunk, at chunk-relative times; the first chunk is 'music' (nothing said)."""

    def __init__(self):
        self.calls = []

    def transcribe(self, audio, language=None, **kw):
        n = len(self.calls)
        self.calls.append({"seconds": len(audio) / 16000, "language": language, **kw})
        lines = [] if n == 0 else [SimpleNamespace(start=0.5, end=2.0, text=f" Pivot en el eje {n}. "),
                                   SimpleNamespace(start=2.5, end=4.0, text=f"Más despacio {n}.")]
        return iter(lines), SimpleNamespace(language="es")


class FakeClaude:
    """Answers the translate and key-point requests the way the real API would (JSON text)."""

    def __init__(self):
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kw):
        self.requests.append(kw)
        schema = kw["output_config"]["format"]["schema"]
        if "lines" in schema["properties"]:
            ids = [json.loads(ln)["i"] for ln in kw["messages"][0]["content"].split("\n") if ln.startswith("{")]
            out = {"lines": [{"i": i, "ko": f"번역 {i}"} for i in ids]}
        else:
            out = {
                "title": "피벗과 축", "summary": "축 위에서 피벗하는 법을 배웠다.",
                "key_points": [
                    {"t": 99999, "point": "끝", "detail": "", "kind": "other", "role": "all", "quote": ""},
                    {"t": 12.5, "point": "축 위에서 피벗", "detail": "골반을 먼저", "kind": "correction",
                     "role": "follower", "quote": "Pivot en el eje"},
                ],
                "sections": [{"start": 10, "end": 20, "title": "피벗", "summary": "",
                              "figures": ["ocho_atras", "not_a_figure"]}],
                "practice": [{"text": "벽 잡고 피벗 20회", "t": 12.5}, {"text": " ", "t": 0}],
                "glossary": [{"term": "에헤(eje)", "meaning": "축"}],
            }
        msg = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(out))])
        return nullcontext(SimpleNamespace(get_final_message=lambda: msg))


@pytest.fixture
def fakes(monkeypatch):
    whisper, claude = FakeWhisper(), FakeClaude()
    monkeypatch.setattr(N, "CHUNK_S", 10)
    monkeypatch.setattr(N, "_whisper", lambda model: whisper)
    monkeypatch.setattr(N, "make_client", lambda: claude)
    return whisper, claude


def test_transcribe_chunks_offsets_and_resume(tmp_path, fakes):
    whisper, _ = fakes
    audio = tmp_path / "lesson.wav"
    _wav(audio, 25)
    segs, lang, duration = N.transcribe(audio, tmp_path / "cache", model="tiny")
    assert lang == "es" and duration == pytest.approx(25, abs=0.1)
    assert len(whisper.calls) == 3 and whisper.calls[-1]["seconds"] == pytest.approx(5, abs=0.1)
    # chunk 0 was music: language stays open until speech, then is fixed for the rest
    assert [c["language"] for c in whisper.calls] == [None, None, "es"]
    assert [s.start for s in segs] == [10.5, 12.5, 20.5, 22.5]
    assert whisper.calls[2]["initial_prompt"].endswith("Más despacio 1.")
    # a second run reads every chunk from the cache
    again, *_ = N.transcribe(audio, tmp_path / "cache", model="tiny")
    assert len(whisper.calls) == 3 and again == segs


def test_drop_repeats():
    segs = [N.Segment(i, i + 1, "música") for i in range(6)] + [N.Segment(9, 10, "otra")]
    assert [s.text for s in N._drop_repeats(segs)] == ["música"] * 3 + ["otra"]


def test_translate_batches_and_skips_done_lines(fakes):
    _, claude = fakes
    segs = [N.Segment(i, i + 1, f"línea {i}", ko="이미 번역" if i == 0 else "") for i in range(5)]
    saves = []
    N.translate(segs, "es", LIB, claude, batch=2, workers=2, save=lambda: saves.append(1))
    assert [s.ko for s in segs] == ["이미 번역", "번역 1", "번역 2", "번역 3", "번역 4"]
    assert len(claude.requests) == 2 and len(saves) == 2
    assert "오초 아뜨라스" in claude.requests[0]["system"]  # community spelling of the figure names
    assert claude.requests[0]["fallbacks"] == "default"


def test_key_points_clamps_and_filters():
    segs = [N.Segment(12.5, 14, "Pivot en el eje")]
    notes = N.key_points(segs, 30.0, LIB, FakeClaude())
    assert [p["t"] for p in notes["key_points"]] == [12.5, 30.0]
    assert notes["sections"][0]["figures"] == ["ocho_atras"]
    assert [p["text"] for p in notes["practice"]] == ["벽 잡고 피벗 20회"]


def test_srt(tmp_path):
    segs = [N.Segment(3661.25, 3662.5, "hola", ko="안녕"), N.Segment(3663, 3664, "x")]
    N.write_srt(segs, tmp_path / "a.srt")
    N.write_srt(segs, tmp_path / "b.srt", korean=True)
    assert (tmp_path / "a.srt").read_text().startswith("1\n01:01:01,250 --> 01:01:02,500\nhola\n")
    assert (tmp_path / "b.srt").read_text().count("-->") == 1
    assert N.fmt_ts(3725) == "1:02:05" and N.fmt_ts(65) == "1:05"


def test_cli_notes_end_to_end(tmp_path, fakes):
    whisper, claude = fakes
    home = tmp_path / "home"
    home.mkdir()
    video = tmp_path / "private lesson.wav"
    _wav(video, 25)
    out = home / "private lesson_sequenceguider"
    res = CliRunner().invoke(app, ["notes", str(video), "--out", str(out), "--role", "follower"])
    assert res.exit_code == 0, res.output
    assert "축 위에서 피벗" in res.output and "노트:" in res.output

    notes = json.loads((out / "notes.json").read_text(encoding="utf-8"))
    assert notes["title"] == "피벗과 축" and notes["language"] == "es" and notes["translated"]
    assert notes["video"] == str(video.resolve())  # a 2-hour file is pointed at, not copied
    assert notes["practice"] == [{"text": "벽 잡고 피벗 20회", "t": 12.5,
                                  "id": "notes:private lesson_sequenceguider:0"}]
    assert notes["figure_names"] == {"ocho_atras": "오초 아뜨라스"}
    assert "이 학생은 팔로워" in claude.requests[-1]["system"]
    assert (out / "transcript.ko.srt").read_text(encoding="utf-8").count("번역") == 4
    assert "## 핵심" in (out / "notes.md").read_text(encoding="utf-8")

    (lesson,) = find_lessons(home)
    assert lesson["notes"]["key_points"] == 2 and not lesson["has_guide"]
    assert lesson["notes"]["checklist"][0]["id"] == "notes:private lesson_sequenceguider:0"
    assert lesson["notes"]["figures"] == [{"figure": "ocho_atras", "name_ko": "오초 아뜨라스"}]

    # re-run: transcript, translation and key points are all reused
    n_req, n_whisper = len(claude.requests), len(whisper.calls)
    res = CliRunner().invoke(app, ["notes", str(video), "--out", str(out)])
    assert res.exit_code == 0 and "자막 재사용" in res.output
    assert len(claude.requests) == n_req and len(whisper.calls) == n_whisper


def test_cli_notes_without_api_key_keeps_transcript(tmp_path, fakes, monkeypatch):
    def no_key():
        raise RuntimeError("Could not resolve authentication method")

    monkeypatch.setattr(N, "make_client", no_key)
    video = tmp_path / "v.wav"
    _wav(video, 15)
    res = CliRunner().invoke(app, ["notes", str(video)])
    assert res.exit_code == 0, res.output
    out = tmp_path / "v_sequenceguider"
    notes = json.loads((out / "notes.json").read_text(encoding="utf-8"))
    assert notes["key_points"] == [] and "authentication" in notes["incomplete"]
    assert (out / "transcript.srt").exists() and not (out / "transcript.ko.srt").exists()


def test_notes_video_served_from_outside_the_lesson(tmp_path):
    home = tmp_path / "home"
    lesson = home / "l_sequenceguider"
    lesson.mkdir(parents=True)
    video = tmp_path / "elsewhere.mp4"
    video.write_bytes(bytes(range(256)) * 4)
    (lesson / "notes.json").write_text(json.dumps({"title": "t", "video": str(video)}), encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    other = home / "bad_sequenceguider"
    other.mkdir()
    (other / "notes.json").write_text(json.dumps({"video": str(secret)}), encoding="utf-8")

    srv = make_server(home, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        status, headers, body = call(base, "/lessons/l_sequenceguider/notes-video", headers={"Range": "bytes=0-9"})
        assert status == 206 and body == bytes(range(10))
        assert call(base, "/lessons/bad_sequenceguider/notes-video")[0] == 404  # only video files
        assert call(base, "/lessons/l_sequenceguider/notes.json")[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_notes_job_command(tmp_path):
    video = tmp_path / "lesson.mp4"
    video.write_bytes(b"")
    jobs = Jobs(tmp_path / "home")
    jobs.command = ["true"]
    job = jobs.start(str(video), kind="notes", role="leader")
    cmd = job._proc.args
    assert cmd[1:3] == ["notes", str(video.resolve())] and cmd[cmd.index("--out") + 1].endswith(
        "home/lesson_sequenceguider")
    with pytest.raises(ValueError):
        jobs.start(str(video), kind="nope")
