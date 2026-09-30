"""Study-guide report: one self-contained HTML page + result.json.

Layout, top to bottom: the (overlay) video with a clickable sequence
timeline, then two tabs. "배우기" (learn) is for someone learning the
sequence from the video: per figure one key point, the top cautions,
common mistakes and a practice checklist that remembers its ticks, plus a
printable one-page summary. "점검" (review) is the analysis of the person
in the video: a numbered "learning order" summary, then one card per figure
with its cautions, automatic check results and evidence frames. Clicking
a figure plays exactly that segment — the loop a learner actually wants.
"""

import html
import json
from pathlib import Path

from sequenceguider.analyze import Analysis, FigureAnalysis
from sequenceguider.checks import CheckResult
from sequenceguider.figures import Figure
from sequenceguider.render import keyframe_jpeg_b64

CONFIDENCE_KO = {"high": "분할 신뢰 높음", "medium": "분할 확인 권장", "low": "분할 부정확 — @시간 지정 권장"}
STATUS_KO = {"ok": "좋아요", "warn": "주의", "unknown": "판단 보류", "na": "측정 불가"}
ROLE_KO = {"all": "공통", "leader": "리더", "follower": "팔로워"}
PALETTE = ["#5b8def", "#e0864a", "#4fb286", "#b56fd6", "#d65c7a", "#c9a227", "#3aa7b8", "#8a8f98"]


def fmt_t(t: float) -> str:
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m)}:{s:04.1f}"


def _fmt_value(c: CheckResult) -> str:
    if c.value is None:
        return ""
    if c.unit == "deg":
        return f"{c.value:.1f}°"
    return f"{c.value:.2f}"


def to_json(analysis: Analysis, video_name: str) -> dict:
    return {
        "video": video_name,
        "view": analysis.view,
        "role": analysis.role,
        "backend": analysis.track.backend,
        "person_coverage": analysis.person_coverage,
        "steps_detected": len(analysis.steps),
        "figures": [
            {
                "order": fa.segment.index + 1,
                "figure": fa.segment.item.figure.key,
                "name_ko": fa.segment.item.figure.name_ko,
                "count": fa.segment.item.count,
                "start_s": round(fa.segment.start_t, 3),
                "end_s": round(fa.segment.end_t, 3),
                "anchored": fa.segment.anchored,
                "steps_detected": len(fa.segment.steps),
                "steps_expected": fa.segment.item.expected_steps,
                "split_confidence": fa.segment.confidence,
                "lesson": _lesson_json(fa.segment.item.figure, analysis.role),
                "cautions": [
                    {"role": c.role, "text": c.text}
                    for c in fa.segment.item.figure.cautions_for(analysis.role)
                ],
                "checks": [
                    {
                        "id": c.id, "status": c.status, "value": c.value, "bound": c.bound,
                        "direction": c.direction, "fraction_out": c.fraction_out, "worst_t": c.worst_t,
                        "target": c.target, "message": c.message,
                    }
                    for c in fa.checks
                ],
            }
            for fa in analysis.figures
        ],
    }


def _lesson_json(fig: Figure, role: str) -> dict:
    lesson = fig.lesson(role)
    return {
        "key_point": lesson.key_point,
        "prerequisites": list(fig.prerequisites),
        "cautions": [c.text for c in lesson.cautions],
        "more_cautions": [c.text for c in lesson.more_cautions],
        "mistakes": [c.text for c in lesson.mistakes],
        "checklist": [{"id": i.id, "kind": i.kind, "role": i.role, "text": i.text} for i in lesson.checklist],
    }


def _prerequisites(analysis: Analysis) -> list[str]:
    """Names of figures the sequence builds on but doesn't itself teach, in order."""
    in_sequence = {fa.segment.item.figure.key for fa in analysis.figures}
    out: dict[str, str] = {}
    for fa in analysis.figures:
        fig = fa.segment.item.figure
        for key, name in zip(fig.prerequisites, fig.prerequisite_names):
            if key not in in_sequence:
                out.setdefault(key, name)
    return list(out.values())


def _role_tag(role: str) -> str:
    return "" if role == "all" else f"<span class='role role-{role}'>{ROLE_KO[role]}</span>"


