"""Drawing: skeletons, keyframe stills, and the captioned overlay video.

Korean captions use OpenCV 5's built-in Unicode font (covers Hangul); the
overlay is encoded as H.264 via PyAV so browsers can play it inline.
"""

import base64
from collections.abc import Callable
from pathlib import Path

import av
import cv2
import numpy as np

from sequenceguider import geometry as g
from sequenceguider.analyze import Analysis, FigureAnalysis
from sequenceguider.pose.base import BONES, J, PoseTrack

OK = (70, 190, 90)  # RGB
WARN = (230, 70, 55)
BONE = (90, 210, 255)
WHITE = (255, 255, 255)

try:
    _FONT = cv2.FontFace("uni")
except (AttributeError, cv2.error):  # OpenCV < 5: ASCII-only fallback
    _FONT = None


def put_text(img: np.ndarray, text: str, org: tuple[int, int], color, size: int) -> int:
    x, y = int(org[0]), int(org[1])
    if _FONT is not None:
        end, _ = cv2.putText(img, text, (x, y), color, _FONT, size)
        return int(end[0])
    text = text.encode("ascii", "replace").decode()
    cv2.putText(img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, size / 30, color, 2, cv2.LINE_AA)
    return x + int(len(text) * size * 0.55)


def text_width(text: str, size: int) -> int:
    scratch = np.zeros((size * 2, size * (len(text) + 2), 3), np.uint8)
    return put_text(scratch, text, (0, int(size * 1.4)), WHITE, size)


def shade(img: np.ndarray, x0: int, y0: int, x1: int, y1: int, color=(0, 0, 0), alpha: float = 0.55) -> None:
    h, w = img.shape[:2]
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if x1 > x0 and y1 > y0:
        roi = img[y0:y1, x0:x1]
        roi[:] = (roi * (1 - alpha) + np.array(color) * alpha).astype(np.uint8)


def draw_skeleton(img: np.ndarray, track: PoseTrack, i: int, scale: float = 1.0, thickness: int = 2) -> None:
    valid = track.conf[i] >= g.CONF
    pts = track.xy[i] * scale
    for a, b in BONES:
        if valid[J[a]] and valid[J[b]]:
            cv2.line(img, tuple(np.round(pts[J[a]]).astype(int)), tuple(np.round(pts[J[b]]).astype(int)),
                     BONE, thickness, cv2.LINE_AA)
    for k in np.flatnonzero(valid):
        cv2.circle(img, tuple(np.round(pts[k]).astype(int)), thickness + 1, WHITE, -1, cv2.LINE_AA)


def draw_body_line(img: np.ndarray, track: PoseTrack, i: int, scale: float, ok: bool) -> None:
    """Support foot -> hip -> shoulders polyline, plus a true vertical from the
    foot: the picture that makes '한 선' and '축' visible."""
    sup = g.support_point(track)[i]
    hip = g.hip_mid(track)[i]
    sh = g.shoulder_mid(track)[i]
    if not np.all(np.isfinite([*sup, *hip, *sh])):
        return
    p = [tuple(np.round(v * scale).astype(int)) for v in (sup, hip, sh)]
    color = OK if ok else WARN
    cv2.line(img, p[0], p[1], color, 3, cv2.LINE_AA)
    cv2.line(img, p[1], p[2], color, 3, cv2.LINE_AA)
    height = int(max(40, (p[0][1] - p[2][1]) * 1.15))
    cv2.line(img, p[0], (p[0][0], p[0][1] - height), (220, 220, 220), 1, cv2.LINE_AA)


def read_frame(video: Path, index: int) -> np.ndarray | None:
    cap = cv2.VideoCapture(str(video))
    cap.set(cv2.CAP_PROP_POS_FRAMES, index)
    ok, bgr = cap.read()
    cap.release()
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB) if ok else None


