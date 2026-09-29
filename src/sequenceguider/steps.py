"""Step (weight change) detection from foot motion.

A tango step is one foot travelling and landing. Each foot is tracked
separately: its speed (in torso lengths per second, so camera distance
doesn't matter) switches a hysteresis "moving" state on and off, and a
moving interval that actually covered some ground counts as one step.
Pivots (ochos, giros) rotate on a planted foot and barely translate it,
so they don't create spurious steps.
"""

import warnings
from dataclasses import dataclass

import numpy as np

from sequenceguider.geometry import point, moving_average, reference_torso_length
from sequenceguider.pose.base import PoseTrack


@dataclass(frozen=True)
class Step:
    foot: str  # "l" | "r"
    start_idx: int
    end_idx: int
    start_t: float
    end_t: float
    distance: float  # travelled, in torso lengths

    @property
    def mid_t(self) -> float:
        return (self.start_t + self.end_t) / 2


def foot_position(track: PoseTrack, side: str) -> np.ndarray:
    """Mean of ankle/heel/toe (whichever are visible) — steadier than the ankle alone."""
    pts = np.stack([point(track, f"{j}_{side}") for j in ("ankle", "heel", "foot_index")])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN frames -> NaN, expected
        return np.nanmean(pts, axis=0)


def foot_speed(track: PoseTrack, side: str, torso: float) -> np.ndarray:
    pos = moving_average(foot_position(track, side), max(1, int(round(track.fps * 0.1))) | 1)
    dt = np.gradient(track.t)
    vel = np.gradient(pos, axis=0) / dt[:, None]
    return np.linalg.norm(vel, axis=1) / torso


def detect_steps(
    track: PoseTrack,
    *,
    speed_on: float = 0.9,
    speed_off: float = 0.35,
    min_duration_s: float = 0.12,
    min_distance: float = 0.2,
    merge_gap_s: float = 0.08,
) -> list[Step]:
    torso = reference_torso_length(track)
    steps: list[Step] = []
    for side in ("l", "r"):
        speed = foot_speed(track, side, torso)
        pos = foot_position(track, side)
        intervals: list[list[int]] = []
        moving, start = False, 0
        for i, v in enumerate(speed):
            if not np.isfinite(v):
                v = 0.0
            if not moving and v >= speed_on:
                moving, start = True, i
                # walk back to where the foot started accelerating
                while start > 0 and np.isfinite(speed[start - 1]) and speed[start - 1] > speed_off:
                    start -= 1
            elif moving and v < speed_off:
                moving = False
                intervals.append([start, i])
        if moving:
            intervals.append([start, len(speed) - 1])

        merged: list[list[int]] = []
        for s, e in intervals:
            if merged and track.t[s] - track.t[merged[-1][1]] <= merge_gap_s:
                merged[-1][1] = e
            else:
                merged.append([s, e])

        for s, e in merged:
            duration = track.t[e] - track.t[s]
            a, b = pos[s], pos[e]
            dist = float(np.linalg.norm(b - a) / torso) if np.all(np.isfinite([*a, *b])) else 0.0
            if duration >= min_duration_s and dist >= min_distance:
                steps.append(Step(side, s, e, float(track.t[s]), float(track.t[e]), dist))
    return sorted(steps, key=lambda st: st.start_t)
