"""Automatic posture checks, evaluated per figure segment.

Each check turns the PoseTrack into a per-frame series; figures.yaml
decides which checks a figure uses and may override the thresholds (a
fuera de eje WANTS the body tilted, so it checks the body line instead of
the axis). A segment passes when its p90 (or p10 for a lower bound) stays
within range — "most of the figure", not the single worst jittery frame
— and the worst frame is kept as visual evidence for the report.

Default thresholds are hypotheses, like the cautions themselves.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import numpy as np

from sequenceguider import geometry as g
from sequenceguider.figures import DEFAULT, CheckSpec
from sequenceguider.pose.base import PoseTrack

View = Literal["side", "front"]
Status = Literal["ok", "warn", "unknown", "na"]


@dataclass(frozen=True)
class CheckDef:
    id: str
    label: str
    unit: str
    views: tuple[str, ...]
    compute: Callable[[PoseTrack, float], np.ndarray]  # (track, torso_px) -> (T,)
    min: float | None = None
    max: float | None = None
    agg: Literal["percentile", "min"] = "percentile"
    message_high: str = ""
    message_low: str = ""
    explain: str = ""


def _torso_lean(track: PoseTrack, torso: float) -> np.ndarray:
    return g.facing(track) * g.angle_from_vertical(g.shoulder_mid(track) - g.hip_mid(track))


def _body_line(track: PoseTrack, torso: float) -> np.ndarray:
    return 180.0 - g.joint_angle(g.shoulder_mid(track), g.hip_mid(track), g.support_point(track))


def _pelvis_offset(track: PoseTrack, torso: float) -> np.ndarray:
    """Pelvis distance in front of (+) / behind (−) the support-foot -> shoulder
    line. Independent of how far the whole body leans, so an intended
    off-axis tilt doesn't read as "hips back" — only a bend at the hips does:
    hips pushed forward (banana) > 0, seat sticking out (piking) < 0."""
    foot, hip, sh = g.support_point(track), g.hip_mid(track), g.shoulder_mid(track)
    line = sh - foot
    with np.errstate(invalid="ignore", divide="ignore"):
        line = line / np.linalg.norm(line, axis=1, keepdims=True)
    normal = np.stack([-line[:, 1], line[:, 0]], axis=1)  # perpendicular to the line
    normal *= np.sign(normal[:, 0] * g.facing(track))[:, None]  # orient toward the facing side
    return np.sum((hip - foot) * normal, axis=1) / torso


def _axis_vertical(track: PoseTrack, torso: float) -> np.ndarray:
    center = (g.shoulder_mid(track) + g.hip_mid(track)) / 2
    return np.abs(g.angle_from_vertical(center - g.support_point(track)))


def _support_knee(track: PoseTrack, torso: float) -> np.ndarray:
    side = g.support_side(track)
    angles = {s: g.joint_angle(g.point(track, f"hip_{s}"), g.point(track, f"knee_{s}"), g.point(track, f"ankle_{s}"))
              for s in ("l", "r")}
    out = np.full(len(track), np.nan)
    out[side == 0] = angles["l"][side == 0]
    out[side == 1] = angles["r"][side == 1]
    both = side == 2
    out[both] = np.fmax(angles["l"][both], angles["r"][both])
    return out


def _head_forward(track: PoseTrack, torso: float) -> np.ndarray:
    ears = np.stack([g.point(track, "ear_l"), g.point(track, "ear_r")])
    with np.errstate(invalid="ignore"):
        ear_x = np.where(np.isnan(ears[0, :, 0]), ears[1, :, 0],
                         np.where(np.isnan(ears[1, :, 0]), ears[0, :, 0], ears.mean(axis=0)[:, 0]))
    return g.facing(track) * (ear_x - g.shoulder_mid(track)[:, 0]) / torso


def _feet_distance(track: PoseTrack, torso: float) -> np.ndarray:
    return np.linalg.norm(g.point(track, "ankle_l") - g.point(track, "ankle_r"), axis=1) / torso


CHECKS: dict[str, CheckDef] = {
    c.id: c
    for c in [
        CheckDef(
            "torso_lean", "상체 전후 기울기", "deg", ("side",), _torso_lean, min=-3.0, max=8.0,
            message_high="상체가 앞으로 쏠렸어요 — 명치를 세우고 골반 위에 상체를 쌓으세요",
            message_low="상체가 뒤로 젖혀졌어요 — 가슴을 살짝 파트너 쪽으로",
            explain="골반→어깨 선이 수직에서 앞(+)/뒤(−)로 기운 각도",
        ),
        CheckDef(
            "body_line", "몸 한 선 (허리 꺾임)", "deg", ("side", "front"), _body_line, max=15.0,
            message_high="허리에서 몸이 꺾였어요 — 머리부터 지지발까지 한 선으로",
            explain="어깨–골반–지지발이 일직선에서 벗어난 각도. 0이면 완전한 한 선",
        ),
        CheckDef(
            "pelvis_offset", "골반 위치", "torso", ("side",), _pelvis_offset, min=-0.10, max=0.10,
            message_high="골반이 앞으로 밀려 나왔어요 — 골반을 뒤로, 상체와 한 선으로",
            message_low="엉덩이가 뒤로 빠졌어요 — 골반을 지지발 위로 가져오세요",
            explain="지지발–어깨를 잇는 선에서 골반이 앞(+)으로 밀리거나 뒤(−)로 빠진 거리, 상체 길이 단위",
        ),
        CheckDef(
            "axis_vertical", "축 수직성", "deg", ("side", "front"), _axis_vertical, max=6.0,
            message_high="축이 지지발에서 벗어났어요 — 체중이 실린 발 위에 몸을 쌓으세요",
            explain="지지발→몸통 중심 선이 수직에서 벗어난 각도",
        ),
        CheckDef(
            "support_knee", "지지 무릎 각도", "deg", ("side",), _support_knee, min=150.0, max=179.0,
            message_high="지지 무릎이 잠겼어요 — 무릎을 살짝 풀어주세요",
            message_low="지지 무릎이 너무 굽혀졌어요 — 바닥을 밀며 서 보세요",
            explain="지지 다리의 골반–무릎–발목 각도. 180 = 완전히 편 무릎",
        ),
        CheckDef(
            "head_forward", "머리 위치", "torso", ("side",), _head_forward, max=0.15,
            message_high="머리가 앞으로 나왔어요 — 발을 보지 말고 귀를 어깨 위로",
            explain="어깨보다 귀가 앞으로 나온 정도, 상체 길이 단위",
        ),
        CheckDef(
            "feet_collect", "발 모으기", "torso", ("side", "front"), _feet_distance, max=0.30, agg="min",
            message_high="이 구간에서 발을 한 번도 모으지 않았어요 — 스텝 사이·피벗 전에 발을 모으세요",
            explain="구간 중 두 발목이 가장 가까웠던 거리, 상체 길이 단위",
        ),
    ]
}


@dataclass
class CheckResult:
    id: str
    label: str
    unit: str
    status: Status
    value: float | None = None
    bound: float | None = None
    direction: Literal["high", "low"] | None = None
    fraction_out: float | None = None  # share of the segment spent out of range
    worst_idx: int | None = None
    worst_t: float | None = None
    message: str | None = None
    explain: str = ""
    target: str = ""


def _target_text(lo: float | None, hi: float | None, unit: str) -> str:
    u = "°" if unit == "deg" else ""
    if lo is not None and hi is not None:
        return f"{lo:g}{u} ~ {hi:g}{u}"
    return f"≤ {hi:g}{u}" if hi is not None else f"≥ {lo:g}{u}"


def evaluate(
    spec: CheckSpec, track: PoseTrack, torso_px: float, start_idx: int, end_idx: int, view: View,
    cache: dict[str, np.ndarray] | None = None,
) -> CheckResult:
    d = CHECKS.get(spec.id)
    if d is None:
        raise ValueError(f"unknown check id {spec.id!r} (known: {', '.join(CHECKS)})")
    lo = d.min if spec.min == DEFAULT else spec.min
    hi = d.max if spec.max == DEFAULT else spec.max
    base = CheckResult(d.id, d.label, d.unit, "na", explain=d.explain, target=_target_text(lo, hi, d.unit))
    if view not in d.views:
        base.message = f"{'측면' if 'side' in d.views else '정면'} 촬영에서만 측정 가능"
        return base

    if cache is not None and d.id in cache:
        series = cache[d.id]
    else:
        series = d.compute(track, torso_px)
        if cache is not None:
            cache[d.id] = series
    seg = series[start_idx:end_idx]
    finite = np.isfinite(seg)
    if seg.size < 5 or finite.mean() < 0.3:
        base.status = "unknown"
        base.message = "관절이 충분히 보이지 않아 판단 보류"
        return base
    vals = seg[finite]
    idx = np.flatnonzero(finite) + start_idx

    # (direction, measured, bound, normalized excess)
    violations: list[tuple[str, float, float, float]] = []
    if d.agg == "min":
        # "did it happen at least once" checks (e.g. feet came together)
        typical = float(vals.min())
        if hi is not None and typical > hi:
            violations.append(("high", typical, hi, (typical - hi) / max(abs(hi), 1e-6)))
        worst = int(idx[int(np.argmin(vals))])
        out = float(np.mean(vals > hi)) if hi is not None else 0.0
    else:
        typical = float(np.median(vals))
        p_hi, p_lo = float(np.percentile(vals, 90)), float(np.percentile(vals, 10))
        if hi is not None and p_hi > hi:
            violations.append(("high", p_hi, hi, (p_hi - hi) / max(abs(hi), 1.0)))
        if lo is not None and p_lo < lo:
            violations.append(("low", p_lo, lo, (lo - p_lo) / max(abs(lo), 1.0)))
        out_mask = np.zeros(vals.shape, dtype=bool)
        if hi is not None:
            out_mask |= vals > hi
        if lo is not None:
            out_mask |= vals < lo
        out = float(out_mask.mean())
        worst = None

    base.fraction_out = out
    if not violations:
        base.status = "ok"
        base.value = typical
        if worst is not None:
            base.worst_idx, base.worst_t = worst, float(track.t[worst])
        return base

    direction, value, bound, _ = max(violations, key=lambda v: v[3])
    if worst is None:
        worst = int(idx[int(np.argmax(vals))] if direction == "high" else idx[int(np.argmin(vals))])
    base.status = "warn"
    base.value, base.bound, base.direction = value, bound, direction
    base.worst_idx, base.worst_t = worst, float(track.t[worst])
    if direction == "high":
        base.message = spec.message_high or d.message_high
    else:
        base.message = spec.message_low or d.message_low
    return base
