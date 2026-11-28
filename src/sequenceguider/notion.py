"""Import notes written in Notion: the "Markdown & CSV" export of a database (with subpages) → lessons.

Each row of the database (one page) becomes one lesson folder in the home folder, next to the analysed
ones, so the library, categories and practice records work on them the same way:

    <home>/<title>_sequenceguider/
        lesson.json        title, media (the YouTube links in 영상주소 and in the text), categories,
                           and "notion": where it came from (page id, kind, date, people, keywords, summary, pages)
        notion/<n>.md      the page and each of its subpages, links pointed at what the import copied
        notion/files/...   images and attachments the pages use

The export's quirks this handles: an outer zip holding `...-Part-1.zip` parts; Korean file names stored
without the UTF-8 flag (decoded as cp437 by zipfile); a 32-hex page id at the end of every file and
folder name (with or without it on the subpage folder); the CSV's UTF-8 BOM; URL-encoded relative links.

Importing the same export again (or a newer one) is safe: a lesson is found again by its Notion page id,
its pages are rewritten only if they changed, links already in the lesson stay, and categories the user
changed in the app are kept.
"""

import csv
import hashlib
import io
import json
import re
import shutil
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

from sequenceguider import categories, markdown

NOTION_DIR = "notion"
_ID = re.compile(r"^(.*?)\s+([0-9a-f]{32})$")
_LINK = re.compile(r"(!?\[(?:[^\[\]]|\[[^\]]*\])*\]\()(<[^>]*>|[^)\s]*(?:\([^)\s]*\)[^)\s]*)*)((?:\s+\"[^\"]*\")?\))")
_URL = re.compile(r"https?://[^\s<>\"')\]|]+[^\s<>\"')\]|.,;:!?]")
_YOUTUBE = re.compile(r"(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|embed/|live/)|youtu\.be/)([\w-]{11})")
MEDIA_EXTS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".m4a", ".mp3", ".wav", ".aac", ".ogg", ".opus", ".flac"}

# which Notion property means what (names lowercased, spaces removed)
ROLES = {
    "people": {"사람", "people", "person", "강사", "선생님", "teacher", "teachers", "파트너", "partner", "참여자"},
    "keywords": {"keyword", "keywords", "키워드", "tags", "tag", "태그", "카테고리", "category", "categories", "주제"},
    "summary": {"요약", "summary", "한줄요약", "메모"},
    "date": {"날짜", "date", "일자", "레슨날짜", "수업날짜", "날짜및시간"},
    "video": {"영상주소", "영상", "video", "videourl", "url", "link", "링크", "유튜브", "youtube", "영상링크"},
}
KNOWN_PROPS = {n for names in ROLES.values() for n in names} | {"created", "생성일", "생성일시", "createdtime", "최종편집일시",
                                                               "lasteditedtime", "status", "상태", "type", "유형"}

KINDS = {"lesson": "레슨", "practica": "쁘락", "feedback": "피드백", "note": "정리"}
_KIND_WORDS = [  # first match wins: "쁘락 오답 체크" is a practica note, "피드백 정리 노트" feedback
    ("practica", re.compile(r"쁘락|쁘라|프락|practica|práctica|밀롱가|milonga", re.IGNORECASE)),
    ("feedback", re.compile(r"피드백|오답|feedback|교정", re.IGNORECASE)),
    ("lesson", re.compile(r"레슨|수업|강습|클래스|워크숍|워크샵|class|lesson|workshop|seminar|세미나", re.IGNORECASE)),
]


def kind_of(title: str, keywords=()) -> str:
    text = " ".join([title, *keywords])
    return next((k for k, p in _KIND_WORDS if p.search(text)), "note")


def _key(name: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", name)).lower()


def _role(prop: str) -> str | None:
    k = _key(prop)
    return next((r for r, names in ROLES.items() if k in names), None)


def split_name(stem: str) -> tuple[str, str | None]:
    """'소똥 개인레슨 1a12afc0...' -> ('소똥 개인레슨', '1a12afc0...')."""
    stem = unicodedata.normalize("NFC", stem)
    m = _ID.match(stem)
    return (m.group(1).strip(), m.group(2)) if m else (stem.strip(), None)


# -- dates ---------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                         "september", "october", "november", "december"], 1)}


def parse_date(text: str) -> str | None:
    """Notion writes dates the way the workspace's language does: '2025년 12월 13일 오후 3:00',
    'December 13, 2025', '2025/12/13', '2025-12-13 → 2025-12-14'. The first date, as YYYY-MM-DD."""
    text = (text or "").strip().lstrip("@")
    try:
        if m := re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", text):
            return date(*map(int, m.groups())).isoformat()
        if m := re.search(r"(\d{4})[-./]\s*(\d{1,2})[-./]\s*(\d{1,2})", text):
            return date(*map(int, m.groups())).isoformat()
        if m := re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", text):
            month = next((n for name, n in _MONTHS.items() if name.startswith(m.group(1).lower())), None)
            if month:
                return date(int(m.group(3)), month, int(m.group(2))).isoformat()
        if m := re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text):  # US style
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
    except ValueError:
        return None
    return None


