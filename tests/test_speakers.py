"""Speaker turns (`notes --speakers`) with the voice-fingerprint and language models replaced by fakes."""

import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from typer.testing import CliRunner

import sequenceguider.notes as N
import sequenceguider.speakers as S
from sequenceguider.cli import app
from sequenceguider.web import Jobs, make_server
from tests.test_web import call

SPEC = "남자 강사=남/en, 여자 강사=여/en, 통역=남/ko"
SR = S.SR


def test_parse_speakers():
    sp = S.parse_speakers(SPEC)
    assert [(p.name, p.gender, p.language) for p in sp] == [
        ("남자 강사", "male", "en"), ("여자 강사", "female", "en"), ("통역", "male", "ko")]
    assert S.parse_speakers("Ana = female spanish, 나 = 한국어")[1].language == "ko"
    for bad in ("a=남, a=여", "=남/en", "a=외계어"):
        with pytest.raises(ValueError):
            S.parse_speakers(bad)


def test_is_korean():
    assert S.is_korean("공중으로 올라가는 거 하지 마세요") and S.is_korean("임펄스(impulse)가 중요해요")
    assert not S.is_korean("Effortless, yes?") and not S.is_korean("1, 2, 3")


def _tone(hz: float, seconds: float) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    # a buzzy voice-like tone: fundamental plus harmonics
    return (0.1 * sum(np.sin(2 * np.pi * hz * k * t) / k for k in range(1, 6))).astype(np.float32)


def test_voice_pitch():
    assert S.voice_pitch(_tone(120, 1.0)) == pytest.approx(120, rel=0.05)
    assert S.voice_pitch(_tone(220, 1.0)) == pytest.approx(220, rel=0.05)
    assert S.voice_pitch(np.zeros(SR, dtype=np.float32)) == 0.0


def test_fbank_shape():
    f = S.fbank(_tone(150, 1.0))
    assert f.shape == (98, 80) and abs(float(f.mean())) < 1e-3  # mean-normalised


# three "people": a low English voice, a high English voice and a low Korean one,
# taking turns with a silence between each
SCRIPT = [("en", 110), ("ko", 125), ("en", 220), ("ko", 125), ("en", 110), ("en", 220), ("ko", 125)]


def _recording():
    gap = np.zeros(SR, dtype=np.float32)
    parts, spans = [gap], []
    t = 1.0
    for lang, hz in SCRIPT:
        parts += [_tone(hz, 4.0), gap]
        spans.append((t, t + 4.0, lang, hz))
        t += 5.0
    return np.concatenate(parts), spans


class FakeEmbedder:
    """A 'fingerprint' that is just the pitch: same pitch, same voice."""

    def __call__(self, samples):
        hz = S.voice_pitch(samples)
        v = np.array([np.cos(hz / 100), np.sin(hz / 100), 0.1])
        return v / np.linalg.norm(v)


def _fake_language(spans):
    def detect(samples):
        hz = S.voice_pitch(samples)
        lang = next(lg for _, _, lg, h in spans if abs(h - hz) < 5)
        return {lang: 0.9, "es": 0.05}
    return detect


@pytest.fixture
def no_vad(monkeypatch):
    """Silero VAD finds nothing in pure tones: say where the tones are instead."""
    _, spans = _recording()
    monkeypatch.setattr(S, "speech_pieces", lambda audio: [(a, b) for a, b, *_ in spans])
    return spans


def test_diarize(no_vad):
    audio, spans = _recording()
    log = []
    turns = S.diarize(audio, S.parse_speakers(SPEC), _fake_language(spans), embedder=FakeEmbedder(),
                      extra_groups=0, log=log.append)
    assert [(t.speaker, t.language) for t in turns] == [
        ("남자 강사", "en"), ("통역", "ko"), ("여자 강사", "en"), ("통역", "ko"), ("남자 강사", "en"),
        ("여자 강사", "en"), ("통역", "ko")]
    assert len(log) == 3 and any("통역" in line for line in log)
    runs = S.language_runs(turns)
    assert [r.language for r in runs] == ["en", "ko", "en", "ko", "en", "ko"]
    assert runs[4].start == turns[4].start and runs[4].end == turns[5].end
    assert S.speaker_at(turns, 26.5, 29.0) == "여자 강사" and S.speaker_at(turns, 5.2, 5.8) in ("남자 강사", "통역")


class TurnWhisper:
    """Answers each clip with a line in the language it was asked for."""

    def __init__(self):
        self.calls = []

    def transcribe(self, audio, language=None, clip_timestamps=None, **kw):
        self.calls.append({"language": language, "clips": clip_timestamps, **kw})
        clips = clip_timestamps or [0, len(audio) / SR]
        text = {"ko": "한국어 통역 {}", "en": "English line {}"}[language]
        lines = [SimpleNamespace(start=a + 0.5, end=b - 0.5, text=text.format(i), no_speech_prob=0.0, avg_logprob=-0.2)
                 for i, (a, b) in enumerate(zip(clips[::2], clips[1::2]))]
        return iter(lines), SimpleNamespace(language=language)

    def detect_language(self, samples):
        return None, None, list(_fake_language(_recording()[1])(samples).items())


