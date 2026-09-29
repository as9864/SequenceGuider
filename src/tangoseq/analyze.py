"""PoseTrack + sequence -> per-figure segments with check results."""

from dataclasses import dataclass, field

import numpy as np

from tangoseq.align import Segment, align_sequence
from tangoseq.checks import CheckResult, View, evaluate
from tangoseq.figures import SequenceItem
from tangoseq.geometry import reference_torso_length
from tangoseq.pose.base import PoseTrack
from tangoseq.steps import Step, detect_steps


@dataclass
class FigureAnalysis:
    segment: Segment
    checks: list[CheckResult]
    start_idx: int
    end_idx: int  # exclusive

    @property
    def warnings(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == "warn"]


@dataclass
class Analysis:
    track: PoseTrack
    view: View
    role: str
    steps: list[Step]
    figures: list[FigureAnalysis]
    series: dict[str, np.ndarray] = field(default_factory=dict)  # per-check per-frame values
    person_coverage: float = 0.0  # share of frames where a person was found


def analyze(track: PoseTrack, items: list[SequenceItem], *, view: View = "side", role: str = "all") -> Analysis:
    torso = reference_torso_length(track)
    steps = detect_steps(track)
    duration = float(track.t[-1] + 1.0 / track.fps)
    segments = align_sequence(items, steps, duration)
    cache: dict[str, np.ndarray] = {}
    figures = []
    for seg in segments:
        s_idx, e_idx = track.index_at(seg.start_t), max(track.index_at(seg.end_t), track.index_at(seg.start_t) + 1)
        results = [evaluate(spec, track, torso, s_idx, e_idx, view, cache) for spec in seg.item.figure.checks]
        figures.append(FigureAnalysis(segment=seg, checks=results, start_idx=s_idx, end_idx=e_idx))
    coverage = float(np.mean(track.conf.max(axis=1) > 0))
    return Analysis(track=track, view=view, role=role, steps=steps, figures=figures, series=cache,
                    person_coverage=coverage)
