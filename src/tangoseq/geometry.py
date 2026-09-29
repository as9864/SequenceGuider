"""Vectorized 2D body geometry over a PoseTrack (all arrays (T,), NaN = unknown).

Single-camera 2D: angles are measured in the image plane with image
vertical as gravity, so a level camera matters. Sagittal quantities
(forward lean, pelvis ahead/behind) only make sense from a side view.
"""

import warnings

import numpy as np

from tangoseq.pose.base import J, PoseTrack

CONF = 0.5


def point(track: PoseTrack, name: str) -> np.ndarray:
    """(T, 2) joint positions with NaN where confidence is low."""
    p = track.joint(name).astype(float).copy()
    p[track.conf[:, J[name]] < CONF] = np.nan
    return p


def mid(track: PoseTrack, a: str, b: str) -> np.ndarray:
    return (point(track, a) + point(track, b)) / 2


def shoulder_mid(track: PoseTrack) -> np.ndarray:
    return mid(track, "shoulder_l", "shoulder_r")


def hip_mid(track: PoseTrack) -> np.ndarray:
    return mid(track, "hip_l", "hip_r")


def torso_length(track: PoseTrack) -> np.ndarray:
    return np.linalg.norm(shoulder_mid(track) - hip_mid(track), axis=1)


def reference_torso_length(track: PoseTrack) -> float:
    """Median torso length over the video — the unit for distances, so
    thresholds don't depend on camera distance or resolution."""
    tl = torso_length(track)
    tl = tl[np.isfinite(tl) & (tl > 1)]
    if tl.size == 0:
        raise ValueError("상체(어깨·골반)가 한 번도 인식되지 않았습니다 — 전신이 나오는 영상인지 확인하세요")
    return float(np.median(tl))


def angle_from_vertical(v: np.ndarray) -> np.ndarray:
    """Signed angle (deg) of image vectors v (T,2) from image-up, + toward image-right."""
    return np.degrees(np.arctan2(v[:, 0], -v[:, 1]))


def facing(track: PoseTrack) -> np.ndarray:
    """+1 facing image-right, -1 facing image-left, NaN if not in profile.

    In profile the nose sits ahead of the ears. Frames where the nose is
    within a small fraction of torso length of the ears (facing the camera
    or away) are left undetermined. The sign is then held across short
    gaps: dancers don't flip facing between two frames.
    """
    nose = point(track, "nose")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        ears = np.nanmean(np.stack([point(track, "ear_l"), point(track, "ear_r")]), axis=0)
    dx = nose[:, 0] - ears[:, 0]
    tl = torso_length(track)
    out = np.where(np.abs(dx) > 0.08 * tl, np.sign(dx), np.nan)
    # hold last known facing for up to ~0.5s
    hold = max(1, int(round(track.fps * 0.5)))
    last, age = np.nan, hold + 1
    for i in range(len(out)):
        if np.isfinite(out[i]):
            last, age = out[i], 0
        else:
            age += 1
            if age <= hold:
                out[i] = last
    return out


def support_side(track: PoseTrack) -> np.ndarray:
    """0 = left, 1 = right, 2 = both (feet together), -1 unknown — per frame.

    Weight sits over the foot whose ankle is horizontally closest to the
    hip midpoint; feet closer than a quarter torso length count as both.
    """
    hip = hip_mid(track)
    al, ar = point(track, "ankle_l"), point(track, "ankle_r")
    tl = torso_length(track)
    dl = np.abs(al[:, 0] - hip[:, 0])
    dr = np.abs(ar[:, 0] - hip[:, 0])
    spread = np.abs(al[:, 0] - ar[:, 0])
    out = np.where(dl <= dr, 0, 1)
    out = np.where(np.isnan(dl) & ~np.isnan(dr), 1, out)
    out = np.where(np.isnan(dr) & ~np.isnan(dl), 0, out)
    out = np.where(spread < 0.25 * tl, 2, out)
    out = np.where(np.isnan(dl) & np.isnan(dr), -1, out)
    return out.astype(int)


def support_point(track: PoseTrack, name: str = "ankle") -> np.ndarray:
    """(T,2) support-foot point (ankle midpoint when both feet carry weight)."""
    side = support_side(track)
    l, r = point(track, f"{name}_l"), point(track, f"{name}_r")
    both = (l + r) / 2
    out = np.full_like(l, np.nan)
    out[side == 0] = l[side == 0]
    out[side == 1] = r[side == 1]
    out[side == 2] = both[side == 2]
    return out


def joint_angle(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Angle at b (deg) between b->a and b->c, per frame."""
    v1, v2 = a - b, c - b
    cos = np.sum(v1 * v2, axis=1) / (np.linalg.norm(v1, axis=1) * np.linalg.norm(v2, axis=1))
    return np.degrees(np.arccos(np.clip(cos, -1.0, 1.0)))


def moving_average(x: np.ndarray, window: int) -> np.ndarray:
    """NaN-aware centered moving average along axis 0."""
    if window <= 1:
        return x
    x = np.asarray(x, dtype=float)
    finite = np.isfinite(x)
    filled = np.where(finite, x, 0.0)
    kernel = np.ones(window)
    conv = lambda a: np.apply_along_axis(lambda s: np.convolve(s, kernel, mode="same"), 0, a)  # noqa: E731
    num, den = conv(filled), conv(finite.astype(float))
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0, num / den, np.nan)
