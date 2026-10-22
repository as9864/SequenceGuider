"""Tango categories of a lesson: what it is about (걷기, 아브라소, 사까다, 볼레오, ...), several per lesson.

They tag a whole lesson so the library can show "every lesson about 볼레오". `suggest` guesses
them from what the analyses found (figures in the guide, the notes' sections, the notes' own
`categories`, words in the summary, key points and transcript); the user edits them in the web app,
and the edited list is kept in lesson.json (see web.py).
"""

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Category:
    id: str
    name_ko: str
    group: str  # 기본기 | 피구라 | 장식·기술 | 음악
    aliases: tuple[str, ...]  # words that mean it in notes and transcripts (ko / es / en)
    figures: tuple[str, ...] = ()  # figures.yaml keys that imply it


CATEGORIES: tuple[Category, ...] = (
    # 기본기
    Category("caminata", "걷기", "기본기", ("걷기", "걸음", "워킹", "까미나따", "카미나타", "caminata", "caminar", "walk", "walking"),
             ("caminata",)),
    Category("abrazo", "안기", "기본기", ("안기", "아브라소", "아브라쏘", "abrazo", "embrace", "홀딩", "프레임")),
    Category("energia", "에너지", "기본기", ("에너지", "energía", "energia", "energy", "텐션", "tension", "탄성")),
    Category("conexion", "연결·리드", "기본기", ("커넥션", "connection", "conexión", "conexion", "마르까", "marca")),
    Category("eje", "축·자세", "기본기", ("축을", "축이", "축은", "축에", "축과", "축으로", "eje", "axis", "postura", "posture")),
    Category("pivote", "피벗", "기본기", ("피벗", "피봇", "pivot", "pivote")),
    Category("disociacion", "디소시아시온", "기본기", ("디소시아시온", "디소시아시옹", "디소시에이션", "disociación", "disociacion", "dissociation")),
    # 피구라
    Category("salida", "살리다·기본 스텝", "피구라", ("살리다", "기본 스텝", "기본스텝", "salida", "basic step"), ("salida_cruzada",)),
    Category("cruzada", "크루사다", "피구라", ("크루사다", "끄루사다", "cruzada", "cross"), ("cruzada", "salida_cruzada")),
    Category("ocho", "오초", "피구라", ("오초", "ocho", "ochos"), ("ocho_adelante", "ocho_atras")),
    Category("ocho_cortado", "오초 꼬르따도", "피구라", ("오초 꼬르따도", "오초 코르타도", "꼬르따도", "ocho cortado")),
    Category("giro", "히로·몰리네떼", "피구라", ("히로", "몰리네떼", "몰리네테", "giro", "molinete"), ("giro",)),
    Category("rock", "꾸니따·락", "피구라", ("꾸니따", "쿠니타", "cunita", "rock step", "락스텝", "락 스텝", "리바운드 스텝")),
    Category("sacada", "사까다", "피구라", ("사까다", "사카다", "싸까다", "sacada"), ("sacada",)),
    Category("boleo", "볼레오", "피구라", ("볼레오", "boleo", "voleo"), ("boleo",)),
    Category("gancho", "간초", "피구라", ("간초", "간쵸", "gancho", "hook"), ("gancho",)),
    Category("parada", "빠라다·빠사다", "피구라", ("빠라다", "파라다", "parada", "빠사다", "파사다", "pasada"), ("parada", "pasada")),
    Category("sandwichito", "산드위치또", "피구라", ("산드위치또", "산드위치토", "샌드위치", "sandwichito", "sanguchito", "sandwich")),
    Category("barrida", "바리다", "피구라", ("바리다", "barrida", "arrastre", "아라스뜨레", "sweep"), ("barrida",)),
    Category("calesita", "깔레시따", "피구라", ("깔레시따", "칼레시타", "calesita"), ("calesita",)),
    Category("colgada", "꼴가다", "피구라", ("꼴가다", "콜가다", "colgada"), ("colgada",)),
    Category("volcada", "볼까다", "피구라", ("볼까다", "볼카다", "volcada"), ("volcada",)),
    Category("rebote", "레보떼", "피구라", ("레보떼", "레보테", "rebote", "rebound"), ("rebote",)),
    # 장식·기술
    Category("adorno", "아도르노·장식", "장식·기술", ("아도르노", "adorno", "adornos", "embellishment", "장식", "라피스", "lápiz", "lapiz",
                                                  "엔로스께", "enrosque", "피룰레떼", "firulete")),
    Category("cambio_de_frente", "방향 전환", "장식·기술", ("방향 전환", "방향전환", "cambio de frente", "change of direction")),
    # 음악
    Category("musicalidad", "음악성", "음악", ("음악성", "뮤지컬리티", "musicality", "musicalidad", "리듬", "ritmo", "rhythm", "박자",
                                           "뜨라스삐에", "트라스피에", "traspié", "traspie", "더블타임", "double time")),
)

