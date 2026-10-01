"""`sequenceguider notes`: transcript, Korean translation and key points of a long lesson video.

A 1–2 hour private lesson is mostly talk: the teacher explains, corrects, shows
again. Three steps turn it into something you can study from:

1. transcribe   speech → timestamped text, locally with faster-whisper. The
                audio is cut into 10-minute chunks and each chunk is cached, so
                an interrupted run picks up where it stopped
2. translate    each line into Korean (Claude API), batch by batch, saved as it
                goes; skipped when the lesson is already in Korean
3. key points   the whole transcript in one request (Claude API): summary, the
                points that matter with the time they were said, how the lesson
                flowed, a practice checklist and a glossary

Written into the lesson folder (<name>_sequenceguider/, same as `analyze`):
    transcript.json            segments: start, end, text, ko
    transcript.srt / .ko.srt   subtitles for any video player
    notes.json                 everything the web app's lesson view shows
    notes.md                   the same notes as plain text

Steps 2–3 need an Anthropic API key (ANTHROPIC_API_KEY). Without one you still
get the transcript and subtitles; run again with a key to add the rest.
"""

import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

from sequenceguider.figures import FigureLibrary

MODEL = "claude-opus-5-5"
SAMPLE_RATE = 16000  # what faster-whisper decodes to
CHUNK_S = 600
KINDS = {
    "correction": "교정",
    "principle": "원리",
    "technique": "테크닉",
    "connection": "연결·아브라소",
    "musicality": "음악성",
    "exercise": "연습법",
    "other": "기타",
}
ROLES = ("all", "leader", "follower")
LANGUAGE_NAMES = {"ko": "한국어", "es": "스페인어", "en": "영어", "ja": "일본어", "fr": "프랑스어", "it": "이탈리아어",
                  "de": "독일어", "pt": "포르투갈어", "zh": "중국어", "ru": "러시아어"}


class NotesError(RuntimeError):
    pass


@dataclass
class Segment:
    start: float
    end: float
    text: str
    ko: str = ""


def fmt_ts(t: float) -> str:
    """1:02:03 for long lessons, 4:05 otherwise."""
    t = int(max(0.0, t))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def tango_vocabulary(library: FigureLibrary) -> str:
    """A hint for the speech model so 'sacada' doesn't come out as 'sagada'."""
    names = sorted({f.name_es for f in library.figures.values()})
    extra = ["abrazo", "eje", "pivot", "disociación", "cruzada", "milonga", "tango", "vals", "compás",
             "líder", "follower", "sandwichito", "parada", "adorno", "embellishment"]
    return "Clase de tango. " + ", ".join(names + extra) + "."


# ---------------------------------------------------------------- 1. transcribe

def _whisper(model: str):
    from faster_whisper import WhisperModel

    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return WhisperModel(model, device="cuda", compute_type="float16")
    except Exception:  # no CUDA build / driver: CPU it is
        pass
    return WhisperModel(model, device="cpu", compute_type="int8")