def person_crop(track: PoseTrack, index: int, frame_w: int, frame_h: int, margin: float = 0.35) -> tuple[int, int, int, int]:
    """(x0, y0, x1, y1) around the dancer at a 3:4 portrait aspect, or the full
    frame when too few joints are visible — a small figure in a wide studio
    shot is useless as a posture example."""
    valid = track.conf[index] >= g.CONF
    if valid.sum() < 6:
        return 0, 0, frame_w, frame_h
    pts = track.xy[index][valid]
    (x0, y0), (x1, y1) = pts.min(axis=0), pts.max(axis=0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    h = max(y1 - y0, (x1 - x0) * 4 / 3) * (1 + 2 * margin)
    w = h * 3 / 4
    if w >= frame_w * 0.8 or h >= frame_h * 0.9:
        return 0, 0, frame_w, frame_h
    x0 = int(np.clip(cx - w / 2, 0, frame_w - w))
    y0 = int(np.clip(cy - h / 2, 0, frame_h - h))
    return x0, y0, int(x0 + w), int(y0 + h)


def keyframe_jpeg_b64(video: Path, track: PoseTrack, index: int, *, ok: bool, caption: str, width: int = 360) -> str | None:
    frame = read_frame(video, index)
    if frame is None:
        return None
    fh, fw = frame.shape[:2]
    sx, sy = fw / track.image_size[0], fh / track.image_size[1]  # track coords -> this frame
    x0, y0, x1, y1 = person_crop(track, index, fw, fh)
    crop = frame[y0:y1, x0:x1]
    s = width / crop.shape[1]
    img = cv2.resize(crop, (width, int(round(crop.shape[0] * s))), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
    shifted = PoseTrack(
        t=track.t[index : index + 1],
        xy=((track.xy[index : index + 1] * [sx, sy]) - [x0, y0]) * s,
        conf=track.conf[index : index + 1], fps=track.fps, image_size=track.image_size,
    )
    draw_skeleton(img, shifted, 0, thickness=2)
    draw_body_line(img, shifted, 0, 1.0, ok)
    fs = max(14, width // 20)
    shade(img, 0, img.shape[0] - int(fs * 1.9), img.shape[1], img.shape[0])
    put_text(img, caption, (8, img.shape[0] - int(fs * 0.6)), WHITE, fs)
    ok_enc, buf = cv2.imencode(".jpg", cv2.cvtColor(img, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 85])
    return base64.b64encode(buf.tobytes()).decode("ascii") if ok_enc else None


def _figure_at(analysis: Analysis, t: float) -> FigureAnalysis | None:
    for fa in analysis.figures:
        if fa.segment.start_t <= t < fa.segment.end_t:
            return fa
    return None


def _out_of_range_now(analysis: Analysis, fa: FigureAnalysis, i: int) -> list[str]:
    msgs = []
    for c in fa.warnings:
        v = analysis.series.get(c.id)
        if v is None or not np.isfinite(v[i]):
            continue
        if (c.direction == "high" and v[i] > c.bound) or (c.direction == "low" and v[i] < c.bound):
            msgs.append(c.message or c.label)
    return msgs


def render_overlay(
    video: Path, analysis: Analysis, out_path: Path, *, max_long_edge: int = 960,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Source video + skeleton + current figure caption + live warnings (H.264)."""
    track = analysis.track
    cap = cv2.VideoCapture(str(video))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or len(track)
    w, h = track.image_size
    s = min(1.0, max_long_edge / max(w, h))
    ow, oh = int(round(w * s)) // 2 * 2, int(round(h * s)) // 2 * 2
    s_x, s_y = ow / w, oh / h
    fs = max(16, oh // 26)
    n_fig = len(analysis.figures)

    container = av.open(str(out_path), mode="w")
    stream = container.add_stream("libx264", rate=round(track.fps))
    stream.width, stream.height, stream.pix_fmt = ow, oh, "yuv420p"
    stream.options = {"crf": "23", "preset": "veryfast", "movflags": "+faststart"}
    i = 0
    while True:
        ok, bgr = cap.read()
        if not ok or i >= len(track):
            break
        img = cv2.resize(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), (ow, oh), interpolation=cv2.INTER_AREA)
        draw_skeleton(img, track, i, scale=min(s_x, s_y))
        fa = _figure_at(analysis, float(track.t[i]))
        if fa is not None:
            warn_now = _out_of_range_now(analysis, fa, i)
            draw_body_line(img, track, i, min(s_x, s_y), ok=not warn_now)
            title = f"{fa.segment.index + 1}/{n_fig}  {fa.segment.item.label}"
            shade(img, 0, 0, ow, int(fs * 1.9))
            put_text(img, title, (12, int(fs * 1.35)), WHITE, fs)
            if warn_now:
                msg = warn_now[0]
                tw = min(ow - 16, text_width(msg, fs))
                shade(img, 8, oh - int(fs * 2.1), 8 + tw + 24, oh - 8, WARN, 0.8)
                put_text(img, msg, (20, oh - int(fs * 0.75)), WHITE, fs)
            else:
                cautions = fa.segment.item.figure.cautions_for(analysis.role)
                if cautions:
                    tip = cautions[0].text
                    shade(img, 0, oh - int(fs * 2.0), ow, oh)
                    put_text(img, tip, (12, oh - int(fs * 0.7)), (235, 235, 235), int(fs * 0.85))
        frame = av.VideoFrame.from_ndarray(img, format="rgb24")
        for packet in stream.encode(frame):
            container.mux(packet)
        i += 1
        if progress is not None:
            progress(i, total)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    cap.release()
    return out_path
