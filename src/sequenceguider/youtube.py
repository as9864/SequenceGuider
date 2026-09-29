"""YouTube (or any yt-dlp supported site) input.

Two jobs:
1. fetch the video to a local file (video-only, <=720p is plenty for pose)
2. read the figure ORDER from the video's own metadata. Tango class videos
   very often list their content as chapters or as timestamp lines in the
   description ("0:45 오초 아뜨라스"); matched against the figure library,
   that gives the sequence AND each figure's start/end time — far better
   than splitting by step counts. Without those, the captions (the teacher
   saying "이제 오초 아뜨라스 해 볼게요" at 1:05) give a rougher version of
   the same thing — which is also what YouTube's own AI summary is built from.

Only for videos you own or are allowed to download, analysed locally for
personal study (YouTube's Terms of Service restrict downloading).
"""

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sequenceguider.figures import Figure, FigureLibrary, Mention, SequenceItem, normalize, parse_time

URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def is_url(text: str) -> bool:
    return bool(URL_RE.match(text.strip()))


@dataclass
class Chapter:
    start_s: float | None  # None: order known, time not (pasted text without timestamps)
    end_s: float | None
    title: str


@dataclass
class Cue:
    """One caption line."""
    start_s: float
    end_s: float
    text: str


@dataclass
class VideoSource:
    url: str
    id: str
    title: str
    duration_s: float | None
    description: str = ""
    chapters: list[Chapter] = field(default_factory=list)
    path: str | None = None  # local file once downloaded
    # captions, fetched only when chapters/description give no order;
    # None = not fetched (yet), [] = fetched but there are none
    transcript: list[Cue] | None = None
    transcript_lang: str = ""

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "VideoSource":
        d = json.loads(path.read_text(encoding="utf-8"))
        d["chapters"] = [Chapter(**c) for c in d.get("chapters", [])]
        if d.get("transcript") is not None:
            d["transcript"] = [Cue(**c) for c in d["transcript"]]
        return cls(**d)

    @property
    def needs_transcript(self) -> bool:
        return not self.chapters and not chapters_from_description(self.description)


def _from_info(url: str, info: dict) -> VideoSource:
    chapters = [
        Chapter(float(c.get("start_time") or 0.0), c.get("end_time"), str(c.get("title") or ""))
        for c in (info.get("chapters") or [])
    ]
    return VideoSource(
        url=info.get("webpage_url") or url,
        id=str(info.get("id") or "video"),
        title=str(info.get("title") or ""),
        duration_s=info.get("duration"),
        description=str(info.get("description") or ""),
        chapters=chapters,
    )


def _ydl_options(dest_dir: Path | None, max_height: int) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # video only (no audio merge -> no ffmpeg needed); prefer H.264 mp4, which OpenCV reads everywhere
        "format": f"bv*[height<={max_height}][vcodec^=avc1]/bv*[height<={max_height}]/b[height<={max_height}]/b",
    }
    if dest_dir is not None:
        opts["outtmpl"] = str(dest_dir / "%(id)s.%(ext)s")
    return opts


def _pick_caption(info: dict) -> tuple[str, dict] | None:
    """(language, format) of the captions to read figure names from.

    Uploaded subtitles first (original language, then Korean/Spanish/English),
    else the ORIGINAL-language auto captions ("ko-orig"). Never a machine
    translation: translating "오초 아뜨라스" or "ocho atrás" mangles exactly
    the words we are looking for.
    """
    def best_format(tracks: list[dict]) -> dict | None:
        by_ext = {t.get("ext"): t for t in tracks if t.get("url")}
        return next((by_ext[e] for e in ("json3", "vtt") if e in by_ext), None)

    orig = str(info.get("language") or "")
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    for want in (orig, "ko", "es", "en"):
        for lang, tracks in manual.items():
            if want and (lang == want or lang.startswith(want + "-")) and (fmt := best_format(tracks)):
                return lang, fmt
    for lang, tracks in manual.items():
        if fmt := best_format(tracks):
            return lang, fmt
    auto = info.get("automatic_captions") or {}
    for lang, tracks in auto.items():
        if lang.endswith("-orig") and (fmt := best_format(tracks)):
            return lang.removesuffix("-orig"), fmt
    if orig and orig in auto and (fmt := best_format(auto[orig])):
        return orig, fmt
    return None


def parse_json3(raw: str) -> list[Cue]:
    cues = []
    for ev in json.loads(raw).get("events") or []:
        text = "".join(seg.get("utf8", "") for seg in ev.get("segs") or []).replace("\n", " ").strip()
        if not text or "tStartMs" not in ev:
            continue
        start = ev["tStartMs"] / 1000
        cues.append(Cue(start, start + ev.get("dDurationMs", 0) / 1000, text))
    return cues


