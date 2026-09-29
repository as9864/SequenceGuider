"""MediaPipe PoseLandmarker backend (Apache-2.0).

Chosen over COCO-17 models because BlazePose's 33 landmarks include heel
and toe points — step detection and "which foot carries the weight" need
feet, not just ankles. Runs fast enough on CPU for offline analysis.

The model file is downloaded once to ~/.cache/tangoseq/.
"""

import urllib.request
from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np

from tangoseq.pose.base import JOINTS, PoseTrack

MODEL_URLS = {
    "lite": "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task",
    "full": "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task",
    "heavy": "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_heavy/float16/latest/pose_landmarker_heavy.task",
}
CACHE_DIR = Path.home() / ".cache" / "tangoseq"

# BlazePose landmark index for each of our joints
_BLAZEPOSE_INDEX = {
    "nose": 0, "ear_l": 7, "ear_r": 8, "shoulder_l": 11, "shoulder_r": 12,
    "elbow_l": 13, "elbow_r": 14, "wrist_l": 15, "wrist_r": 16,
    "hip_l": 23, "hip_r": 24, "knee_l": 25, "knee_r": 26,
    "ankle_l": 27, "ankle_r": 28, "heel_l": 29, "heel_r": 30,
    "foot_index_l": 31, "foot_index_r": 32,
}
_INDEX = np.array([_BLAZEPOSE_INDEX[n] for n in JOINTS])


def ensure_model(variant: str = "full") -> Path:
    if variant not in MODEL_URLS:
        raise ValueError(f"unknown model variant {variant!r} (lite | full | heavy)")
    path = CACHE_DIR / f"pose_landmarker_{variant}.task"
    if not path.exists():
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(MODEL_URLS[variant], tmp)
        tmp.rename(path)
    return path


def _pick_person(landmark_sets: list, person: str) -> int:
    """Index of the pose to track when several people are in frame.

    'left'/'right': by hip x position — for couple footage, pick which
    dancer to coach. 'largest': the most spread-out skeleton (closest).
    """
    if len(landmark_sets) == 1:
        return 0
    if person in ("left", "right"):
        xs = [np.mean([lm[23].x, lm[24].x]) for lm in landmark_sets]
        return int(np.argmin(xs) if person == "left" else np.argmax(xs))
    heights = [max(p.y for p in lm) - min(p.y for p in lm) for lm in landmark_sets]
    return int(np.argmax(heights))


def extract_pose(
    video_path: Path,
    *,
    variant: str = "full",
    person: str = "largest",
    max_long_edge: int | None = 960,
    progress: Callable[[int, int], None] | None = None,
) -> PoseTrack:
    from mediapipe import Image, ImageFormat
    from mediapipe.tasks.python import BaseOptions
    from mediapipe.tasks.python.vision import PoseLandmarker, PoseLandmarkerOptions, RunningMode

    options = PoseLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(ensure_model(variant))),
        running_mode=RunningMode.VIDEO,
        num_poses=1 if person == "largest" else 2,
        min_pose_detection_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"영상을 열 수 없습니다: {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    ts, xys, confs = [], [], []
    last_ms = -1
    with PoseLandmarker.create_from_options(options) as landmarker:
        i = 0
        while True:
            ok, bgr = cap.read()
            if not ok:
                break
            t = i / fps
            ms = max(int(round(t * 1000)), last_ms + 1)  # VIDEO mode needs strictly increasing ms
            last_ms = ms
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            if max_long_edge and max(rgb.shape[:2]) > max_long_edge:
                s = max_long_edge / max(rgb.shape[:2])
                rgb = cv2.resize(rgb, (round(rgb.shape[1] * s), round(rgb.shape[0] * s)), interpolation=cv2.INTER_AREA)
            result = landmarker.detect_for_video(Image(image_format=ImageFormat.SRGB, data=rgb), ms)

            xy = np.zeros((len(JOINTS), 2))
            conf = np.zeros(len(JOINTS))
            if result.pose_landmarks:
                lm = result.pose_landmarks[_pick_person(result.pose_landmarks, person)]
                pts = np.array([[p.x, p.y, p.visibility or 0.0, p.presence or 0.0] for p in lm])[_INDEX]
                xy = pts[:, :2] * [width, height]  # normalized -> source pixels
                conf = np.clip(np.minimum(pts[:, 2], pts[:, 3]) if pts[:, 3].any() else pts[:, 2], 0, 1)
            ts.append(t)
            xys.append(xy)
            confs.append(conf)
            i += 1
            if progress is not None:
                progress(i, total)
    cap.release()
    if not ts:
        raise RuntimeError(f"영상에서 프레임을 읽지 못했습니다: {video_path}")
    return PoseTrack(
        t=np.array(ts), xy=np.stack(xys), conf=np.stack(confs), fps=fps,
        image_size=(width, height), backend=f"mediapipe-pose-{variant}",
    )
