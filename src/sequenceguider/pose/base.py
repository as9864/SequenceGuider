"""PoseTrack — one person's 2D keypoints over a whole video.

The only thing the analysis side (steps, alignment, checks, report) reads.
Backends write it; tests build it synthetically. Cached as .npz so
re-running with a different sequence or thresholds skips pose inference.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np

JOINTS: tuple[str, ...] = (
    "nose",
    "ear_l",
    "ear_r",
    "shoulder_l",
    "shoulder_r",
    "elbow_l",
    "elbow_r",
    "wrist_l",
    "wrist_r",
    "hip_l",
    "hip_r",
    "knee_l",
    "knee_r",
    "ankle_l",
    "ankle_r",
    "heel_l",
    "heel_r",
    "foot_index_l",
    "foot_index_r",
)
J = {name: i for i, name in enumerate(JOINTS)}

BONES: tuple[tuple[str, str], ...] = (
    ("shoulder_l", "shoulder_r"),
    ("shoulder_l", "elbow_l"),
    ("elbow_l", "wrist_l"),
    ("shoulder_r", "elbow_r"),
    ("elbow_r", "wrist_r"),
    ("shoulder_l", "hip_l"),
    ("shoulder_r", "hip_r"),
    ("hip_l", "hip_r"),
    ("hip_l", "knee_l"),
    ("knee_l", "ankle_l"),
    ("ankle_l", "heel_l"),
    ("heel_l", "foot_index_l"),
    ("ankle_l", "foot_index_l"),
    ("hip_r", "knee_r"),
    ("knee_r", "ankle_r"),
    ("ankle_r", "heel_r"),
    ("heel_r", "foot_index_r"),
    ("ankle_r", "foot_index_r"),
    ("nose", "ear_l"),
    ("nose", "ear_r"),
)


@dataclass
class PoseTrack:
    t: np.ndarray  # (T,) seconds
    xy: np.ndarray  # (T, J, 2) image pixels, y down
    conf: np.ndarray  # (T, J) in [0, 1]; 0 where the person wasn't found
    fps: float
    image_size: tuple[int, int]  # (width, height)
    backend: str = "unknown"

    def __post_init__(self) -> None:
        t_len = self.t.shape[0]
        if self.xy.shape != (t_len, len(JOINTS), 2) or self.conf.shape != (t_len, len(JOINTS)):
            raise ValueError(f"shape mismatch: t={self.t.shape} xy={self.xy.shape} conf={self.conf.shape}")

    def __len__(self) -> int:
        return self.t.shape[0]

    def valid(self, threshold: float = 0.5) -> np.ndarray:
        return self.conf >= threshold

    def joint(self, name: str) -> np.ndarray:
        return self.xy[:, J[name]]

    def index_at(self, t: float) -> int:
        return int(np.clip(np.searchsorted(self.t, t), 0, len(self) - 1))

    def save(self, path: Path) -> None:
        np.savez_compressed(
            path, t=self.t, xy=self.xy, conf=self.conf, fps=self.fps,
            image_size=np.array(self.image_size), backend=self.backend,
            joints=np.array(JOINTS),
        )

    @classmethod
    def load(cls, path: Path) -> "PoseTrack":
        d = np.load(path, allow_pickle=False)
        if tuple(d["joints"].tolist()) != JOINTS:
            raise ValueError(f"{path}: joint layout differs from this version — delete the cache and re-run")
        return cls(
            t=d["t"], xy=d["xy"], conf=d["conf"], fps=float(d["fps"]),
            image_size=tuple(int(v) for v in d["image_size"]), backend=str(d["backend"]),
        )