_VTT_TIME = re.compile(r"((?:\d+:)?\d{1,2}:\d{2}\.\d{3})\s*-->\s*((?:\d+:)?\d{1,2}:\d{2}\.\d{3})")


def _vtt_seconds(t: str) -> float:
    *hm, sec = t.split(":")
    return sum(int(x) * 60 ** (len(hm) - i) for i, x in enumerate(hm)) + float(sec)


def parse_vtt(raw: str) -> list[Cue]:
    cues = []
    for block in re.split(r"\n\s*\n", raw.replace("\r", "")):
        lines = block.strip().splitlines()
        for i, line in enumerate(lines):
            if m := _VTT_TIME.search(line):
                text = " ".join(re.sub(r"<[^>]+>", "", t).strip() for t in lines[i + 1 :]).strip()
                if text:
                    cues.append(Cue(_vtt_seconds(m.group(1)), _vtt_seconds(m.group(2)), text))
                break
    # auto captions roll: each line is repeated in the next cue — keep the first
    return [c for i, c in enumerate(cues) if i == 0 or c.text != cues[i - 1].text]


def _fetch_transcript(ydl, info: dict) -> tuple[list[Cue], str]:
    """Captions of the video, or ([], "") when there are none or they can't
    be fetched — a missing transcript is never an error, just no order."""
    picked = _pick_caption(info)
    if picked is None:
        return [], ""
    lang, fmt = picked
    try:
        raw = ydl.urlopen(fmt["url"]).read().decode("utf-8", errors="replace")
        cues = parse_json3(raw) if fmt.get("ext") == "json3" else parse_vtt(raw)
    except Exception:  # network / format trouble: fall back to "no captions"
        return [], ""
    return cues, lang


def _with_transcript(ydl, src: VideoSource, info: dict) -> VideoSource:
    if src.needs_transcript:
        src.transcript, src.transcript_lang = _fetch_transcript(ydl, info)
    else:
        src.transcript = []
    return src


def fetch_info(url: str) -> VideoSource:
    """Metadata only — title, chapters, description (captions if those have
    no order). No video download."""
    from yt_dlp import YoutubeDL

    with YoutubeDL(_ydl_options(None, 720)) as ydl:
        info = ydl.extract_info(url, download=False)
        return _with_transcript(ydl, _from_info(url, info), info)


def fetch_video(url: str, dest_dir: Path, *, max_height: int = 720) -> VideoSource:
    """Download to dest_dir (reused if already there) and return its metadata.

    source.json next to the file lets a re-run skip the network entirely.
    """
    cached = dest_dir / "source.json"
    if cached.exists():
        src = VideoSource.load(cached)
        if src.url == url or src.id in url:
            # a source.json from before captions were read has transcript None:
            # go on — yt-dlp skips the download when the file is already there
            if src.path and Path(src.path).exists() and (src.transcript is not None or not src.needs_transcript):
                return src

    from yt_dlp import YoutubeDL

    dest_dir.mkdir(parents=True, exist_ok=True)
    with YoutubeDL(_ydl_options(dest_dir, max_height)) as ydl:
        info = ydl.extract_info(url, download=True)
        downloads = info.get("requested_downloads") or []
        path = downloads[0].get("filepath") if downloads else ydl.prepare_filename(info)
        src = _with_transcript(ydl, _from_info(url, info), info)
    src.path = str(path)
    src.save(cached)
    return src


# "0:45 오초 아뜨라스", "[1:02:03] - 볼레오", "오초 아뜨라스 (2:10)", "3. 1:20 사까다"
_TIMESTAMP = re.compile(r"(?<![\d:])((?:\d{1,2}:)?\d{1,2}:\d{2})(?![\d:])")
_COUNT_IN_TITLE = re.compile(r"(?:[x×*]\s*(\d+)|(\d+)\s*(?:회|번|times))", re.IGNORECASE)


def chapters_from_description(description: str) -> list[Chapter]:
    """Timestamp lines in a description, as chapters. Needs at least two such
    lines — one stray time mention isn't a table of contents."""
    found: list[tuple[float, str]] = []
    for line in description.splitlines():
        m = _TIMESTAMP.search(line)
        if not m:
            continue
        title = (line[: m.start()] + " " + line[m.end() :]).strip()
        title = re.sub(r"^[\s\-–—:.)\]\[(#\d.]*(?=\D)", "", title).strip(" -–—:|[]()")
        if title:
            found.append((parse_time(m.group(1)), title))
    if len(found) < 2:
        return []
    found.sort(key=lambda x: x[0])
    return [
        Chapter(start, found[i + 1][0] if i + 1 < len(found) else None, title)
        for i, (start, title) in enumerate(found)
    ]


