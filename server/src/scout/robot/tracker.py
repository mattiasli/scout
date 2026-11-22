"""Keeping to one particular target among others of its class.

The detector tells a person from a cat, not one person from another. Follow remembers
what the one it follows looks like, as colour histograms of the top and bottom halves of
its box (for a person, roughly their top and their trousers) and of the whole box, which
changes less when the picture cuts them off as the car comes closer. Each frame it
takes the candidate that looks most like it, preferring the one nearest where it should
be after the car's last turn and nearest the size it was. A candidate that doesn't look
like it is never taken, and nor is one far bigger or smaller than it was moments ago (in
a home, a dark chair or a coat on a hook can be taken for a person in dark clothes), so
follow doesn't switch to something else without saying so. Two people dressed alike can
still fool it; that is what ``follow_view`` is for, where the AI watching can check and
pick again.

Colours, not a re-identification network: it is cheap enough to run on every frame on
the server's CPU, needs no extra model, and holds up to the camera's exposure changing
as it turns, because greys are binned by brightness and colours by hue softly across
neighbouring bins.
"""

from __future__ import annotations

import io
import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from PIL import Image

from scout.robot.detector import Detection

HUE_BINS = 8  # each split into weak and strong saturation
GREY_BINS = 4  # black, dark, light, white
GREY_SATURATION = 0.2  # below this a pixel counts as grey, whatever its hue
STRONG_SATURATION = 0.5
PART_BINS = HUE_BINS * 2 + GREY_BINS
PARTS = 3  # top half, bottom half, whole box
MAX_PART_PIXELS = 64 * 64  # bigger crops are shrunk first: the histogram doesn't need more
# A box this close to the left, right or top of the picture is cut off there. The
# detector's box often stops a few pixels short of the edge of what it sees.
EDGE = 0.05
# ... and this close to the bottom. The camera sits low and tilts up, so the feet of
# someone in full view often come within 5% of the bottom.
EDGE_BOTTOM = 0.02
SAME_ONE = 0.5  # a box this much inside another (of its area) is taken as more of the same


def open_picture(jpeg: bytes) -> Image.Image:
    """The picture as RGB; raises ``ValueError`` if it can't be read."""
    try:
        with Image.open(io.BytesIO(jpeg)) as img:
            return img.convert("RGB")
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("unreadable picture") from exc


def _soft(
    out: np.ndarray,
    offset: int,
    pos: np.ndarray,
    bins: int,
    *,
    wrap: bool,
    stride: int = 1,
    extra: np.ndarray | None = None,
) -> None:
    """Add each value to the two nearest bins, weighted by closeness."""
    if not wrap:
        pos = np.clip(pos, 0, bins - 1)
    low = np.floor(pos)
    weight = pos - low
    low_i = low.astype(int)
    high_i = low_i + 1
    if wrap:
        low_i %= bins
        high_i %= bins
    else:
        high_i = np.minimum(high_i, bins - 1)
    add = 0 if extra is None else extra
    np.add.at(out, offset + low_i * stride + add, 1 - weight)
    np.add.at(out, offset + high_i * stride + add, weight)


def _histogram(hsv: np.ndarray) -> np.ndarray:
    """A normalised colour histogram of ``hsv`` pixels, an (n, 3) array in 0..1."""
    out = np.zeros(PART_BINS)
    if len(hsv) == 0:
        return out
    hue, sat, val = hsv[:, 0], hsv[:, 1], hsv[:, 2]
    grey = sat < GREY_SATURATION
    _soft(out, HUE_BINS * 2, val[grey] * GREY_BINS - 0.5, GREY_BINS, wrap=False)
    colour = ~grey
    strong = (sat[colour] >= STRONG_SATURATION).astype(int)
    _soft(out, 0, hue[colour] * HUE_BINS - 0.5, HUE_BINS, wrap=True, stride=2, extra=strong)
    total = out.sum()
    return out / total if total > 0 else out


