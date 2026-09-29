"""Split the video into one segment per figure of the given sequence.

There's no labelled tango data to train a figure recognizer on, so the
dancer supplies the ORDER and this module supplies the TIMING: detected
steps are shared out between figures in proportion to how many steps each
figure usually takes (figures.yaml `steps` × repeat count). Anchors
("@1:23") pin a figure's start and split the problem into independent
blocks, which is how the user corrects a bad automatic split.

Each segment reports how far its detected step count strayed from the
expected one — a large mismatch means "check this split, add an anchor".
"""

from dataclasses import dataclass

import numpy as np

from tangoseq.figures import SequenceItem
from tangoseq.steps import Step


@dataclass
class Segment:
    item: SequenceItem
    index: int
    start_t: float
    end_t: float
    steps: list[Step]
    anchored: bool

    @property
    def mismatch(self) -> float:
        """|detected - expected| / expected — 0 is a perfect match."""
        exp = self.item.expected_steps
        return abs(len(self.steps) - exp) / exp

    @property
    def confidence(self) -> str:
        if self.anchored and self.mismatch <= 0.5:
            return "high"
        if self.mismatch <= 0.34:
            return "high"
        return "medium" if self.mismatch <= 0.75 else "low"


def _split_counts(n: int, weights: list[int]) -> list[int]:
    """Share n steps between items proportionally to weights; every item gets
    at least one step when there are enough to go around."""
    k = len(weights)
    total = sum(weights)
    bounds = [round(n * sum(weights[: i + 1]) / total) for i in range(k)]
    counts = [bounds[0]] + [bounds[i] - bounds[i - 1] for i in range(1, k)]
    if n >= k:
        # borrow from the largest to fill any empty item
        for i in range(k):
            while counts[i] == 0:
                j = int(np.argmax(counts))
                counts[j] -= 1
                counts[i] += 1
    return counts


def _align_block(items: list[SequenceItem], steps: list[Step], start_t: float, end_t: float) -> list[tuple]:
    """(start_t, end_t, steps) per item within [start_t, end_t)."""
    weights = [it.expected_steps for it in items]
    if not steps:
        # no motion detected: split time by expected length
        total = sum(weights)
        edges = [start_t + (end_t - start_t) * sum(weights[:i]) / total for i in range(len(items) + 1)]
        return [(edges[i], edges[i + 1], []) for i in range(len(items))]

    counts = _split_counts(len(steps), weights)
    groups, pos = [], 0
    for c in counts:
        groups.append(steps[pos : pos + c])
        pos += c

    out = []
    for i, group in enumerate(groups):
        seg_start = start_t if i == 0 else None
        if seg_start is None:
            # boundary halfway between the previous figure's last step and this one's first
            prev = next((g for g in reversed(groups[:i]) if g), None)
            if group and prev:
                seg_start = (prev[-1].end_t + group[0].start_t) / 2
            elif group:
                seg_start = group[0].start_t
            else:
                seg_start = out[-1][1] if out else start_t
        out.append([seg_start, None, group])
    for i in range(len(out)):
        out[i][1] = out[i + 1][0] if i + 1 < len(out) else end_t
        if out[i][1] <= out[i][0]:  # empty group squeezed to nothing: give it a sliver
            out[i][1] = out[i][0] + 1e-3
    return [tuple(o) for o in out]


def align_sequence(items: list[SequenceItem], steps: list[Step], duration_s: float) -> list[Segment]:
    # Blocks start at the video start and at every anchored item.
    block_starts = [0] + [i for i, it in enumerate(items) if it.anchor_s is not None and i > 0]
    segments: list[Segment] = []
    for b, first in enumerate(block_starts):
        last = block_starts[b + 1] if b + 1 < len(block_starts) else len(items)
        block_items = items[first:last]
        start_t = block_items[0].anchor_s if block_items[0].anchor_s is not None else 0.0
        end_t = items[last].anchor_s if last < len(items) else duration_s
        if block_items[0].anchor_s is None and not segments:
            # unanchored first block: start at the first detected step, not second 0 of the video
            in_block = [s for s in steps if s.start_t < end_t]
            if in_block:
                start_t = max(0.0, in_block[0].start_t - 0.3)
        block_steps = [s for s in steps if start_t <= s.mid_t < end_t]
        if last == len(items) and block_steps:
            # don't let the final figure absorb trailing chatter/standing around
            end_t = min(end_t, max(s.end_t for s in block_steps) + 1.0)
        for j, (s, e, group) in enumerate(_align_block(block_items, block_steps, start_t, end_t)):
            item = block_items[j]
            segments.append(
                Segment(item=item, index=len(segments), start_t=float(s), end_t=float(e), steps=list(group),
                        anchored=item.anchor_s is not None)
            )
    return segments