@dataclass
class ChapterMatch:
    chapter: Chapter
    figure: Figure | None
    count: int = 1


@dataclass
class MetadataSequence:
    source: str  # "chapters" | "description" | "transcript" | "text" | "none"
    matches: list[ChapterMatch]

    @property
    def items(self) -> list[SequenceItem]:
        return [
            SequenceItem(figure=m.figure, count=m.count, anchor_s=m.chapter.start_s, end_s=m.chapter.end_s,
                         raw=m.chapter.title)
            for m in self.matches
            if m.figure is not None
        ]

    def as_sequence_text(self) -> str:
        """Editable `-s` string: users fix one wrong chapter instead of retyping all."""
        parts = []
        for m in self.matches:
            if m.figure is None:
                continue
            count = f" x{m.count}" if m.count > 1 else ""
            at = ""
            if m.chapter.start_s is not None:
                mins, secs = divmod(int(round(m.chapter.start_s)), 60)
                at = f" @{mins}:{secs:02d}"
            parts.append(f"{m.figure.name_ko}{count}{at}")
        return ", ".join(parts)


# Aliases that are everyday words in speech ("걷기", "stop", "턴") — fine in a
# typed sequence or a chapter title, pure noise in a transcript.
GENERIC_SPOKEN = frozenset(normalize(w) for w in (
    "walk", "걷기", "워크", "basic", "베이직", "기본스텝", "기본 8", "cross", "turn", "턴",
    "stop", "정지", "sweep", "스윕", "hook", "rebound", "리바운드",
))


REFERENCE_GAP_S = 15.0  # back to the previous figure this soon: the other name was only a reference


