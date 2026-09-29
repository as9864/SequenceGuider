"""Figure knowledge base (data/figures.yaml) and sequence parsing.

A sequence is written the way dancers say it:
    "살리다, 오초 아뜨라스 x3, 푸에라 데 에헤 @1:23"
- separators: comma, newline, "->", "→", ">"
- "x3" / "×3" / "*3": repeat count (three back ochos in a row)
- "@1:23" / "@83.5": optional start time anchor (seconds or m:ss) —
  pins where that figure starts when the automatic split gets it wrong
"""

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_LIBRARY = Path(__file__).parent / "data" / "figures.yaml"
ROLES = ("all", "leader", "follower")


@dataclass(frozen=True)
class Caution:
    role: str
    text: str


DEFAULT = "default"  # CheckSpec bound not given: use the check's own default


@dataclass(frozen=True)
class CheckSpec:
    """A figure's use of a check. min/max: a number overrides the check's
    default bound, an explicit `null` in YAML removes that bound."""

    id: str
    min: float | None | str = DEFAULT
    max: float | None | str = DEFAULT
    message_high: str | None = None
    message_low: str | None = None


@dataclass(frozen=True)
class Figure:
    key: str
    name_ko: str
    name_es: str
    aliases: tuple[str, ...]
    steps: int
    summary: str
    cautions: tuple[Caution, ...]
    checks: tuple[CheckSpec, ...]

    def cautions_for(self, role: str) -> list[Caution]:
        if role == "all":
            return list(self.cautions)
        return [c for c in self.cautions if c.role in ("all", role)]


@dataclass
class SequenceItem:
    figure: Figure
    count: int = 1
    anchor_s: float | None = None
    end_s: float | None = None  # hard end (e.g. the video chapter ends here)
    raw: str = ""

    @property
    def expected_steps(self) -> int:
        return max(1, self.figure.steps * self.count)

    @property
    def label(self) -> str:
        return self.figure.name_ko + (f" ×{self.count}" if self.count > 1 else "")