def transcribe(media: Path, cache_dir: Path, *, model: str = "turbo", language: str | None = None,
               vocabulary: str = "", progress: Callable[[float, float], None] | None = None,
               whisper=None) -> tuple[list[Segment], str, float]:
    """Speech → segments with absolute times. Returns (segments, language, duration_s).

    `whisper` is a ready WhisperModel-like object (tests pass a fake).
    """
    try:
        from faster_whisper import decode_audio
    except ImportError as e:
        raise NotesError("음성 인식 패키지가 없습니다. `uv sync --extra notes`로 설치하세요.") from e

    audio = decode_audio(str(media), sampling_rate=SAMPLE_RATE)
    duration = len(audio) / SAMPLE_RATE
    if duration < 1:
        raise NotesError("영상에서 소리를 찾지 못했습니다.")
    cache = cache_dir / f"whisper-{model}-{language or 'auto'}"
    cache.mkdir(parents=True, exist_ok=True)
    n_chunks = int(-(-duration // CHUNK_S))
    segments: list[Segment] = []
    lang = language
    for i in range(n_chunks):
        offset = i * CHUNK_S
        part = cache / f"{i:03d}.json"
        if part.exists():
            data = json.loads(part.read_text(encoding="utf-8"))
        else:
            if whisper is None:
                whisper = _whisper(model)
            chunk = audio[int(offset * SAMPLE_RATE): int(min(duration, offset + CHUNK_S) * SAMPLE_RATE)]
            # the last line said is the best hint for how the next chunk starts
            prompt = " ".join(filter(None, [vocabulary, segments[-1].text if segments else ""]))
            found, info = whisper.transcribe(
                chunk, language=lang, initial_prompt=prompt or None, vad_filter=True, beam_size=5,
                # off: on long audio it lets one misheard line repeat for minutes
                condition_on_previous_text=False,
            )
            data = {"language": None, "segments": []}
            for s in found:
                text = s.text.strip()
                if text:
                    data["segments"].append({"start": round(offset + s.start, 2), "end": round(offset + s.end, 2),
                                             "text": text})
                if progress:
                    progress(offset + s.end, duration)
            if data["segments"]:
                data["language"] = info.language
            part.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        # music-only chunks guess languages at random: fix it from the first chunk with speech
        if lang is None and data.get("language"):
            lang = data["language"]
        segments += [Segment(**s) for s in data["segments"]]
        if progress:
            progress(min(duration, offset + CHUNK_S), duration)
    return _drop_repeats(segments), lang or "unknown", duration


def _drop_repeats(segments: list[Segment], limit: int = 3) -> list[Segment]:
    """Whisper on music sometimes prints the same line over and over: keep the first few."""
    out: list[Segment] = []
    run = 0
    for s in segments:
        run = run + 1 if out and s.text == out[-1].text else 0
        if run < limit:
            out.append(s)
    return out


# ---------------------------------------------------------------- Claude

def make_client():
    try:
        import anthropic
    except ImportError as e:
        raise NotesError("anthropic 패키지가 없습니다. `uv sync --extra notes`로 설치하세요.") from e
    return anthropic.Anthropic()


def ask_json(client, *, system: str, prompt: str, schema: dict, effort: str, model: str = MODEL,
             max_tokens: int = 32000) -> dict:
    """One structured-output request, streamed (long input and output)."""
    with client.beta.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        # if a safety classifier declines (a lesson can mention injuries etc.), retry on a fallback model
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise NotesError("Claude가 이 내용의 처리를 거절했습니다.")
    if msg.stop_reason == "max_tokens":
        raise NotesError("응답이 너무 길어 잘렸습니다.")
    text = next((b.text for b in msg.content if b.type == "text"), "")
    return json.loads(text)


def _term_hint(library: FigureLibrary) -> str:
    return ", ".join(f"{f.name_es} → {f.name_ko}" for f in library.figures.values())


# ---------------------------------------------------------------- 2. translate

TRANSLATE_SCHEMA = {
    "type": "object",
    "properties": {"lines": {"type": "array", "items": {
        "type": "object",
        "properties": {"i": {"type": "integer"}, "ko": {"type": "string"}},
        "required": ["i", "ko"], "additionalProperties": False}}},
    "required": ["lines"], "additionalProperties": False,
}


def _translate_system(language: str, library: FigureLibrary) -> str:
    src = LANGUAGE_NAMES.get(language, language)
    return f"""탱고 개인 레슨을 음성 인식으로 받아 적은 {src} 자막을 한국어로 옮깁니다. 한국 탱고 학생이 영상과 함께 읽습니다.

- 줄마다 따로 옮기고 번호(i)를 그대로 돌려주세요. 줄을 합치거나 빼지 마세요.
- 말하는 그대로의 자연스러운 구어체로. 강사가 학생에게 하는 말이니 존댓말("~하세요")로 옮기세요.
- 탱고 용어는 한국 탱고 커뮤니티에서 쓰는 표기로: {_term_hint(library)}. 그 밖의 용어도 소리 나는 대로 한글로 쓰고 필요하면 괄호에 원어를 붙이세요 (예: 에헤(eje)).
- 음성 인식 오류로 보이는 단어는 앞뒤 문맥으로 바로잡아 옮기세요. 음악 가사나 의미 없는 소리는 "(음악)"처럼 짧게 표시하세요."""


def translate(segments: list[Segment], language: str, library: FigureLibrary, client, *,
              save: Callable[[], None] | None = None, batch: int = 80, workers: int = 4, model: str = MODEL,
              progress: Callable[[int, int], None] | None = None) -> None:
    """Fill in segment.ko in place. Lines that already have it are skipped (resume)."""
    todo = [i for i, s in enumerate(segments) if not s.ko]
    if not todo:
        return
    system = _translate_system(language, library)
    batches = [todo[k:k + batch] for k in range(0, len(todo), batch)]

    def run(ids: list[int]) -> tuple[list[int], dict]:
        # a few lines either side so a sentence split across the batch edge still reads right
        before = [segments[j].text for j in range(max(0, ids[0] - 3), ids[0])]
        lines = "\n".join(json.dumps({"i": i, "text": segments[i].text}, ensure_ascii=False) for i in ids)
        prompt = (("앞 문맥 (옮기지 마세요): " + " / ".join(before) + "\n\n") if before else "") + "옮길 줄:\n" + lines
        return ids, ask_json(client, system=system, prompt=prompt, schema=TRANSLATE_SCHEMA, effort="low",
                             model=model, max_tokens=16000)

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(run, b) for b in batches]):
            ids, result = fut.result()
            wanted = set(ids)
            for line in result.get("lines", []):
                if line.get("i") in wanted:
                    segments[line["i"]].ko = str(line.get("ko", "")).strip()
            done += len(ids)
            if save:
                save()
            if progress:
                progress(done, len(todo))