def _learn_card(fa: FigureAnalysis, analysis: Analysis, color: str) -> str:
    seg = fa.segment
    fig = seg.item.figure
    lesson = fig.lesson(analysis.role)
    cautions = "".join(f"<li>{_role_tag(c.role)}{html.escape(c.text)}</li>" for c in lesson.cautions)
    more = ("<details><summary>주의점 더 보기 ({})</summary><ul class='cautions'>{}</ul></details>".format(
        len(lesson.more_cautions),
        "".join(f"<li>{_role_tag(c.role)}{html.escape(c.text)}</li>" for c in lesson.more_cautions))
        if lesson.more_cautions else "")
    mistakes = ("<h3>흔한 실수</h3><ul class='mistakes'>{}</ul>".format(
        "".join(f"<li>{_role_tag(c.role)}{html.escape(c.text)}</li>" for c in lesson.mistakes))
        if lesson.mistakes else "")
    items = "".join(
        f"<li><label><input type='checkbox' data-id='{html.escape(i.id)}'>"
        f"<span class='kind'>{'연습' if i.kind == 'drill' else '확인'}</span>{_role_tag(i.role)}"
        f"{html.escape(i.text)}</label></li>"
        for i in lesson.checklist
    )
    return f"""
<section class="card learn" style="--accent:{color}">
  <header>
    <div class="num-circle">{seg.index + 1}</div>
    <div class="titles">
      <h2>{html.escape(seg.item.label)} <span class="es">{html.escape(fig.name_es)}</span></h2>
      <p class="muted">{html.escape(fig.summary)}</p>
    </div>
    <div class="meta">
      <button class="play" data-s="{seg.start_t:.2f}" data-e="{seg.end_t:.2f}">▶ {fmt_t(seg.start_t)} – {fmt_t(seg.end_t)}</button>
    </div>
  </header>
  <p class="key-point">{html.escape(lesson.key_point)}</p>
  <div class="body">
    <div>
      <h3>주의할 점</h3>
      <ul class="cautions">{cautions}</ul>
      {more}
      {mistakes}
    </div>
    <div>
      <h3>연습 체크리스트 <span class="progress muted small"></span></h3>
      <ul class="checklist">{items}</ul>
    </div>
  </div>
</section>"""


def _summary_sheet(analysis: Analysis, title: str) -> str:
    """One printable page: the sequence, each figure's key point and checklist."""
    seq = " → ".join(html.escape(fa.segment.item.label) for fa in analysis.figures)
    rows = []
    for fa in analysis.figures:
        lesson = fa.segment.item.figure.lesson(analysis.role)
        checks = "".join(f"<li>☐ {html.escape(i.text)}</li>" for i in lesson.checklist)
        rows.append(f"<li><b>{html.escape(fa.segment.item.label)}</b> <span class='muted small'>"
                    f"{fmt_t(fa.segment.start_t)}</span> — {html.escape(lesson.key_point)}<ul>{checks}</ul></li>")
    return f"""
<section class="sheet" id="sheet">
  <div class="sheet-head"><h2>한 장 요약</h2><button id="print-sheet">인쇄</button></div>
  <p class="print-only"><b>{html.escape(title)}</b></p>
  <p class="seq">{seq}</p>
  <ol>{''.join(rows)}</ol>
</section>"""


def _evidence_frames(fa: FigureAnalysis) -> list[tuple[int, bool, str]]:
    """(frame index, ok?, caption): the worst frame of each warning, deduped;
    the segment midpoint when everything passed."""
    out: list[tuple[int, bool, str]] = []
    for c in fa.warnings:
        if c.worst_idx is None:
            continue
        if any(abs(c.worst_idx - i) < 5 for i, _, _ in out):
            continue
        out.append((c.worst_idx, False, f"{fmt_t(c.worst_t)} · {c.label} {_fmt_value(c)}"))
        if len(out) == 2:
            break
    if not out:
        mid = (fa.start_idx + fa.end_idx) // 2
        out.append((mid, True, "대표 장면"))
    return out


def _check_rows(fa: FigureAnalysis) -> str:
    rows = []
    for c in fa.checks:
        detail = html.escape(c.message or "")
        if c.status == "warn" and c.fraction_out is not None:
            detail += f" <span class='muted'>(구간의 {c.fraction_out:.0%})</span>"
        jump = (f"<button class='jump' data-t='{c.worst_t:.2f}'>▶ {fmt_t(c.worst_t)}</button>"
                if c.status == "warn" and c.worst_t is not None else "")
        value = f"<span class='num'>{_fmt_value(c)}</span>" if c.value is not None else ""
        rows.append(
            f"<div class='check st-{c.status}'>"
            f"<div class='line1'><span class='badge {c.status}'>{STATUS_KO[c.status]}</span>"
            f"<b title='{html.escape(c.explain)}'>{html.escape(c.label)}</b>{value}"
            f"<span class='muted small'>기준 {html.escape(c.target)}</span></div>"
            + (f"<div class='msg'>{detail} {jump}</div>" if detail else "")
            + "</div>"
        )
    return "\n".join(rows)