def _part(image: Image.Image, x0: float, y0: float, x1: float, y1: float) -> np.ndarray:
    w, h = image.size
    left, right = int(x0 * w), max(int(x0 * w) + 1, int(x1 * w))
    top, bottom = int(y0 * h), max(int(y0 * h) + 1, int(y1 * h))
    crop = image.crop((left, top, right, bottom))
    if crop.width * crop.height > MAX_PART_PIXELS:
        scale = math.sqrt(MAX_PART_PIXELS / (crop.width * crop.height))
        crop = crop.resize((max(1, int(crop.width * scale)), max(1, int(crop.height * scale))))
    hsv = np.asarray(crop.convert("HSV"), dtype=np.float32).reshape(-1, 3) / 255.0
    return _histogram(hsv)


def describe(image: Image.Image, det: Detection) -> np.ndarray:
    """What the box looks like: colour histograms of its top half, its bottom half and
    the whole of it.

    The box's sides are trimmed, where a person's box is mostly background beside the
    arms, and so are the very top and bottom.
    """
    x0 = det.x0 + det.width * 0.2
    x1 = det.x1 - det.width * 0.2
    top = det.y0 + det.height * 0.08
    middle = det.y0 + det.height * 0.5
    bottom = det.y1 - det.height * 0.05
    upper, lower = _part(image, x0, top, x1, middle), _part(image, x0, middle, x1, bottom)
    return np.concatenate([upper, lower, (upper + lower) / 2])


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """How alike two descriptions are, 0 (nothing in common) to 1 (the same colours): the
    histogram intersection of each part, averaged."""
    return float(np.minimum(a, b).sum() / PARTS)


@dataclass
class Appearance:
    """The target's look: as first seen, and a running blend of recent sightings that
    follows slow changes in light. A candidate is compared with both."""

    reference: np.ndarray
    recent: np.ndarray

    @classmethod
    def of(cls, description: np.ndarray) -> Appearance:
        return cls(description.copy(), description.copy())

    def match(self, description: np.ndarray) -> float:
        return max(similarity(self.reference, description), similarity(self.recent, description))

    def learn(self, description: np.ndarray, rate: float = 0.25) -> None:
        self.recent = (1 - rate) * self.recent + rate * description


def _change(new: float, old: float, *, new_cut: bool, old_cut: bool) -> float:
    change = math.log(max(new, 1e-3) / max(old, 1e-3))
    if (change < 0 and new_cut) or (change > 0 and old_cut):
        return 0.0  # the cut-off could explain it
    return change


def size_change(candidate: Detection, last: Detection) -> float:
    """How much bigger ``candidate``'s box is than ``last``'s, as the log of the ratio
    (negative when smaller), averaged over width and height: a cat turning from side-on
    to face the car gets narrower but no shorter. A box cut off at an edge of the picture
    is at least as big as it looks that way, so a change the cut-off could explain counts
    as none: a person the camera cuts off at the knees is still as big as they were."""
    across = _change(
        candidate.width,
        last.width,
        new_cut=_cut_across(candidate),
        old_cut=_cut_across(last),
    )
    up = _change(
        candidate.height,
        last.height,
        new_cut=_cut_up(candidate),
        old_cut=_cut_up(last),
    )
    return (across + up) / 2


def _cut_across(det: Detection) -> bool:
    return det.x0 <= EDGE or det.x1 >= 1 - EDGE


def _cut_up(det: Detection) -> bool:
    return det.y0 <= EDGE or det.y1 >= 1 - EDGE_BOTTOM


def cut_off(det: Detection) -> bool:
    """Whether ``det``'s box reaches an edge of the picture, so part of it is out of view.
    The detector is less sure of a person it sees only part of, close up."""
    return _cut_across(det) or _cut_up(det)


def overlap(a: Detection, b: Detection) -> float:
    """How much of the smaller of two boxes is inside the other, from 0 to 1. The detector
    sometimes boxes a person's legs as well as all of them, and that box is all inside theirs."""
    across = min(a.x1, b.x1) - max(a.x0, b.x0)
    up = min(a.y1, b.y1) - max(a.y0, b.y0)
    smaller = min(a.width * a.height, b.width * b.height)
    if across <= 0 or up <= 0 or smaller <= 0:
        return 0.0
    return across * up / smaller