def _split_list(value: str) -> list[str]:
    return [v.strip() for v in re.split(r",\s*", value or "") if v.strip()]


# -- opening the export --------------------------------------------------------

def _zip_name(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if not info.flag_bits & 0x800:  # no UTF-8 flag: zipfile read it as cp437; Notion (and Windows) wrote UTF-8 or cp949
        raw = name.encode("cp437", errors="replace")
        for enc in ("utf-8", "cp949"):
            try:
                name = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    return unicodedata.normalize("NFC", name.replace("\\", "/"))


def _extract(zf: zipfile.ZipFile, dest: Path, depth: int = 0) -> None:
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = _zip_name(info)
        parts = [p for p in PurePosixPath(name).parts if p not in ("", ".", "..") and not p.endswith(":")]
        if not parts or parts[0] == "__MACOSX" or parts[-1].startswith("._"):
            continue
        target = dest.joinpath(*parts)
        if not target.resolve().is_relative_to(dest.resolve()):
            continue
        if parts[-1].lower().endswith(".zip") and depth < 2:  # big exports: Export-...-Part-1.zip inside the zip
            with zf.open(info) as f, zipfile.ZipFile(io.BytesIO(f.read())) as inner:
                _extract(inner, dest, depth + 1)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as f, open(target, "wb") as out:
            shutil.copyfileobj(f, out)


def open_export(src: Path, work: Path) -> Path:
    """The export as a folder: `src` itself if it's an unzipped folder, else the zip extracted into `work`."""
    if src.is_dir():
        return src
    if not zipfile.is_zipfile(src):
        raise ValueError("노션에서 내보낸 zip 파일이 아니에요 (Markdown & CSV로 내보낸 파일을 올려 주세요)")
    work.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src) as zf:
        _extract(zf, work)
    return work


# -- reading pages ---------------------------------------------------------------

@dataclass
class Page:
    path: Path
    title: str
    id: str
    props: dict[str, str] = field(default_factory=dict)
    body: str = ""
    children: list["Page"] = field(default_factory=list)
    is_row: bool = False

    def tree(self, depth: int = 0):
        yield self, depth
        for c in self.children:
            yield from c.tree(depth + 1)


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _parse_page(path: Path, prop_names: set[str]) -> tuple[str | None, dict[str, str], str]:
    """(title from '# ...', properties, body) of an exported page. The properties are the
    'Name: value' lines Notion writes right under the title."""
    lines = _read_text(path).replace("\r\n", "\n").split("\n")
    i, title = 0, None
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and (m := re.match(r"^#\s+(.*)$", lines[i].strip())):
        title, i = m.group(1).strip(), i + 1
    j = i
    while j < len(lines) and not lines[j].strip():
        j += 1
    props: dict[str, str] = {}
    while j < len(lines) and (m := re.match(r"^([^:\n]{1,40}?):\s?(.*)$", lines[j].strip())):
        name = m.group(1).strip()
        if _key(name) not in prop_names and _key(name) not in KNOWN_PROPS:
            break
        props[name] = m.group(2).strip()
        j += 1
    body_start = j if props else i
    return title, props, "\n".join(lines[body_start:]).strip("\n")


def _read_csvs(root: Path) -> tuple[dict[str, list[dict]], set[str], set[Path]]:
    """Rows of every database CSV by title, all their column names, and the folders their pages are in."""
    rows: dict[str, list[dict]] = {}
    names: set[str] = set()
    db_dirs: set[Path] = set()
    for path in sorted(root.rglob("*.csv")):
        title, _ = split_name(path.stem.removesuffix("_all"))
        db_dirs |= {path.with_suffix(""), path.with_name(path.stem.removesuffix("_all")), path.parent / title}
        if path.stem.endswith("_all") and path.with_name(path.stem.removesuffix("_all") + ".csv").is_file():
            continue  # newer exports write both; the _all one has the same rows
        reader = csv.DictReader(io.StringIO(_read_text(path)))
        if not reader.fieldnames:
            continue
        names |= {_key(f) for f in reader.fieldnames if f}
        title_col = reader.fieldnames[0]
        for row in reader:
            t = unicodedata.normalize("NFC", (row.get(title_col) or "").strip())
            rows.setdefault(t, []).append({k: (v or "").strip() for k, v in row.items() if k and k != title_col})
    return rows, names, db_dirs