def test_transcribe_by_language_runs(tmp_path, no_vad):
    audio, spans = _recording()
    voices = S.parse_speakers(SPEC)
    whisper = TurnWhisper()
    turns = S.diarize(audio, voices, _fake_language(spans), embedder=FakeEmbedder(), extra_groups=0)
    segs, lang, _ = N.transcribe(tmp_path / "x.m4a", tmp_path / "cache", audio=audio, turns=turns, whisper=whisper,
                                 vocabulary="Clase de tango. sacada, giro.")
    # one pass per language, over that language's stretches only; no Spanish hint for the Korean ones
    assert sorted(c["language"] for c in whisper.calls) == ["en", "ko"]
    ko = next(c for c in whisper.calls if c["language"] == "ko")
    assert ko["clips"] == [6.0, 10.0, 16.0, 20.0, 31.0, 35.0] and ko["initial_prompt"] is None
    assert [s.speaker for s in segs] == ["남자 강사", "통역", "여자 강사", "통역", "남자 강사", "통역"]
    assert all(N.is_korean(s.text) == (s.speaker == "통역") for s in segs)
    assert lang == "en"  # most of the talk


def test_made_up_lines_are_dropped():
    seg = SimpleNamespace(no_speech_prob=0.1, avg_logprob=-0.3)
    vocab = "Clase de tango. sacada, giro."
    assert N._made_up("Clase de tango.", vocab, seg)
    assert N._made_up("다음 영상에서 만나요!", vocab, seg) and N._made_up("시청해주셔서 감사합니다.", vocab, seg)
    assert N._made_up("Okay", vocab, SimpleNamespace(no_speech_prob=0.9, avg_logprob=-1.5))
    assert not N._made_up("Giro.", vocab, seg) and not N._made_up("상체를 낮추는 방법입니다.", vocab, seg)


def test_korean_lines_fill_in_after_translation():
    segs = [N.Segment(0, 1, "Lower the body", ko="상체를 낮추세요"), N.Segment(1, 2, "상체를 낮추는 방법입니다.")]
    N.korean_lines(segs)
    assert segs[1].ko == "상체를 낮추는 방법입니다."
    only_korean = [N.Segment(0, 1, "안녕하세요")]
    N.korean_lines(only_korean)
    assert only_korean[0].ko == ""  # a Korean lesson keeps one text, not two copies


def test_cli_notes_audio_with_speakers(tmp_path, monkeypatch, no_vad):
    audio, spans = _recording()
    whisper = TurnWhisper()
    monkeypatch.setattr(N, "_whisper", lambda model: whisper)
    monkeypatch.setattr(N, "load_audio", lambda media: audio)
    monkeypatch.setattr(S, "Embedder", FakeEmbedder)

    def no_key():
        raise RuntimeError("Could not resolve authentication method")

    monkeypatch.setattr(N, "make_client", no_key)
    rec = tmp_path / "workshop.m4a"
    rec.write_bytes(b"not decoded: load_audio is faked")
    res = CliRunner().invoke(app, ["notes", str(rec), "--speakers", SPEC])
    assert res.exit_code == 0, res.output
    assert "통역" in res.output and "화자별 말한 시간" in res.output
    out = tmp_path / "workshop_sequenceguider"
    notes = json.loads((out / "notes.json").read_text(encoding="utf-8"))
    assert notes["media"] == "audio" and [p["name"] for p in notes["speakers"]] == ["남자 강사", "여자 강사", "통역"]
    srt = (out / "transcript.srt").read_text(encoding="utf-8")
    assert "[통역] 한국어 통역" in srt and "[여자 강사] English line" in srt

    # same speakers again: everything reused; different speakers: done again
    n = len(whisper.calls)
    res = CliRunner().invoke(app, ["notes", str(rec), "--speakers", SPEC])
    assert "자막 재사용" in res.output and len(whisper.calls) == n
    res = CliRunner().invoke(app, ["notes", str(rec), "--speakers", "선생님=en, 통역=ko"])
    assert res.exit_code == 0 and len(whisper.calls) > n

    res = CliRunner().invoke(app, ["notes", str(rec), "--speakers", "a=외계어"])
    assert res.exit_code == 2


def test_audio_served_to_the_player(tmp_path):
    home = tmp_path / "home"
    lesson = home / "w_sequenceguider"
    lesson.mkdir(parents=True)
    rec = tmp_path / "workshop.m4a"
    rec.write_bytes(bytes(range(256)))
    (lesson / "notes.json").write_text(json.dumps({"title": "t", "video": str(rec), "media": "audio"}), encoding="utf-8")
    srv = make_server(home, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, headers, body = call(f"http://127.0.0.1:{srv.server_address[1]}", "/media/w_sequenceguider/main",
                                     headers={"Range": "bytes=0-3"})
        assert status == 206 and body == bytes(range(4)) and headers["Content-Type"] == "audio/mp4"
    finally:
        srv.shutdown()
        srv.server_close()


def test_notes_job_passes_speakers(tmp_path):
    rec = tmp_path / "w.m4a"
    rec.write_bytes(b"")
    jobs = Jobs(tmp_path / "home")
    jobs.command = ["python", "-c", "pass"]
    job = jobs.start(str(rec), kind="notes", speakers=SPEC)
    args = job._proc.args
    assert args[args.index("--speakers") + 1] == SPEC