def _times(change: float) -> str:
    return f"{math.exp(abs(change)):.1f}× {'bigger' if change > 0 else 'smaller'}"


@dataclass(frozen=True)
class Choice:
    """Which candidate is the target (``index``, or None), how well it matched, whether
    another one came close, and why, for status. ``matches`` has every candidate's match,
    empty when there was no appearance to match against; ``notes`` says, for each, why it
    wasn't taken despite looking alike (None for the rest)."""

    index: int | None
    match: float | None
    unsure: bool
    why: str
    matches: tuple[float, ...] = ()
    notes: tuple[str | None, ...] = ()


def choose(
    candidates: Sequence[Detection],
    descriptions: Sequence[np.ndarray],
    appearance: Appearance | None,
    expected: tuple[float, float] | None,
    *,
    min_match: float,
    last: Detection | None = None,
    max_size_change: float = math.inf,
    typical_score: float | None = None,
    position_weight: float = 0.5,
    size_weight: float = 0.3,
    score_weight: float = 0.3,
    margin: float = 0.08,
) -> Choice:
    """The candidate that is the target.

    Without an appearance yet (nothing picked), the biggest. Otherwise the best of those
    that match at least ``min_match`` and are at most ``max_size_change`` times bigger or
    smaller than ``last`` (the latest sighting), scored by match less ``position_weight``
    times its distance from ``expected`` (in picture widths), less ``size_weight`` times
    its size change (``size_change``), and less ``score_weight`` times how much less sure
    the detector is of it than ``typical_score``, how sure it usually is of the target: a
    chair or a coat on a hook taken for a person is taken with less confidence than a
    person standing in view. ``unsure`` when the runner-up scores within ``margin``; a box
    mostly inside the chosen one's (``SAME_ONE``) is more of the same, not a runner-up.
    """
    if not candidates:
        return Choice(None, None, False, "none in view")
    if appearance is None:
        index = max(range(len(candidates)), key=lambda i: candidates[i].width * candidates[i].height)
        return Choice(index, None, False, "the biggest in view")
    matches = [appearance.match(d) for d in descriptions]
    notes: list[str | None] = [None] * len(candidates)
    limit = math.log(max_size_change)
    scored: list[tuple[float, int]] = []
    for i, (det, m) in enumerate(zip(candidates, matches, strict=True)):
        if m < min_match:
            continue
        score = m
        if last is not None:
            change = size_change(det, last)
            if abs(change) > limit:
                notes[i] = (
                    f"looks alike but is {_times(change)} than the target was, too big a change to be it"
                )
                continue
            score -= size_weight * abs(change)
        if typical_score is not None:
            score -= score_weight * max(0.0, typical_score - det.score)
        if expected is not None:
            score -= position_weight * math.hypot(det.cx - expected[0], det.cy - expected[1])
        scored.append((score, i))
    if not scored:
        best = max(range(len(candidates)), key=lambda i: matches[i])
        if notes[best] is not None:
            change = size_change(candidates[best], last) if last is not None else 0.0
            why = (
                f"the one in view that looks most like it (match {matches[best]:.2f}) is "
                f"{_times(change)} than the target was, too big a change to be it"
            )
        else:
            why = f"none of the {len(candidates)} in view looks like it (best match {matches[best]:.2f})"
        return Choice(None, matches[best], False, why, tuple(matches), tuple(notes))
    scored.sort(reverse=True)
    index = scored[0][1]
    rivals = [(s, i) for s, i in scored[1:] if overlap(candidates[i], candidates[index]) <= SAME_ONE]
    unsure = bool(rivals) and scored[0][0] - rivals[0][0] < margin
    why = f"match {matches[index]:.2f}"
    if unsure:
        why += f", but another one in view looks almost as alike ({matches[rivals[0][1]]:.2f})"
    return Choice(index, matches[index], unsure, why, tuple(matches), tuple(notes))