BY_ID = {c.id: c for c in CATEGORIES}

TEXT_HITS = 1  # in notes written for review (title, summary, key points): one mention is enough
TRANSCRIPT_HITS = 5  # in raw speech words come and go: count it only if it keeps coming up


def as_json() -> list[dict]:
    return [{"id": c.id, "name_ko": c.name_ko, "group": c.group} for c in CATEGORIES]


def clean(ids) -> list[str]:
    """Known ids only, once each, in the list's order."""
    want = {str(i) for i in ids or []}
    return [c.id for c in CATEGORIES if c.id in want]


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return unicodedata.normalize("NFC", "".join(ch for ch in text if not unicodedata.combining(ch)))


def _patterns() -> dict[str, re.Pattern]:
    out = {}
    for c in CATEGORIES:
        parts = []
        for a in c.aliases:
            a = re.escape(_norm(a))
            # Latin words need word boundaries ("turn" isn't "return"); Korean is matched inside words
            parts.append(rf"\b{a}\b" if re.match(r"[a-z]", a) else a)
        out[c.id] = re.compile("|".join(parts))
    return out


_PATTERNS = _patterns()


def count_mentions(text: str) -> dict[str, int]:
    text = _norm(text)
    return {cid: n for cid, p in _PATTERNS.items() if (n := len(p.findall(text)))}


def suggest(*, figures=(), texts=(), transcript: str = "", chosen=()) -> list[str]:
    """Categories that fit what was found. `figures`: figures.yaml keys; `texts`: notes written for
    review; `transcript`: the raw speech; `chosen`: category ids the notes model picked itself."""
    found = set(clean(chosen))
    figs = set(figures)
    found |= {c.id for c in CATEGORIES if figs & set(c.figures)}
    found |= {cid for cid, n in count_mentions("\n".join(texts)).items() if n >= TEXT_HITS}
    found |= {cid for cid, n in count_mentions(transcript).items() if n >= TRANSCRIPT_HITS}
    return clean(found)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def suggest_for_folder(folder: Path, title: str = "") -> list[str]:
    """What `suggest` makes of one analysis folder (result.json, notes.json, transcript.json)."""
    figures, texts, transcript, chosen = [], [title], "", []
    result = _read_json(folder / "result.json") or {}
    figures += [f.get("figure") for f in result.get("figures", [])]
    texts.append(str(result.get("title") or ""))
    notes = _read_json(folder / "notes.json") or {}
    if notes:
        chosen = notes.get("categories", [])
        texts += [str(notes.get("title", "")), str(notes.get("summary", ""))]
        for p in notes.get("key_points", []):
            texts += [str(p.get("point", "")), str(p.get("detail", ""))]
        for s in notes.get("sections", []):
            figures += s.get("figures", [])
            texts += [str(s.get("title", "")), str(s.get("summary", ""))]
        texts += [str(p.get("text", "")) for p in notes.get("practice", [])]
        texts += [str(g.get("term", "")) for g in notes.get("glossary", [])]
    tr = _read_json(folder / "transcript.json") or {}
    transcript = "\n".join(f"{s.get('text', '')} {s.get('ko') or ''}" for s in tr.get("segments", []))
    return suggest(figures=figures, texts=texts, transcript=transcript, chosen=chosen)