def _figure_card(fa: FigureAnalysis, analysis: Analysis, video: Path, color: str) -> str:
    seg = fa.segment
    fig = seg.item.figure
    cautions = "\n".join(
        f"<li><span class='role role-{c.role}'>{ROLE_KO[c.role]}</span>{html.escape(c.text)}</li>"
        for c in fig.cautions_for(analysis.role)
    )
    frames = []
    for idx, ok, caption in _evidence_frames(fa):
        b64 = keyframe_jpeg_b64(video, analysis.track, idx, ok=ok, caption=caption)
        if b64:
            t = analysis.track.t[idx]
            frames.append(f"<figure><img alt='{html.escape(caption)}' src='data:image/jpeg;base64,{b64}' "
                          f"data-t='{t:.2f}' class='jump-img'></figure>")
    n_warn = len(fa.warnings)
    summary_badge = (f"<span class='badge warn'>주의 {n_warn}</span>" if n_warn
                     else "<span class='badge ok'>자동 체크 통과</span>")
    steps_note = f"스텝 {len(seg.steps)}개 감지 / 보통 {seg.item.expected_steps}개"
    return f"""
<section class="card" id="fig-{seg.index + 1}" style="--accent:{color}">
  <header>
    <div class="num-circle">{seg.index + 1}</div>
    <div class="titles">
      <h2>{html.escape(seg.item.label)} <span class="es">{html.escape(fig.name_es)}</span></h2>
      <p class="muted">{html.escape(fig.summary)}</p>
    </div>
    <div class="meta">
      <button class="play" data-s="{seg.start_t:.2f}" data-e="{seg.end_t:.2f}">▶ {fmt_t(seg.start_t)} – {fmt_t(seg.end_t)}</button>
      <div class="muted small">{steps_note} · {CONFIDENCE_KO[seg.confidence]}{" · 시간 지정됨" if seg.anchored else ""}</div>
      {summary_badge}
    </div>
  </header>
  <div class="body">
    <div>
      <h3>주의할 점</h3>
      <ul class="cautions">{cautions}</ul>
      <h3>자동 자세 체크 <span class="muted small">({'측면' if analysis.view == 'side' else '정면'} 촬영 기준, 2D 근사)</span></h3>
      <div class="checks">{_check_rows(fa)}</div>
    </div>
    <div class="frames">{''.join(frames)}</div>
  </div>
</section>"""