def read_export(root: Path) -> list[Page]:
    """The database's rows (each with its subpages) found under `root`. Without a database CSV,
    every top-level page is a row."""
    csv_rows, prop_names, db_dirs = _read_csvs(root)
    mds = sorted((p for p in root.rglob("*.md") if not p.name.startswith("._")), key=lambda p: (len(p.parts), str(p)))
    pages: dict[Path, Page] = {}
    owner: dict[Path, Page] = {}  # a page's folder -> the page (its subpages and files are in it)
    containers: set[Path] = set()  # 'Tango Note <id>.md' next to 'Tango Note <id>.csv': the database itself
    for md in mds:
        name_title, pid = split_name(md.stem)
        title, props, body = _parse_page(md, prop_names)
        title = unicodedata.normalize("NFC", title or name_title)
        pid = pid or hashlib.sha1(str(md.relative_to(root)).encode()).hexdigest()[:32]
        page = Page(md, title, pid, props, body)
        pages[md] = page
        for d in (md.with_suffix(""), md.parent / name_title):
            if d.is_dir():
                owner.setdefault(d, page)
        if md.with_suffix(".csv").is_file() or md.with_name(md.stem + "_all.csv").is_file():
            containers.add(md)

    def parent_of(md: Path) -> Page | None:
        for d in md.parents:
            if d == root or not d.is_relative_to(root):
                return None
            if (p := owner.get(d)) is not None and p.path != md:
                return p
        return None

    rows: list[Page] = []
    for md, page in pages.items():  # shallowest first, so a page's parent is placed before it
        if md in containers:
            continue
        parent = parent_of(md)
        while parent is not None and parent.path in containers:
            parent = parent_of(parent.path)
        in_db = md.parent in db_dirs or not db_dirs
        if parent is None and in_db:
            page.is_row = True
            rows.append(page)
        elif parent is not None:
            parent.children.append(page)
        elif not db_dirs:
            page.is_row = True
            rows.append(page)
    # fill properties the page didn't write from its CSV row (matched by title, in order)
    used: dict[str, int] = {}
    for page in rows:
        found = csv_rows.get(page.title) or []
        n = used.get(page.title, 0)
        if n < len(found):
            used[page.title] = n + 1
            for k, v in found[n].items():
                if v and not page.props.get(k):
                    page.props[k] = v
    for page in pages.values():  # subpages in the order the page links to them
        if page.children:
            text = unicodedata.normalize("NFC", unquote(page.body))
            order = {c.path: text.find(c.path.name) for c in page.children}
            page.children.sort(key=lambda c: (order[c.path] < 0, order[c.path], c.title))
    return rows


# -- writing lessons --------------------------------------------------------------

@dataclass
class Result:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    lessons: list[dict] = field(default_factory=list)  # {id, title, kind, status}

    def as_json(self) -> dict:
        return {"created": len(self.created), "updated": len(self.updated), "unchanged": len(self.unchanged),
                "lessons": self.lessons}


