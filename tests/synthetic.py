"""Synthetic side-view dancer with exactly known steps and posture.

A known number of steps at known times, a known torso lean, a known pelvis
offset — so step detection, alignment and checks can be tested for
"reads back what was put in", no video or model required.
"""

import numpy as np

from sequenceguider.pose.base import JOINTS, J, PoseTrack

TORSO = 200.0  # px, hip_mid -> shoulder_mid


def _up(angle_deg: float, length: float) -> np.ndarray:
    a = np.radians(angle_deg)
    return np.array([np.sin(a), -np.cos(a)]) * length


def walk(
    n_steps: int = 8,
    *,
    fps: float = 30.0,
    period: float = 0.8,
    swing: float = 0.35,
    lead_in: float = 1.0,
    tail: float = 1.0,
    step_len: float = 0.5,  # torso lengths per foot move
    lean_deg=0.0,  # float or callable(t) -> deg, + forward
    pelvis_ahead: float = 0.0,  # torso lengths, pelvis in front of the shoulders line
    facing: int = 1,
    step_times: list[float] | None = None,
    tilt_deg: float = 0.0,  # whole body tilted as one straight plank about the feet (off-axis), + forward
) -> PoseTrack:
    times = step_times if step_times is not None else [lead_in + k * period for k in range(n_steps)]
    duration = (times[-1] + swing + tail) if times else lead_in + tail
    t = np.arange(0.0, duration, 1.0 / fps)
    feet = {"l": np.zeros(len(t)), "r": np.zeros(len(t))}
    pos = {"l": 0.0, "r": 0.0}
    L = step_len * TORSO
    events = []
    for k, t0 in enumerate(times):
        side = "l" if k % 2 == 0 else "r"
        other = "r" if side == "l" else "l"
        target = pos[other] + L  # pass the planted foot
        events.append((t0, side, pos[side], target))
        pos[side] = target
    for side in ("l", "r"):
        x = np.zeros(len(t))
        for t0, s, a, b in events:
            if s != side:
                continue
            u = np.clip((t - t0) / swing, 0.0, 1.0)
            u = 0.5 - 0.5 * np.cos(np.pi * u)  # smooth start/stop
            x = np.where(t >= t0, a + (b - a) * u, x)
        feet[side] = x

    xy = np.zeros((len(t), len(JOINTS), 2))
    base_x, floor_y = 300.0, 700.0
    for i, ti in enumerate(t):
        fl = base_x + facing * feet["l"][i]
        fr = base_x + facing * feet["r"][i]
        hip = np.array([(fl + fr) / 2, floor_y - 2.2 * TORSO])
        lean = lean_deg(ti) if callable(lean_deg) else lean_deg
        shoulder = hip + _up(facing * lean, TORSO) - np.array([facing * pelvis_ahead * TORSO, 0.0])
        ear = shoulder + np.array([0.0, -0.35 * TORSO])
        p = {
            "hip_l": hip, "hip_r": hip, "shoulder_l": shoulder, "shoulder_r": shoulder,
            "ear_l": ear, "ear_r": ear, "nose": ear + np.array([facing * 25.0, 5.0]),
            "elbow_l": shoulder + [facing * 50.0, 60.0], "elbow_r": shoulder + [facing * 50.0, 60.0],
            "wrist_l": shoulder + [facing * 100.0, 40.0], "wrist_r": shoulder + [facing * 100.0, 40.0],
        }
        for side, fx in (("l", fl), ("r", fr)):
            ankle = np.array([fx, floor_y])
            p[f"ankle_{side}"] = ankle
            p[f"knee_{side}"] = (hip + ankle) / 2 + [facing * 6.0, 0.0]  # ~174° knee
            p[f"heel_{side}"] = ankle + [-facing * 15.0, 8.0]
            p[f"foot_index_{side}"] = ankle + [facing * 40.0, 10.0]
        pts = np.array([p[n] for n in JOINTS])
        if tilt_deg:
            pivot = np.array([hip[0], floor_y])
            a = np.radians(facing * tilt_deg)
            rot = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])  # clockwise on screen (y down)
            pts = pivot + (pts - pivot) @ rot.T
        xy[i] = pts
    conf = np.full((len(t), len(JOINTS)), 0.95)
    return PoseTrack(t=t, xy=xy, conf=conf, fps=fps, image_size=(1280, 720), backend="synthetic")


def steps_at(track: PoseTrack) -> None:  # pragma: no cover - debugging helper
    from sequenceguider.steps import detect_steps

    for s in detect_steps(track):
        print(s)


__all__ = ["walk", "TORSO", "J"]