_CSS = """
:root { --bg:#f7f7f5; --card:#fff; --fg:#1d1d1f; --muted:#6b6b70; --line:#e3e3e0;
        --ok:#2f9e57; --warn:#d8453a; --unk:#8a8f98; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#141416; --card:#1e1e22; --fg:#ececef; --muted:#9a9aa2; --line:#2e2e34; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font-family: "Pretendard", "Apple SD Gothic Neo", "Malgun Gothic", system-ui, sans-serif; line-height:1.55; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 1.6rem; margin: 0 0 4px; }
h2 { font-size: 1.2rem; margin: 0; }
h3 { font-size: .95rem; margin: 16px 0 8px; }
.es { font-weight: 400; color: var(--muted); font-size: .9rem; margin-left: 6px; }
.muted { color: var(--muted); } .small { font-size: .82rem; }
video { width: 100%; max-height: 62vh; background:#000; border-radius: 10px; }
.timeline { display:flex; height: 34px; border-radius: 8px; overflow: hidden; margin: 10px 0 4px; position: relative; background: var(--line); }
.timeline div { color:#fff; font-size:.78rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
                padding: 8px 6px; cursor:pointer; border-right: 1px solid rgba(255,255,255,.4); position:absolute; top:0; bottom:0; }
.timeline div.warn::after { content:"!"; margin-left:4px; font-weight:700; }
.cursor { position:absolute; top:0; bottom:0; width:2px; background: var(--fg); pointer-events:none; }
.order { background: var(--card); border:1px solid var(--line); border-radius: 12px; padding: 12px 18px; margin: 18px 0; }
.order ol { margin: 6px 0; padding-left: 22px; } .order li { margin: 4px 0; }
.order a { color: inherit; font-weight: 600; text-decoration: none; }
.card { background: var(--card); border:1px solid var(--line); border-left: 6px solid var(--accent);
        border-radius: 12px; padding: 16px 18px; margin: 16px 0; }
.card header { display:flex; gap: 14px; align-items: flex-start; flex-wrap: wrap; }
.titles { flex: 1 1 300px; } .titles p { margin: 4px 0 0; }
.meta { display:flex; flex-direction: column; align-items: flex-end; gap: 4px; }
.num-circle { width: 34px; height: 34px; border-radius: 50%; background: var(--accent); color:#fff;
              display:grid; place-items:center; font-weight: 700; flex: none; }
.body { display:grid; grid-template-columns: 1.2fr 1fr; gap: 18px; }
@media (max-width: 760px) { .body { grid-template-columns: 1fr; } .meta { align-items: flex-start; } }
.cautions { margin: 0; padding-left: 0; list-style: none; }
.cautions li { margin: 6px 0; padding: 8px 10px; background: var(--bg); border-radius: 8px; }
.role { display:inline-block; font-size:.72rem; border-radius: 4px; padding: 1px 6px; margin-right: 8px;
        background: var(--line); color: var(--fg); vertical-align: 1px; }
.role-leader { background:#5b8def33; } .role-follower { background:#d65c7a33; }
.check { border-top: 1px solid var(--line); padding: 8px 2px; }
.check .line1 { display:flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; }
.check .msg { margin: 4px 0 0 2px; font-size: .88rem; }
.num { font-variant-numeric: tabular-nums; white-space: nowrap; font-weight: 600; }
.check.st-na, .check.st-unknown { color: var(--muted); }
.badge { display:inline-block; font-size:.75rem; font-weight:600; border-radius: 999px; padding: 2px 9px; color:#fff; white-space: nowrap; }
.badge.ok { background: var(--ok); } .badge.warn { background: var(--warn); }
.badge.unknown, .badge.na { background: var(--unk); }
button { font: inherit; cursor: pointer; border: 1px solid var(--line); background: var(--bg); color: var(--fg);
         border-radius: 8px; padding: 4px 10px; }
button:hover { border-color: var(--muted); }
button.jump { font-size: .78rem; padding: 1px 7px; margin-left: 4px; }
.frames { display:flex; flex-wrap: wrap; gap: 10px; align-content: flex-start; }
.frames figure { flex: 1 1 200px; max-width: 360px; }
.frames figure { margin: 0; } .frames img { width: 100%; border-radius: 8px; cursor: pointer; display:block; }
.tabs { display:flex; gap: 6px; margin: 22px 0 4px; border-bottom: 1px solid var(--line); }
.tabs button { border: none; border-bottom: 3px solid transparent; border-radius: 0; background: none;
               padding: 8px 14px; font-weight: 600; color: var(--muted); }
.tabs button.active { color: var(--fg); border-bottom-color: var(--fg); }
.panel[hidden] { display: none; }
.prereq { margin: 14px 0; }
.key-point { font-size: 1.12rem; font-weight: 700; margin: 12px 0 0; padding: 10px 14px;
             border-radius: 8px; background: var(--bg); border-left: 4px solid var(--accent); }
details { margin: 6px 0; } details summary { cursor: pointer; color: var(--muted); font-size: .88rem; }
.mistakes { margin: 0; padding-left: 0; list-style: none; }
.mistakes li { margin: 6px 0; padding: 8px 10px; border-radius: 8px; background: #d8453a1a; }
.mistakes li::before { content: "✕ "; color: var(--warn); font-weight: 700; }
.checklist { margin: 0; padding: 0; list-style: none; }
.checklist li { margin: 4px 0; }
.checklist label { display:flex; gap: 8px; align-items: baseline; padding: 6px 8px; border-radius: 8px; cursor: pointer; }
.checklist label:hover { background: var(--bg); }
.checklist input { flex: none; transform: translateY(2px); }
.checklist .role { flex: none; white-space: nowrap; margin-right: 0; }
.checklist input:checked ~ * { color: var(--muted); }
.checklist .kind { font-size: .72rem; font-weight: 600; border: 1px solid var(--line); border-radius: 4px;
                   padding: 0 5px; flex: none; }
.sheet { background: var(--card); border:1px solid var(--line); border-radius: 12px; padding: 12px 18px; margin: 24px 0; }
.sheet-head { display:flex; justify-content: space-between; align-items: center; }
.sheet .seq { font-weight: 600; } .sheet ol { padding-left: 22px; } .sheet ol ul { list-style: none; padding-left: 8px; margin: 2px 0 8px; }
.print-only { display: none; }
@media print {
  body.print-sheet main > *:not(#learn) { display: none !important; }
  body.print-sheet #learn > *:not(#sheet) { display: none !important; }
  body.print-sheet #sheet { border: none; margin: 0; padding: 0; }
  body.print-sheet #print-sheet { display: none; }
  body.print-sheet .print-only { display: block; }
  body { background: #fff; color: #000; }
}
.note { font-size: .85rem; color: var(--muted); border-top: 1px solid var(--line); margin-top: 28px; padding-top: 12px; }
"""