def _folder_stem(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("._")[:60] or "note"


def _media_id(url: str) -> str:
    return "n" + hashlib.sha1(url.encode()).hexdigest()[:7]


def _youtube_key(url: str) -> str:
    m = _YOUTUBE.search(url)
    return m.group(1) if m else url


def _info(page: Page) -> dict:
    by_role: dict[str, str] = {}
    for k, v in page.props.items():
        if (r := _role(k)) and v and r not in by_role:
            by_role[r] = v
    keywords = _split_list(by_role.get("keywords", ""))
    videos = _URL.findall(by_role.get("video", ""))
    return {
        "people": _split_list(by_role.get("people", "")),
        "keywords": keywords,
        "summary": by_role.get("summary", ""),
        "date": parse_date(by_role.get("date", "")),
        "videos": videos,
        "kind": kind_of(page.title, keywords),
    }


def keyword_categories(keywords: list[str], texts=()) -> tuple[list[str], list[str]]:
    """(category ids, keywords that are none of them). A keyword counts when a category's words are in it."""
    found, other = [], []
    for kw in keywords:
        hits = categories.count_mentions(kw)
        found += hits
        if not hits:
            other.append(kw)
    found += categories.count_mentions("\n".join(texts))
    return categories.clean(found), other


def _existing(home: Path) -> dict[str, Path]:
    """Lesson folders already imported, by Notion page id."""
    out = {}
    for meta_path in home.glob("*/lesson.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(meta, dict) and (nid := (meta.get("notion") or {}).get("id")):
            out[nid] = meta_path.parent
    return out


def _digest(row: Page, export_root: Path) -> str:
    h = hashlib.sha1()
    for page, depth in row.tree():
        h.update(f"{depth}\0{page.title}\0{json.dumps(page.props, sort_keys=True, ensure_ascii=False)}\0{page.body}\0".encode())
    folder = row.path.with_suffix("")
    for d in {folder, row.path.parent / split_name(row.path.stem)[0]}:
        if d.is_dir():
            for f in sorted(d.rglob("*")):
                if f.is_file() and f.suffix.lower() not in (".md", ".csv"):
                    h.update(f"{f.relative_to(export_root)}\0{f.stat().st_size}\0".encode())
    return h.hexdigest()


def _rewrite(page: Page, export_root: Path, files_dir: Path, page_anchor: dict[Path, str],
             row_lesson: dict[Path, str], copied: dict[Path, str], media_files: list[str]) -> str:
    """The page's body with its relative links pointed at what this import wrote:
    attachments -> files/<name>, its subpages -> #page-<id>, other imported pages -> #/lesson/<id>/doc."""
    def fix(m: re.Match) -> str:
        target = m.group(2).strip().removeprefix("<").removesuffix(">")
        if re.match(r"^[a-z][\w+.-]*:", target, re.IGNORECASE) or target.startswith("#"):
            return m.group(0)
        rel, _, frag = unquote(target).partition("#")
        path = (page.path.parent / rel)
        try:
            path = path.resolve()
        except OSError:
            return m.group(0)
        path = Path(unicodedata.normalize("NFC", str(path)))
        if path in page_anchor:
            return f"{m.group(1)}#page-{page_anchor[path]}{m.group(3)}"
        if path in row_lesson:
            return f"{m.group(1)}#/lesson/{row_lesson[path]}/doc{m.group(3)}"
        if path.is_file() and path.is_relative_to(export_root.resolve()) and path.suffix.lower() not in (".md", ".csv"):
            if path not in copied:
                short = hashlib.sha1(str(path).encode()).hexdigest()[:8]
                safe = re.sub(r'[\\/:*?"<>|\s]+', "_", path.name)[-50:]
                name = f"{short}_{safe}"
                files_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, files_dir / name)
                copied[path] = name
                if path.suffix.lower() in MEDIA_EXTS:
                    media_files.append(name)
            return f"{m.group(1)}files/{copied[path].replace(' ', '%20')}{m.group(3)}"
        return m.group(0)  # a page that wasn't exported: the renderer keeps just its text
    return _LINK.sub(fix, page.body)


def import_export(home: Path, src: Path, *, dry_run: bool = False, work: Path | None = None) -> Result:
    """Turn a Notion export (zip or unzipped folder) into lessons in `home`. See the module docstring."""
    home = home.resolve()
    work = work or home / ".imports" / uuid.uuid4().hex[:8]
    try:
        root = open_export(src, work).resolve()
        rows = read_export(root)
        if not rows:
            raise ValueError("내보낸 파일에서 노션 페이지(.md)를 찾지 못했어요. 내보내기 형식을 'Markdown & CSV'로 골랐는지 확인해 주세요")
        return _write_all(home, root, rows, dry_run)
    finally:
        if work.exists() and not src.is_dir():
            shutil.rmtree(work, ignore_errors=True)


def _write_all(home: Path, root: Path, rows: list[Page], dry_run: bool) -> Result:
    result = Result()
    existing = _existing(home)
    taken = {p.name for p in home.iterdir() if p.is_dir()}
    folders: dict[str, Path] = {}
    for row in rows:  # pick every folder first, so pages can link to each other
        if row.id in existing:
            folders[row.id] = existing[row.id]
            continue
        stem, n = _folder_stem(row.title), 2
        name = f"{stem}_sequenceguider"
        while name in taken:
            name, n = f"{stem}_{n}_sequenceguider", n + 1
        taken.add(name)
        folders[row.id] = home / name
    row_lesson = {Path(unicodedata.normalize("NFC", str(r.path.resolve()))): folders[r.id].name for r in rows}
    for row in rows:
        folder = folders[row.id]
        info = _info(row)
        status = "created" if row.id not in existing else "updated"
        meta = (json.loads((folder / "lesson.json").read_text(encoding="utf-8"))
                if status == "updated" else {"title": row.title, "media": []})
        digest = _digest(row, root)
        old = meta.get("notion") or {}
        if status == "updated" and old.get("digest") == digest:
            status = "unchanged"
        getattr(result, status).append(folder.name)
        result.lessons.append({"id": folder.name, "title": row.title, "kind": info["kind"], "status": status})
        if dry_run or status == "unchanged":
            continue
        folder.mkdir(parents=True, exist_ok=True)
        ndir = folder / NOTION_DIR
        if ndir.exists():
            shutil.rmtree(ndir)
        ndir.mkdir()
        tree = list(row.tree())
        anchors = {Path(unicodedata.normalize("NFC", str(p.path.resolve()))): f"{i}" for i, (p, _) in enumerate(tree)}
        copied: dict[Path, str] = {}
        media_files: list[str] = []
        pages = []
        for i, (page, depth) in enumerate(tree):
            body = _rewrite(page, root, ndir / "files", anchors, row_lesson, copied, media_files)
            (ndir / f"{i}.md").write_text(body, encoding="utf-8")
            pages.append({"anchor": str(i), "title": page.title, "depth": depth, "file": f"{i}.md",
                          "props": {} if i == 0 else page.props})
        # media: the video links (영상주소, then YouTube links in the text) and video/audio attachments
        links = list(info["videos"])
        for page, _ in tree:
            links += [u for u in _URL.findall(page.body) if _YOUTUBE.search(u)]
        have = {_youtube_key(m.get("src") or "") for m in meta["media"]}
        now = datetime.now().isoformat(timespec="seconds")
        for url in links:
            if _youtube_key(url) in have:
                continue
            have.add(_youtube_key(url))
            mid = _media_id(url)
            meta["media"].append({"id": mid, "label": "", "src": url, "dir": f"media/{mid}", "analyze": False,
                                  "added_at": now, "from": "notion"})
        for name in media_files:
            src = f"{NOTION_DIR}/files/{name}"
            if src not in {m.get("src") for m in meta["media"]}:
                mid = _media_id(src)
                meta["media"].append({"id": mid, "label": name.split("_", 1)[-1], "src": src, "dir": f"media/{mid}",
                                      "analyze": False, "added_at": now, "from": "notion"})
        cats, tags = keyword_categories(info["keywords"], [row.title, info["summary"]])
        if "categories" not in meta or meta["categories"] == old.get("categories"):
            meta["categories"] = cats  # the user hasn't changed them in the app since the last import
        meta["title"] = row.title if status == "created" or meta.get("title") == old.get("title") else meta["title"]
        meta["notion"] = {
            "id": row.id, "title": row.title, "kind": info["kind"], "date": info["date"], "people": info["people"],
            "keywords": info["keywords"], "tags": tags, "summary": info["summary"], "categories": cats,
            "props": row.props, "pages": pages, "digest": digest, "imported_at": now,
        }
        tmp = folder / "lesson.json.tmp"
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(folder / "lesson.json")
    return result


# -- showing a lesson's pages -------------------------------------------------------

def render_doc(folder: Path, lesson_id: str) -> dict:
    """The imported pages of a lesson as HTML, for the web app."""
    meta = json.loads((folder / "lesson.json").read_text(encoding="utf-8"))
    info = meta.get("notion")
    if not info:
        raise KeyError("노션에서 가져온 글이 없는 레슨입니다")
    base = f"/lessons/{lesson_id}/{NOTION_DIR}/"

    def link(target: str, image: bool) -> str | None:
        if target.startswith("files/"):
            return base + target
        if target.startswith("#") or re.match(r"^(https?:|mailto:)", target, re.IGNORECASE):
            return target
        return None  # a link to something the export didn't include

    pages = []
    for p in info.get("pages", []):
        try:
            text = (folder / NOTION_DIR / p["file"]).read_text(encoding="utf-8")
        except OSError:
            text = ""
        props = [{"name": k, "value": v} for k, v in (p.get("props") or {}).items() if v]
        pages.append({"anchor": p["anchor"], "title": p["title"], "depth": p["depth"], "props": props,
                      "html": markdown.render(text, link)})
    shown = {_key(k) for k in ("사람", "keyword", "요약", "날짜", "영상주소")}
    extra = [{"name": k, "value": v} for k, v in (info.get("props") or {}).items()
             if v and _role(k) is None and _key(k) not in shown]
    return {"title": meta.get("title") or info.get("title"), "kind": info.get("kind"), "date": info.get("date"),
            "people": info.get("people", []), "keywords": info.get("keywords", []), "summary": info.get("summary", ""),
            "props": extra, "pages": pages, "imported_at": info.get("imported_at")}
