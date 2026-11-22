"""Keeping to one particular target among others of its class.

The detector tells a person from a cat, not one person from another. Follow remembers
what the one it follows looks like, as colour histograms of the top and bottom halves of
its box (for a person, roughly their top and their trousers) and of the whole box, which
changes less when the picture cuts them off as the car comes closer. Each frame it
takes the candidate that looks most like it, preferring the one nearest where it should
be after the car's last turn. A candidate that doesn't look like it is never taken, so
follow doesn't switch to someone else without saying so. Two people dressed alike can
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


@dataclass(frozen=True)
class Choice:
    """Which candidate is the target (``index``, or None), how well it matched, whether
    another one came close, and why, for status. ``matches`` has every candidate's match,
    empty when there was no appearance to match against."""

    index: int | None
    match: float | None
    unsure: bool
    why: str
    matches: tuple[float, ...] = ()


def choose(
    candidates: Sequence[Detection],
    descriptions: Sequence[np.ndarray],
    appearance: Appearance | None,
    expected: tuple[float, float] | None,
    *,
    min_match: float,
    position_weight: float = 0.5,
    margin: float = 0.08,
) -> Choice:
    """The candidate that is the target.

    Without an appearance yet (nothing picked), the biggest. Otherwise the best of those
    that match at least ``min_match``, scored by match less ``position_weight`` times its
    distance from ``expected`` (in picture widths). ``unsure`` when the runner-up scores
    within ``margin``.
    """
    if not candidates:
        return Choice(None, None, False, "none in view")
    if appearance is None:
        index = max(range(len(candidates)), key=lambda i: candidates[i].width * candidates[i].height)
        return Choice(index, None, False, "the biggest in view")
    matches = [appearance.match(d) for d in descriptions]
    scored: list[tuple[float, int]] = []
    for i, (det, m) in enumerate(zip(candidates, matches, strict=True)):
        if m < min_match:
            continue
        score = m
        if expected is not None:
            score -= position_weight * math.hypot(det.cx - expected[0], det.cy - expected[1])
        scored.append((score, i))
    if not scored:
        best = max(matches)
        why = f"none of the {len(candidates)} in view looks like it (best match {best:.2f})"
        return Choice(None, best, False, why, tuple(matches))
    scored.sort(reverse=True)
    index = scored[0][1]
    unsure = len(scored) > 1 and scored[0][0] - scored[1][0] < margin
    why = f"match {matches[index]:.2f}"
    if unsure:
        why += f", but another one in view looks almost as alike ({matches[scored[1][1]]:.2f})"
    return Choice(index, matches[index], unsure, why, tuple(matches))