# ---------------------------------------------------------------- 3. key points

def _notes_schema(figure_keys: list[str]) -> dict:
    t = {"type": "number"}
    s = {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "title": s,
            "summary": s,
            "key_points": {"type": "array", "items": {
                "type": "object",
                "properties": {"t": t, "point": s, "detail": s, "kind": {"type": "string", "enum": list(KINDS)},
                               "role": {"type": "string", "enum": list(ROLES)}, "quote": s},
                "required": ["t", "point", "detail", "kind", "role", "quote"], "additionalProperties": False}},
            "sections": {"type": "array", "items": {
                "type": "object",
                "properties": {"start": t, "end": t, "title": s, "summary": s,
                               "figures": {"type": "array", "items": {"type": "string", "enum": figure_keys}}},
                "required": ["start", "end", "title", "summary", "figures"], "additionalProperties": False}},
            "practice": {"type": "array", "items": {
                "type": "object", "properties": {"text": s, "t": t},
                "required": ["text", "t"], "additionalProperties": False}},
            "glossary": {"type": "array", "items": {
                "type": "object", "properties": {"term": s, "meaning": s},
                "required": ["term", "meaning"], "additionalProperties": False}},
        },
        "required": ["title", "summary", "key_points", "sections", "practice", "glossary"],
        "additionalProperties": False,
    }


def _notes_system(library: FigureLibrary, role: str) -> str:
    who = {"leader": "리더", "follower": "팔로워"}.get(role)
    focus = f"\n- 이 학생은 {who}입니다. {who}에게 해당하는 내용을 우선하되, 상대 역할 설명도 이해에 필요하면 넣으세요." if who else ""
    return f"""당신은 탱고 강사의 조교입니다. 1~2시간짜리 탱고 개인 레슨의 음성 인식 자막을 읽고, 학생이 다시 영상을 보며 복습할 수 있는 한국어 학습 노트를 만듭니다.

자막은 "[시:분:초 | 초] 내용" 형식이고 기계로 받아 적어 오류가 있습니다. 음악만 나오는 구간의 엉뚱한 문장은 무시하세요.

만들 것:
- title: 이 레슨을 한 줄로 (예: "오초 아뜨라스의 피벗과 축 유지")
- summary: 무엇을 배웠고 무엇이 가장 중요했는지 3~5문장
- key_points: 복습할 가치가 있는 핵심. 1시간에 8~15개 정도, 중요한 순서가 아니라 시간 순서로.
  개인 레슨이므로 **이 학생에게 준 교정**(kind: correction)이 가장 값집니다. 놓치지 마세요.
  t는 그 말을 시작한 시점(초). point는 한 문장 요점, detail은 강사가 설명한 이유·방법을 2~3문장으로,
  quote는 근거가 된 원문 그대로 짧게(원래 언어). role은 리더/팔로워/공통 중 누구에게 하는 말인지.
- sections: 레슨 흐름을 5~12개 구간으로 (워밍업, 설명, 시연, 연습, 피드백 등). start/end는 초.
  figures에는 그 구간에서 다룬 피구라를 목록의 키로만 넣고, 목록에 없으면 비워 두세요.
- practice: 집에서 혼자 또는 파트너와 연습할 것 3~8개. 레슨에서 실제로 한 연습·지적에서 뽑고, t는 관련 설명 시점.
- glossary: 레슨에 나온 탱고 용어와 강사가 쓴 독특한 표현을 짧게 풀이 (최대 12개).

규칙:
- 자막에 실제로 있는 내용만. 일반 탱고 상식을 지어 넣지 마세요.
- 모든 글은 한국어. 탱고 용어는 한국 탱고 커뮤니티 표기로 ({_term_hint(library)}), 처음 나올 때 괄호에 원어.
- 피구라 키 목록: {", ".join(f"{k}({f.name_ko})" for k, f in library.figures.items())}{focus}"""


def _transcript_text(segments: list[Segment]) -> str:
    return "\n".join(f"[{fmt_ts(s.start)} | {s.start:.0f}] {s.text}" for s in segments)


def key_points(segments: list[Segment], duration: float, library: FigureLibrary, client, *,
               role: str = "all", model: str = MODEL) -> dict:
    if not segments:
        raise NotesError("받아 적은 말이 없어 핵심을 정리할 수 없습니다.")
    keys = list(library.figures)
    raw = ask_json(client, system=_notes_system(library, role), schema=_notes_schema(keys), effort="high",
                   model=model, max_tokens=64000,
                   prompt=f"레슨 길이 {fmt_ts(duration)}. 자막:\n\n{_transcript_text(segments)}")
    return clean_notes(raw, duration, keys)