_JS = """
const v = document.getElementById('video');
let stopAt = null;
function play(s, e) { v.currentTime = s; stopAt = e; v.play(); window.scrollTo({top: 0, behavior: 'smooth'}); }
document.querySelectorAll('.play, .timeline div').forEach(b => b.addEventListener('click', () =>
  play(parseFloat(b.dataset.s), parseFloat(b.dataset.e))));
document.querySelectorAll('.jump, .jump-img').forEach(b => b.addEventListener('click', () => {
  const t = parseFloat(b.dataset.t); play(Math.max(0, t - 1.0), t + 1.0); }));
const cursor = document.querySelector('.cursor');
v.addEventListener('timeupdate', () => {
  if (stopAt !== null && v.currentTime >= stopAt) { v.pause(); stopAt = null; }
  if (cursor && v.duration) cursor.style.left = (100 * v.currentTime / v.duration) + '%';
});
v.addEventListener('seeking', () => { if (v.paused) stopAt = null; });

// localStorage can be missing or throw (private mode, file:// in some browsers): the page must work without it
const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, val) { try { val === null ? localStorage.removeItem(k) : localStorage.setItem(k, val); } catch (e) {} },
};
const tabs = document.querySelectorAll('.tabs button');
function showTab(name) {
  tabs.forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.panel').forEach(p => p.hidden = p.id !== name);
  store.set('sg.tab', name);
}
tabs.forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
if (tabs.length) showTab(store.get('sg.tab') === 'review' ? 'review' : 'learn');

// checklist ticks are keyed by figure item id, so they carry over to other videos with the same figure
const boxes = document.querySelectorAll('.checklist input');
function refreshProgress() {
  document.querySelectorAll('.card.learn').forEach(card => {
    const all = card.querySelectorAll('.checklist input');
    const done = card.querySelectorAll('.checklist input:checked').length;
    const el = card.querySelector('.progress');
    if (el) el.textContent = all.length ? `${done}/${all.length}` : '';
  });
}
boxes.forEach(b => {
  b.checked = store.get('sg.check.' + b.dataset.id) === '1';
  b.addEventListener('change', () => {
    store.set('sg.check.' + b.dataset.id, b.checked ? '1' : null);
    boxes.forEach(o => { if (o.dataset.id === b.dataset.id) o.checked = b.checked; });
    refreshProgress();
  });
});
refreshProgress();
const printBtn = document.getElementById('print-sheet');
if (printBtn) printBtn.addEventListener('click', () => {
  document.body.classList.add('print-sheet');
  window.print();
  document.body.classList.remove('print-sheet');
});
"""


