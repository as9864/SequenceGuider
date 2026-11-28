"""Spaced-repetition review of what was learned, and the "today's one sequence" pick.

Review cards are made on the fly from what is already on disk, so every lesson,
old ones too, has cards without anything being written:

    point     a key point of a lesson's notes (notes.json key_points), with the
              lesson flow section it falls in for its figures and clip length
    figure    a figure of a guide (`analyze`) with its key point, clipped to its segment
    practica  something marked 헷갈려요 in a practica log, until a later practica
              marks the same thing 잘 됐어요

Only the schedule is stored (records.py `reviews`). A card is reviewed one day
after it was learned, then 3, 7 and 21 days after that, then every 60 days:
"기억나요" moves it one step on, "가물가물" sends it back to tomorrow. New cards
are let in a few a day, so an old library of 30 lessons doesn't land on one day.

Everything here is plain functions over lists and dicts; web.py wires it to the
lessons, the records and the API.
"""

import hashlib
import json
import re
from datetime import date, timedelta
from pathlib import Path

INTERVALS = (1, 3, 7, 21, 60)  # days until the next review, by step
NEW_PER_DAY = 5  # new lesson cards let in each day (practica cards always come in)
QUEUE_SIZE = 10  # cards shown in one sitting; "더 하기" shows the next ones
RECENT_DAYS = 21  # a 헷갈려요 from a practica this recent pushes related cards to the front
CLIP_BEFORE, CLIP_LEN = 3.0, 30.0  # seconds of a key point's clip


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def _day(iso: str | None) -> str:
    return (iso or date.today().isoformat())[:10]


def lesson_cards(home: Path, lesson: dict) -> list[dict]:
    """The review cards of one lesson (an entry of web.find_lessons)."""
    cards = []
    learned = _day(lesson.get("updated_at"))
    for m in lesson["media"]:
        base = {"lesson": lesson["id"], "lesson_title": lesson["title"], "media": m["id"],
                "categories": lesson.get("categories", []), "learned": _day(m.get("added_at") or learned)}
        notes = _read_json(home / lesson["id"] / m.get("dir", "") / "notes.json") if m.get("notes") else None
        if notes:
            sections, names = notes.get("sections") or [], notes.get("figure_names") or {}
            duration = notes.get("duration_s") or 0
            for p in notes.get("key_points") or []:
                t = float(p.get("t") or 0)
                sec = next((s for s in sections if s.get("start", 0) <= t < s.get("end", 0)), None)
                end = t + CLIP_LEN
                if sec and sec.get("end", 0) > t + 8:
                    end = min(end, sec["end"])
                if duration:
                    end = min(end, duration)
                figures = list((sec or {}).get("figures") or [])
                cards.append({**base, "id": f"kp:{lesson['id']}:{m['id']}:{_hash(p.get('point', ''))}", "kind": "point",
                              "point": p.get("point", ""), "detail": p.get("detail", ""), "quote": p.get("quote", ""),
                              "tag": p.get("kind", "other"), "role": p.get("role", "all"),
                              "section": (sec or {}).get("title", ""),
                              "t": t, "start": max(0.0, t - CLIP_BEFORE), "end": end,
                              "figures": figures, "figure_names": {k: names.get(k, k) for k in figures}})
        for f in m.get("figures") or []:
            if not f.get("key_point"):
                continue
            cards.append({**base, "id": f"fig:{lesson['id']}:{m['id']}:{f.get('order')}:{f.get('figure')}", "kind": "figure",
                          "point": f["key_point"], "detail": "", "quote": "", "tag": "figure", "role": lesson.get("role", "all"),
                          "section": f.get("name_ko", ""),
                          "t": f.get("start_s"), "start": f.get("start_s"), "end": f.get("end_s"),
                          "figures": [f["figure"]], "figure_names": {f["figure"]: f.get("name_ko", f["figure"])}})
    return cards


def _item_key(item: dict) -> str:
    return item.get("figure") or _norm(item.get("label", ""))


def open_confusions(practicas: list[dict]) -> list[dict]:
    """헷갈려요 items not since marked 잘 됐어요 at a later practica, newest first.
    `practicas` are records.Practica as dicts, in any order."""
    order = sorted(practicas, key=lambda p: (p["day"], p["id"]))
    out: dict[str, dict] = {}
    for p in order:
        for i, item in enumerate(p["items"]):
            key = _item_key(item)
            if item["feel"] == "confusing":
                out[key] = {**item, "practica": p["id"], "index": i, "day": p["day"], "key": key}
            elif item["feel"] == "good":
                out.pop(key, None)
    return sorted(out.values(), key=lambda x: x["day"], reverse=True)


def practica_cards(practicas: list[dict], figure_names: dict[str, str]) -> list[dict]:
    cards = []
    for c in open_confusions(practicas):
        figures = [c["figure"]] if c.get("figure") else []
        cards.append({"id": f"pr:{c['practica']}:{c['index']}", "kind": "practica", "lesson": c.get("lesson"),
                      "lesson_title": "", "media": None, "categories": [], "learned": c["day"],
                      "point": c["label"], "detail": c.get("note", ""), "quote": "", "tag": "practica", "role": "all",
                      "section": "", "t": None, "start": None, "end": None,
                      "figures": figures, "figure_names": {k: figure_names.get(k, k) for k in figures}})
    return cards


