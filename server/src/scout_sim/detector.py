"""A stand-in object detector that finds the simulator's orange cat by colour.

The real follow detector (YOLOX on COCO) is trained on photos, not on the simulator's
cartoon, so this one lets ``follow`` run end to end against ``scout-sim``. It implements
``scout.robot.detector.Detector`` and only ever reports cats.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from scout.robot.detector import COCO_LABELS, Detection


class SimCatDetector:
    """Finds the one orange thing in the room: the simulated cat."""

    def __init__(self, *, min_fraction: float = 0.0015) -> None:
        self._labels = frozenset(COCO_LABELS)
        self.min_fraction = min_fraction
        self.calls = 0

    @property
    def labels(self) -> frozenset[str]:
        return self._labels

    def detect(self, jpeg: bytes) -> list[Detection]:
        self.calls += 1
        with Image.open(io.BytesIO(jpeg)) as raw:
            rgb = np.asarray(raw.convert("RGB"), dtype=np.int16)
        r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
        mask = (r > 150) & (r - g > 40) & (g - b > 30) & (r - b > 110)
        h, w = mask.shape
        if mask.sum() < self.min_fraction * h * w:
            return []
        cols = np.nonzero(mask.sum(axis=0) >= 2)[0]
        rows = np.nonzero(mask.sum(axis=1) >= 2)[0]
        if cols.size == 0 or rows.size == 0:
            return []
        return [
            Detection(
                label="cat",
                score=0.9,
                x0=float(cols[0]) / w,
                y0=float(rows[0]) / h,
                x1=float(cols[-1] + 1) / w,
                y1=float(rows[-1] + 1) / h,
            )
        ]