def clean_notes(raw: dict, duration: float, figure_keys: list[str]) -> dict:
    """Clamp times into the video, sort, drop what doesn't fit the schema's intent."""
    def t(x) -> float:
        try:
            return round(min(max(float(x), 0.0), duration), 1)
        except (TypeError, ValueError):
            return 0.0

    known = set(figure_keys)
    points = [{**p, "t": t(p.get("t")), "kind": p.get("kind") if p.get("kind") in KINDS else "other",
               "role": p.get("role") if p.get("role") in ROLES else "all"}
              for p in raw.get("key_points", []) if str(p.get("point", "")).strip()]
    sections = []
    for s in raw.get("sections", []):
        start, end = t(s.get("start")), t(s.get("end"))
        sections.append({**s, "start": start, "end": max(start, end),
                         "figures": [k for k in s.get("figures", []) if k in known]})
    return {
        "title": str(raw.get("title", "")).strip(),
        "summary": str(raw.get("summary", "")).strip(),
        "key_points": sorted(points, key=lambda p: p["t"]),
        "sections": sorted(sections, key=lambda s: s["start"]),
        "practice": [{"text": str(p["text"]).strip(), "t": t(p.get("t"))}
                     for p in raw.get("practice", []) if str(p.get("text", "")).strip()],
        "glossary": [g for g in raw.get("glossary", []) if str(g.get("term", "")).strip()],
    }


# ---------------------------------------------------------------- files

def _srt_time(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(segments: list[Segment], path: Path, *, korean: bool = False) -> None:
    blocks = []
    for n, s in enumerate((s for s in segments if not korean or s.ko), 1):
        blocks.append(f"{n}\n{_srt_time(s.start)} --> {_srt_time(s.end)}\n{s.ko if korean else s.text}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")


def save_transcript(out: Path, segments: list[Segment], language: str, duration: float, model: str) -> None:
    data = {"language": language, "duration_s": round(duration, 2), "whisper_model": model,
            "segments": [asdict(s) for s in segments]}
    tmp = out / "transcript.json.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out / "transcript.json")


def load_transcript(out: Path) -> tuple[list[Segment], str, float, str] | None:
    path = out / "transcript.json"
    if not path.exists():
        return None
    d = json.loads(path.read_text(encoding="utf-8"))
    return [Segment(**s) for s in d["segments"]], d["language"], d["duration_s"], d.get("whisper_model", "")


def practice_ids(lesson_id: str, practice: list[dict]) -> list[dict]:
    """Checklist ids for the web app's records. Per lesson: the same advice in two lessons is two items."""
    return [{**p, "id": f"notes:{lesson_id}:{i}"} for i, p in enumerate(practice)]


def notes_markdown(notes: dict, library: FigureLibrary) -> str:
    lines = [f"# {notes.get('title') or notes.get('source_title') or '레슨 노트'}", ""]
    if notes.get("source_url"):
        lines += [f"영상: {notes['source_url']}", ""]
    if notes.get("summary"):
        lines += [notes["summary"], ""]
    if notes.get("key_points"):
        lines += ["## 핵심", ""]
        for p in notes["key_points"]:
            who = {"leader": " · 리더", "follower": " · 팔로워"}.get(p["role"], "")
            lines.append(f"- **[{fmt_ts(p['t'])}] {p['point']}** ({KINDS[p['kind']]}{who})")
            if p.get("detail"):
                lines.append(f"  {p['detail']}")
            if p.get("quote"):
                lines.append(f"  > {p['quote']}")
        lines.append("")
    if notes.get("sections"):
        lines += ["## 레슨 흐름", ""]
        for s in notes["sections"]:
            figs = ", ".join(library.figures[k].name_ko for k in s["figures"] if k in library.figures)
            lines.append(f"- **{fmt_ts(s['start'])}–{fmt_ts(s['end'])} {s['title']}**" + (f" ({figs})" if figs else ""))
            if s.get("summary"):
                lines.append(f"  {s['summary']}")
        lines.append("")
    if notes.get("practice"):
        lines += ["## 연습할 것", ""] + [f"- [ ] {p['text']} ({fmt_ts(p['t'])})" for p in notes["practice"]] + [""]
    if notes.get("glossary"):
        lines += ["## 용어", ""] + [f"- **{g['term']}** — {g['meaning']}" for g in notes["glossary"]] + [""]
    return "\n".join(lines)


def video_ref(out: Path, video: Path | None) -> str | None:
    """How notes.json points at the video: relative if it sits in the lesson folder, else absolute."""
    if video is None:
        return None
    video = video.resolve()
    try:
        return video.relative_to(out.resolve()).as_posix()
    except ValueError:
        return str(video)