def next_state(step: int | None, remembered: bool, day: str) -> tuple[int, str]:
    """(step, due) after an answer. A new card is step None."""
    d = date.fromisoformat(day)
    if not remembered:
        return 0, (d + timedelta(days=INTERVALS[0])).isoformat()
    step = min((step or 0) + 1, len(INTERVALS) - 1)
    return step, (d + timedelta(days=INTERVALS[step])).isoformat()


def plan(cards: list[dict], reviews: dict[str, dict], log: list[dict], practicas: list[dict], day: str,
         *, new_per_day: int = NEW_PER_DAY) -> dict:
    """What to review on `day`.

    queue: due cards then new ones, cards related to a recent practica confusion first,
    then the most overdue, then those forgotten most. done_today: answered today.
    """
    recent = (date.fromisoformat(day) - timedelta(days=RECENT_DAYS)).isoformat()
    hot = {c["figure"] for c in open_confusions(practicas) if c.get("figure") and c["day"] >= recent}
    hot_labels = {_norm(c["label"]) for c in open_confusions(practicas) if c["day"] >= recent}
    done_today = {e["card_id"] for e in log if e["day"] == day}

    def boosted(c: dict) -> bool:
        return c["kind"] == "practica" or bool(hot & set(c["figures"])) or _norm(c["point"]) in hot_labels

    due, new = [], []
    started_today = sum(1 for k, r in reviews.items() if r["first_at"] == day and not k.startswith("pr:"))
    for c in cards:
        r = reviews.get(c["id"])
        if c["id"] in done_today:
            continue
        if r is None:
            if c["learned"] < day or c["kind"] == "practica":
                new.append(c)
        elif r["due"] <= day:
            due.append({**c, "step": r["step"], "due": r["due"], "lapses": r["lapses"]})
    due.sort(key=lambda c: (not boosted(c), c["due"], -c["lapses"]))
    # newest lessons first (what was just learned), in lesson order within one
    new.sort(key=lambda c: (c["lesson"] or "", c["t"] or 0))
    new.sort(key=lambda c: c["learned"], reverse=True)
    new.sort(key=lambda c: (not boosted(c), c["kind"] != "practica"))
    room = max(0, new_per_day - started_today)
    # practica cards always come in; lesson cards up to the day's room, related ones first (sorted above)
    fresh = [c for c in new if c["kind"] == "practica"] + [c for c in new if c["kind"] != "practica"][:room]
    fresh = [{**c, "step": None, "due": day, "lapses": 0} for c in fresh]
    # a fresh practica confusion first, then what's due, then new lesson cards
    queue = [c for c in fresh if c["kind"] == "practica"] + due + [c for c in fresh if c["kind"] != "practica"]
    for c in queue:
        c["boosted"] = boosted(c)

    upcoming: dict[str, int] = {}
    for r in reviews.values():
        if r["due"] > day:
            upcoming[r["due"]] = upcoming.get(r["due"], 0) + 1
    return {"day": day, "queue": queue, "due": len(due), "new": len(fresh), "waiting_new": len(new) - len(fresh),
            "done_today": len(done_today), "streak": streak({e["day"] for e in log}, day),
            "upcoming": dict(sorted(upcoming.items())[:14]), "total_cards": len(cards),
            "learned_cards": sum(1 for c in cards if c["id"] in reviews)}


def streak(days: set[str], day: str) -> int:
    """Days in a row with a review, up to `day` (not yet reviewing today doesn't break it)."""
    d = date.fromisoformat(day)
    if day not in days:
        d -= timedelta(days=1)
    n = 0
    while d.isoformat() in days:
        n += 1
        d -= timedelta(days=1)
    return n


def pick_today(cards: list[dict], queue: list[dict], reviews: dict[str, dict], day: str,
               practicas: list[dict] = ()) -> dict | None:
    """"Today's one sequence": one card with a clip to watch today.

    The lesson scene of the newest practica confusion first (due or not: it's what to
    look at again before the next practica), then the first due card, then whatever
    was reviewed longest ago. Only cards with a clip (a time in a video).
    """
    recent = (date.fromisoformat(day) - timedelta(days=RECENT_DAYS)).isoformat()
    for conf in open_confusions(list(practicas)):
        if conf["day"] < recent or not conf.get("figure"):
            continue
        related = [c for c in cards if c.get("start") is not None and conf["figure"] in c["figures"]]
        if related:
            related.sort(key=lambda c: (reviews.get(c["id"], {}).get("last_at", ""), c["lesson"], c["t"] or 0))
            return {**related[0], "boosted": True}
    clip = [c for c in queue if c.get("start") is not None]
    if clip:
        return next((c for c in clip if c.get("boosted")), clip[0])
    rest = [c for c in cards if c.get("start") is not None]
    if not rest:
        return None
    seed = int(hashlib.sha1(day.encode()).hexdigest(), 16)
    rest.sort(key=lambda c: (reviews.get(c["id"], {}).get("last_at", ""), (int(_hash(c["id"]), 16) ^ seed)))
    return rest[0]
