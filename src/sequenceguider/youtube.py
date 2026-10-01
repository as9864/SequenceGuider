"""YouTube (or any yt-dlp supported site) input.

Two jobs:
1. fetch the video to a local file (video-only, <=720p is plenty for pose)
2. read the figure ORDER from the video's own metadata. Tango class videos
   very often list their content as chapters or as timestamp lines in the
   description ("0:45 오초 아뜨라스"); matched against the figure library,
   that gives the sequence AND each figure's start/end time — far better
   than splitting by step counts.

Only for videos you own or are allowed to download, analysed locally for
personal study (YouTube's Terms of Service restrict downloading).
"""

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sequenceguider.figures import Figure, FigureLibrary, SequenceItem, parse_time

URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def is_url(text: str) -> bool:
    return bool(URL_RE.match(text.strip()))


@dataclass
class Chapter:
    start_s: float
    end_s: float | None
    title: str


@dataclass
class VideoSource:
    url: str
    id: str
    title: str
    duration_s: float | None
    description: str = ""
    chapters: list[Chapter] = field(default_factory=list)
    path: str | None = None  # local file once downloaded

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "VideoSource":
        d = json.loads(path.read_text(encoding="utf-8"))
        d["chapters"] = [Chapter(**c) for c in d.get("chapters", [])]
        return cls(**d)


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


def fetch_info(url: str) -> VideoSource:
    """Metadata only — title, chapters, description. No download."""
    from yt_dlp import YoutubeDL

    with YoutubeDL(_ydl_options(None, 720)) as ydl:
        return _from_info(url, ydl.extract_info(url, download=False))


def fetch_video(url: str, dest_dir: Path, *, max_height: int = 720) -> VideoSource:
    """Download to dest_dir (reused if already there) and return its metadata.

    source.json next to the file lets a re-run skip the network entirely.
    """
    cached = dest_dir / "source.json"
    if cached.exists():
        src = VideoSource.load(cached)
        if src.url == url or src.id in url:
            if src.path and Path(src.path).exists():
                return src

    from yt_dlp import YoutubeDL

    dest_dir.mkdir(parents=True, exist_ok=True)
    with YoutubeDL(_ydl_options(dest_dir, max_height)) as ydl:
        info = ydl.extract_info(url, download=True)
        downloads = info.get("requested_downloads") or []
        path = downloads[0].get("filepath") if downloads else ydl.prepare_filename(info)
    src = _from_info(url, info)
    src.path = str(path)
    src.save(cached)
    return src


def fetch_audio(url: str, dest_dir: Path) -> VideoSource:
    """Download only the sound track (for `notes`: speech-to-text needs no picture).

    Same caching as fetch_video, in its own audio.json so the two don't overwrite
    each other's file. m4a first: a single stream, so no ffmpeg merge.
    """
    cached = dest_dir / "audio.json"
    if cached.exists():
        src = VideoSource.load(cached)
        if (src.url == url or src.id in url) and src.path and Path(src.path).exists():
            return src

    from yt_dlp import YoutubeDL

    dest_dir.mkdir(parents=True, exist_ok=True)
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "format": "bestaudio[ext=m4a]/bestaudio/b",
            "outtmpl": str(dest_dir / "%(id)s.audio.%(ext)s")}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        downloads = info.get("requested_downloads") or []
        path = downloads[0].get("filepath") if downloads else ydl.prepare_filename(info)
    src = _from_info(url, info)
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
    source: str  # "chapters" | "description" | "none"
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
            mins, secs = divmod(int(round(m.chapter.start_s)), 60)
            parts.append(f"{m.figure.name_ko}{count} @{mins}:{secs:02d}")
        return ", ".join(parts)


def sequence_from_metadata(src: VideoSource, library: FigureLibrary) -> MetadataSequence:
    chapters, origin = src.chapters, "chapters"
    if not chapters:
        chapters, origin = chapters_from_description(src.description), "description"
    if not chapters:
        return MetadataSequence("none", [])
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