def _cue_excerpt(text: str, start: int, end: int, width: int = 36) -> str:
    """The mention with a little context, for showing where it came from."""
    line_a = text.rfind("\n", 0, start) + 1
    line_b = text.find("\n", end)
    line_b = len(text) if line_b < 0 else line_b
    a, b = max(line_a, start - width // 3), min(line_b, end + width)
    return ("…" if a > line_a else "") + text[a:b].strip() + ("…" if b < line_b else "")


def _runs(found: list[Mention]) -> list[list[Mention]]:
    """Consecutive mentions of one figure -> one run."""
    runs: list[list[Mention]] = []
    for m in found:
        if runs and runs[-1][0].figure.key == m.figure.key:
            runs[-1].append(m)
        else:
            runs.append([m])
    return runs


def _drop_references(runs: list[list[Mention]], is_reference) -> list[list[Mention]]:
    """A, B, A -> A when the lone B mention is only a passing reference
    ("아까 살리다에서처럼 골반을…") — `is_reference(b, a_before, a_after)`."""
    changed = True
    while changed:
        changed = False
        for i in range(1, len(runs) - 1):
            if (len(runs[i]) == 1 and runs[i - 1][0].figure.key == runs[i + 1][0].figure.key
                    and is_reference(runs[i][0], runs[i - 1][-1], runs[i + 1][0])):
                runs[i - 1] = runs[i - 1] + runs[i + 1]
                del runs[i : i + 2]
                changed = True
                break
    return runs


def matches_from_transcript(cues: list[Cue], library: FigureLibrary,
                            duration_s: float | None) -> list[ChapterMatch]:
    """Figure order and rough start times from what the teacher says.

    Captions are chopped mid-phrase ("오초" | "아뜨라스"), so the cues are
    scanned as one text. Then:
    - consecutive mentions of one figure are one section, starting at the
      first mention (teachers name the figure, then explain and show it);
    - a lone mention of another figure that the teacher leaves again within
      REFERENCE_GAP_S ("살리다에서처럼 골반을… 볼레오는") is a reference, not
      a new section: A, B, A -> A. Named once and then 20 s of showing it
      without talking is still a section;
    - the same figure coming back later after others is a new section.
    Less exact than chapters: times are when it was SAID, so a section may
    start a little before the dancing does.
    """
    if not cues:
        return []

    def said_at(m: Mention) -> float:
        return cues[owner[m.start]].start_s

    text, owner = "", []  # owner[i] = cue index of text[i]
    for i, cue in enumerate(cues):
        piece = cue.text.strip() + " "
        text += piece
        owner.extend([i] * len(piece))
    runs = _drop_references(_runs(library.mentions(text, skip=GENERIC_SPOKEN)),
                            lambda b, _, a_after: said_at(a_after) - said_at(b) < REFERENCE_GAP_S)
    starts = [said_at(run[0]) for run in runs]
    out = []
    for i, run in enumerate(runs):
        end = starts[i + 1] if i + 1 < len(runs) else duration_s
        title = _cue_excerpt(text, run[0].start, run[0].end)
        out.append(ChapterMatch(Chapter(starts[i], end, title), run[0].figure))
    return out


_SENTENCE_END = re.compile(r"[.!?。]+(?=\s)|\n")
_KO_NUMBERS = {"두": 2, "세": 3, "석": 3, "네": 4, "넉": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8}
# right after the name, maybe past a particle: "x3", "3회", "(3번)", "를 세 번" — not "두 번째"
_COUNT_AFTER = re.compile(r"(?:을|를|은|는|이|가|도)?\s*\(?\s*(?:[x×*]\s*(\d+)|(\d+|"
                          + "|".join(_KO_NUMBERS) + r")\s*(?:회|번|times)(?!\s*째))", re.IGNORECASE)


def matches_from_text(text: str, library: FigureLibrary) -> list[ChapterMatch]:
    """Figure order from free text — typically YouTube's AI summary of the
    video, copied and pasted ("영상은 살리다로 시작해 오초 아뜨라스를 세 번…").

    - figures in the order they are named; consecutive mentions are one item;
    - a lone mention of another figure later in a sentence about the figure
      around it is a reference ("오초 아뜨라스는 살리다처럼…"), not an item;
    - "x3" / "3회" / "3번" right after the name is the repeat count;
    - a timestamp on the line ("1:05 오초 아뜨라스", "(2:10)") pins the start
      of the first figure named there, as long as times keep going forward;
    - a closing recap that repeats the whole order is not counted twice.
    """
    found = library.mentions(text, skip=GENERIC_SPOKEN)
    if not found:
        return []
    breaks = [m.end() for m in _SENTENCE_END.finditer(text)]

    def sentence(pos: int) -> int:
        return sum(b <= pos for b in breaks)

    first_in_sentence = {sentence(m.start): m.start for m in reversed(found)}

    def is_reference(b: Mention, a_before: Mention, a_after: Mention) -> bool:
        # the first figure of a sentence is its topic, a later one a comparison
        return (first_in_sentence[sentence(b.start)] != b.start
                and sentence(b.start) in (sentence(a_before.start), sentence(a_after.start)))

    runs = _drop_references(_runs(found), is_reference)

    line_starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
    used_lines: set[int] = set()
    last_t = -1.0
    out: list[ChapterMatch] = []
    for run in runs:
        first = run[0]
        line_no = sum(ls <= first.start for ls in line_starts) - 1
        line_end = text.find("\n", first.start)
        line = text[line_starts[line_no] : line_end if line_end >= 0 else len(text)]
        at = None
        if line_no not in used_lines and (t := _TIMESTAMP.search(line)):
            if parse_time(t.group(1)) > last_t:
                at = last_t = parse_time(t.group(1))
                used_lines.add(line_no)
        count = 1
        for m in run:
            if c := _COUNT_AFTER.match(text, m.end):
                n = c.group(1) or c.group(2)
                count = max(1, int(n) if n.isdigit() else _KO_NUMBERS[n])
                break
        out.append(ChapterMatch(Chapter(at, None, _cue_excerpt(text, first.start, first.end)), first.figure, count))
    # "...요약: 살리다 → 오초 아뜨라스 → 볼레오" after the detailed walk-through
    keys = [m.figure.key for m in out]
    half = len(keys) // 2
    if half >= 3 and len(keys) % 2 == 0 and keys[:half] == keys[half:]:
        for early, late in zip(out[:half], out[half:]):
            if early.chapter.start_s is None:
                early.chapter.start_s = late.chapter.start_s
            early.count = max(early.count, late.count)
        out = out[:half]
        last_t = -1.0
        for m in out:  # anchors must keep going forward
            if m.chapter.start_s is not None:
                if m.chapter.start_s <= last_t:
                    m.chapter.start_s = None
                else:
                    last_t = m.chapter.start_s
    return out


def sequence_from_metadata(src: VideoSource, library: FigureLibrary) -> MetadataSequence:
    chapters, origin = src.chapters, "chapters"
    if not chapters:
        chapters, origin = chapters_from_description(src.description), "description"
    if not chapters:
        spoken = matches_from_transcript(src.transcript or [], library, src.duration_s)
        return MetadataSequence("transcript", spoken) if spoken else MetadataSequence("none", [])
    # close open-ended chapters at the next chapter / video end
    for i, ch in enumerate(chapters):
        if ch.end_s is None:
            ch.end_s = chapters[i + 1].start_s if i + 1 < len(chapters) else src.duration_s
    matches = []
    for ch in chapters:
        m = _COUNT_IN_TITLE.search(ch.title)
        count = int(m.group(1) or m.group(2)) if m else 1
        matches.append(ChapterMatch(ch, library.find_in_text(ch.title), max(1, count)))
    return MetadataSequence(origin, matches)