def write_report(
    analysis: Analysis, video: Path, out_dir: Path, *, video_src: str, title: str,
    source_url: str | None = None, sequence_note: str | None = None,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = float(analysis.track.t[-1] + 1.0 / analysis.track.fps)
    colors = [PALETTE[i % len(PALETTE)] for i in range(len(analysis.figures))]

    bars = []
    for fa, color in zip(analysis.figures, colors):
        s, e = fa.segment.start_t, fa.segment.end_t
        left, width = 100 * s / duration, 100 * max(e - s, 0.01) / duration
        cls = "warn" if fa.warnings else ""
        bars.append(f"<div class='{cls}' style='left:{left:.3f}%;width:{width:.3f}%;background:{color}' "
                    f"data-s='{s:.2f}' data-e='{e:.2f}' title='{html.escape(fa.segment.item.label)}'>"
                    f"{fa.segment.index + 1}. {html.escape(fa.segment.item.label)}</div>")

    order = []
    for fa in analysis.figures:
        cautions = fa.segment.item.figure.cautions_for(analysis.role)
        key = cautions[0].text if cautions else fa.segment.item.figure.summary
        warn = (f" <span class='badge warn'>{html.escape(fa.warnings[0].label)}</span>" if fa.warnings else "")
        order.append(f"<li><a href='#fig-{fa.segment.index + 1}'>{html.escape(fa.segment.item.label)}</a>"
                     f" <span class='muted small'>{fmt_t(fa.segment.start_t)}</span> — {html.escape(key)}{warn}</li>")

    cards = "\n".join(_figure_card(fa, analysis, video, c) for fa, c in zip(analysis.figures, colors))
    learn_cards = "\n".join(_learn_card(fa, analysis, c) for fa, c in zip(analysis.figures, colors))
    prereqs = _prerequisites(analysis)
    prereq_note = ("<p class='prereq'><b>미리 할 줄 알면 좋은 것:</b> {}</p>".format(
        ", ".join(html.escape(name) for name in prereqs)) if prereqs else "")
    low_conf = sum(fa.segment.confidence == "low" for fa in analysis.figures)
    split_note = (f"<p><b>{low_conf}개 피구라의 자동 분할이 부정확할 수 있어요.</b> 타임라인을 보고 시작 시간을 "
                  "<code>피구라 @0:12</code>처럼 지정해 다시 실행하면 정확해집니다.</p>" if low_conf else "")
    source_bits = []
    if source_url:
        source_bits.append(f"원본: <a href='{html.escape(source_url)}' target='_blank' rel='noopener'>"
                           f"{html.escape(source_url)}</a>")
    if sequence_note:
        source_bits.append(f"시퀀스는 {html.escape(sequence_note)}에서 읽었어요 — 틀리면 -s로 고쳐 다시 실행하세요")
    source_line = f"<p class='muted small'>{' · '.join(source_bits)}</p>" if source_bits else ""
    role_text = {"all": "리더·팔로워 공통", "leader": "리더", "follower": "팔로워"}[analysis.role]

    page = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_CSS}</style></head>
<body><main>
<h1>{html.escape(title)}</h1>
<p class="muted">{len(analysis.figures)}개 피구라 · 스텝 {len(analysis.steps)}개 감지 · {role_text} 관점 ·
{'측면' if analysis.view == 'side' else '정면'} 촬영</p>
{source_line}
<video id="video" src="{html.escape(video_src)}" controls playsinline preload="metadata"></video>
<div class="timeline">{''.join(bars)}<span class="cursor"></span></div>
<p class="muted small">타임라인이나 ▶ 버튼을 누르면 그 구간만 재생됩니다.</p>
<nav class="tabs"><button data-tab="learn">배우기</button><button data-tab="review">점검</button></nav>
<div class="panel" id="learn">
<p class="muted small">피구라마다 핵심 한 줄과 연습 체크리스트예요. 체크한 항목은 이 브라우저에 저장됩니다.</p>
{prereq_note}
{learn_cards}
{_summary_sheet(analysis, title)}
</div>
<div class="panel" id="review" hidden>
<p class="muted small">영상 속 사람의 자세를 자동으로 점검한 결과예요. 내 연습 영상을 분석했을 때 보세요.</p>
{split_note}
<div class="order"><b>배우는 순서</b><ol>{''.join(order)}</ol></div>
{cards}
</div>
<div class="note">
주의점은 피구라별 일반 교수법 초안이고, 자동 체크는 한 대의 카메라로 본 2D 근사입니다 — 몸이 카메라와 비스듬하면
실제보다 작게 측정됩니다. 판단이 애매하면 강사의 피드백을 우선하세요. 피구라별 주의점과 기준값은
<code>figures.yaml</code>에서 고칠 수 있습니다.
</div>
</main>
<script>{_JS}</script>
</body></html>"""
    path = out_dir / "index.html"
    path.write_text(page, encoding="utf-8")
    (out_dir / "result.json").write_text(
        json.dumps({**to_json(analysis, video.name), "source_url": source_url, "sequence_source": sequence_note},
                   ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def text_guide(items, role: str = "all") -> str:
    """Video-free study guide for a sequence (the `sequenceguider guide` command)."""
    lines = []
    for n, it in enumerate(items, start=1):
        fig = it.figure
        lines.append(f"{n}. {it.label} ({fig.name_es}) — {fig.summary}")
        if fig.key_point:
            lines.append(f"   ★ {fig.key_point}")
        for c in fig.cautions_for(role):
            tag = "" if c.role == "all" else f"[{ROLE_KO[c.role]}] "
            lines.append(f"   · {tag}{c.text}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