def normalize(text: str) -> str:
    """Lowercase, drop accents/spaces/punctuation: 'Ocho Atrás' == 'ochoatras'."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = unicodedata.normalize("NFC", text)  # recompose Hangul
    return re.sub(r"[\s\-_.·'’]+", "", text.lower())


@dataclass
class FigureLibrary:
    figures: dict[str, Figure]
    _alias_index: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for key, fig in self.figures.items():
            for name in (key, fig.name_ko, fig.name_es, *fig.aliases):
                self._alias_index.setdefault(normalize(name), key)

    @classmethod
    def load(cls, path: Path = DEFAULT_LIBRARY) -> "FigureLibrary":
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)["figures"]
        figures = {}
        for key, spec in raw.items():
            cautions = []
            for c in spec.get("cautions", []):
                if c["role"] not in ROLES:
                    raise ValueError(f"{key}: unknown role {c['role']!r}")
                cautions.append(Caution(role=c["role"], text=c["text"]))
            figures[key] = Figure(
                key=key,
                name_ko=spec["name_ko"],
                name_es=spec["name_es"],
                aliases=tuple(spec.get("aliases", [])),
                steps=int(spec.get("steps", 1)),
                summary=spec.get("summary", ""),
                cautions=tuple(cautions),
                checks=tuple(CheckSpec(**c) for c in spec.get("checks", [])),
            )
        return cls(figures)

    def resolve(self, name: str) -> Figure:
        key = self._alias_index.get(normalize(name))
        if key is None:
            raise UnknownFigureError(name, self.suggest(name))
        return self.figures[key]

    def find_in_text(self, text: str) -> Figure | None:
        """The figure named somewhere inside free text, e.g. a video chapter
        title "3. 오초 아뜨라스 (Ocho atrás) 연습". Longest matching alias wins,
        so "ocho atras" beats the bare "ocho"/"오초".

        Latin aliases must match on word boundaries ("turn" must not fire on
        "return"; an English plural like "Sacadas" is fine); Hangul aliases match as substrings of at least two
        syllables, since Korean attaches particles ("오초를") to the noun.
        """
        plain = unicodedata.normalize("NFKD", text.lower())
        plain = unicodedata.normalize("NFC", "".join(ch for ch in plain if not unicodedata.combining(ch)))
        squashed = normalize(text)
        best: tuple[int, int, str] | None = None  # (-length, position, key)
        for key, fig in self.figures.items():
            for alias in (fig.name_ko, fig.name_es, key.replace("_", " "), *fig.aliases):
                a = normalize(alias)
                if len(a) < 2:
                    continue
                if alias.isascii():
                    words = [re.escape(w) for w in re.split(r"[\s_\-]+", unicodedata.normalize("NFKD", alias.lower())) if w]
                    words = ["".join(ch for ch in w if not unicodedata.combining(ch)) for w in words]
                    m = re.search(r"(?<![a-z])" + r"[\s\-_]*".join(words) + r"(?:e?s)?(?![a-z])", plain)
                    pos = m.start() if m else -1
                else:
                    pos = squashed.find(a)
                if pos >= 0:
                    cand = (-len(a), pos, key)
                    if best is None or cand < best:
                        best = cand
        return self.figures[best[2]] if best else None

    def suggest(self, name: str, n: int = 3) -> list[str]:
        matches = difflib.get_close_matches(normalize(name), list(self._alias_index), n=n * 3, cutoff=0.5)
        seen: list[str] = []
        for m in matches:
            ko = self.figures[self._alias_index[m]].name_ko
            if ko not in seen:
                seen.append(ko)
        return seen[:n]


class UnknownFigureError(ValueError):
    def __init__(self, name: str, suggestions: list[str]):
        self.name = name
        self.suggestions = suggestions
        hint = f" (혹시: {', '.join(suggestions)}?)" if suggestions else ""
        super().__init__(f"알 수 없는 피구라: {name!r}{hint}")


_SEPARATORS = re.compile(r"\s*(?:,|\n|->|→|>|;)\s*")
_COUNT = re.compile(r"\s*[x×*]\s*(\d+)\s*$", re.IGNORECASE)
_ANCHOR = re.compile(r"\s*@\s*(\d+(?::\d{1,2}){0,2}(?:\.\d+)?)\s*$")


def parse_time(text: str) -> float:
    """'83.5' -> 83.5, '1:23' -> 83.0, '1:23.5' -> 83.5, '1:02:03' -> 3723.0."""
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"시간 형식이 아닙니다: {text!r}")
    total = 0.0
    for part in parts[:-1]:
        total = total * 60 + int(part)
    return total * 60 + float(parts[-1]) if len(parts) > 1 else float(parts[-1])


def parse_sequence(text: str, library: FigureLibrary) -> list[SequenceItem]:
    items: list[SequenceItem] = []
    for chunk in _SEPARATORS.split(text.strip()):
        if not chunk:
            continue
        raw = chunk
        anchor = None
        count = 1
        # anchor and count can come in either order: "오초 x3 @0:10" / "오초 @0:10 x3"
        for _ in range(2):
            if m := _ANCHOR.search(chunk):
                anchor = parse_time(m.group(1))
                chunk = chunk[: m.start()]
            if m := _COUNT.search(chunk):
                count = int(m.group(1))
                chunk = chunk[: m.start()]
        if count < 1:
            raise ValueError(f"반복 횟수는 1 이상이어야 합니다: {raw!r}")
        items.append(SequenceItem(figure=library.resolve(chunk.strip()), count=count, anchor_s=anchor, raw=raw))
    if not items:
        raise ValueError("시퀀스가 비어 있습니다")
    anchors = [i.anchor_s for i in items if i.anchor_s is not None]
    if anchors != sorted(anchors):
        raise ValueError("@시간은 시퀀스 순서대로 증가해야 합니다")
    return items
