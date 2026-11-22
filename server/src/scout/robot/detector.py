"""Object detection for ``follow``.

``Detector`` is the interface the follow behaviour uses; tests plug in fakes. The real
one is ``YoloxDetector``: YOLOX-Nano (Megvii, Apache-2.0), a 3.7 MB ONNX model trained on
the 80 COCO classes, run on onnxruntime's CPU provider.

The model isn't in the repository. ``ensure_model`` downloads it on first use from the
pinned release URL below into ``data_dir/models`` and checks its size and SHA-256 before
using it. A file already there with the right hash is used as is, so a server without
internet access can be given the file by hand.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
import numpy as np
from PIL import Image

log = logging.getLogger("scout.robot.detector")

COCO_LABELS: tuple[str, ...] = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat",
    "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack",
    "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball",
    "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
    "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
)  # fmt: skip

# Words people use for the COCO classes that matter most for following.
ALIASES: dict[str, str] = {
    "kitten": "cat",
    "kitty": "cat",
    "cats": "cat",
    "puppy": "dog",
    "dogs": "dog",
    "human": "person",
    "people": "person",
    "man": "person",
    "woman": "person",
    "child": "person",
    "kid": "person",
    "me": "person",
    "ball": "sports ball",
    "teddy": "teddy bear",
}


def normalise_label(target: str) -> str:
    word = " ".join(target.lower().replace("_", " ").split())
    return ALIASES.get(word, word)


@dataclass(frozen=True)
class Detection:
    """One detected object. The box is normalised to 0..1 of the image size."""

    label: str
    score: float
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


class Detector(Protocol):
    """Finds objects in a JPEG. ``detect`` runs in a worker thread."""

    @property
    def labels(self) -> frozenset[str]: ...

    def detect(self, jpeg: bytes) -> list[Detection]: ...


# Builds a detector. Called in a worker thread; raises ``DetectorUnavailable`` if it can't.
DetectorFactory = Callable[[], Detector]


class DetectorUnavailable(Exception):
    """The model couldn't be downloaded, verified or loaded. The message says why."""


@dataclass(frozen=True)
class ModelSpec:
    name: str
    file: str
    url: str
    sha256: str
    size: int
    input_size: int
    licence: str
    source: str


YOLOX_NANO = ModelSpec(
    name="YOLOX-Nano (COCO, 416x416)",
    file="yolox_nano.onnx",
    url="https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.onnx",
    sha256="c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d",
    size=3_659_407,
    input_size=416,
    licence="Apache-2.0",
    source="https://github.com/Megvii-BaseDetection/YOLOX",
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def ensure_model(spec: ModelSpec, models_dir: Path, *, client: httpx.Client | None = None) -> Path:
    """Return the path of a verified model file, downloading it if needed."""
    path = models_dir / spec.file
    if path.is_file():
        if path.stat().st_size == spec.size and _sha256(path) == spec.sha256:
            return path
        log.warning("model file %s doesn't match its pinned hash; downloading it again", path)
    try:
        models_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DetectorUnavailable(f"the models folder can't be created ({exc.strerror})") from exc
    tmp = path.with_name(path.name + ".part")
    own = client is None
    http = client or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, connect=10.0))
    try:
        log.info("downloading %s from %s", spec.name, spec.url)
        h = hashlib.sha256()
        size = 0
        with http.stream("GET", spec.url) as response:
            if response.status_code != 200:
                raise DetectorUnavailable(f"the model download failed (HTTP {response.status_code})")
            with tmp.open("wb") as f:
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > spec.size:
                        raise DetectorUnavailable("the downloaded model is larger than expected")
                    h.update(chunk)
                    f.write(chunk)
        if size != spec.size or h.hexdigest() != spec.sha256:
            raise DetectorUnavailable("the downloaded model doesn't match its pinned SHA-256")
        os.replace(tmp, path)
        return path
    except httpx.HTTPError as exc:
        raise DetectorUnavailable(f"the model couldn't be downloaded ({type(exc).__name__})") from exc
    except OSError as exc:
        raise DetectorUnavailable(f"the model couldn't be saved ({exc.strerror})") from exc
    finally:
        if own:
            http.close()
        tmp.unlink(missing_ok=True)


def _nms(boxes: np.ndarray, scores: np.ndarray, iou: float) -> list[int]:
    order = np.argsort(-scores)
    keep: list[int] = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        rest = order[1:]
        xx0 = np.maximum(boxes[i, 0], boxes[rest, 0])
        yy0 = np.maximum(boxes[i, 1], boxes[rest, 1])
        xx1 = np.minimum(boxes[i, 2], boxes[rest, 2])
        yy1 = np.minimum(boxes[i, 3], boxes[rest, 3])
        inter = np.clip(xx1 - xx0, 0, None) * np.clip(yy1 - yy0, 0, None)
        area_i = (boxes[i, 2] - boxes[i, 0]) * (boxes[i, 3] - boxes[i, 1])
        area_r = (boxes[rest, 2] - boxes[rest, 0]) * (boxes[rest, 3] - boxes[rest, 1])
        overlap = inter / np.maximum(area_i + area_r - inter, 1e-9)
        order = rest[overlap <= iou]
    return keep


def decode_yolox(
    output: np.ndarray,
    *,
    input_size: int,
    ratio: float,
    image_size: tuple[int, int],
    score_threshold: float = 0.3,
    iou: float = 0.45,
    labels: tuple[str, ...] = COCO_LABELS,
) -> list[Detection]:
    """Turn YOLOX's raw ``(N, 5 + classes)`` output into detections.

    Rows are per grid cell at strides 8, 16 and 32: centre offsets, log sizes, then the
    objectness and class scores (already through a sigmoid in the exported model).
    """
    grids = []
    strides = []
    for stride in (8, 16, 32):
        g = input_size // stride
        ys, xs = np.meshgrid(np.arange(g), np.arange(g), indexing="ij")
        grids.append(np.stack((xs, ys), axis=2).reshape(-1, 2))
        strides.append(np.full((g * g, 1), stride))
    grid = np.concatenate(grids).astype(np.float32)
    stride_col = np.concatenate(strides).astype(np.float32)
    if output.shape[0] != grid.shape[0]:
        raise ValueError(f"unexpected YOLOX output shape {output.shape}")
    xy = (output[:, :2] + grid) * stride_col
    wh = np.exp(np.clip(output[:, 2:4], -10, 10)) * stride_col
    scores = output[:, 4:5] * output[:, 5:]
    cls = scores.argmax(axis=1)
    best = scores[np.arange(scores.shape[0]), cls]
    mask = best >= score_threshold
    if not mask.any():
        return []
    xy, wh, cls, best = xy[mask], wh[mask], cls[mask], best[mask]
    boxes = np.concatenate((xy - wh / 2, xy + wh / 2), axis=1) / ratio
    width, height = image_size
    found: list[Detection] = []
    for c in np.unique(cls):
        idx = np.nonzero(cls == c)[0]
        for k in _nms(boxes[idx], best[idx], iou):
            x0, y0, x1, y1 = boxes[idx[k]]
            found.append(
                Detection(
                    label=labels[int(c)] if int(c) < len(labels) else str(int(c)),
                    score=float(best[idx[k]]),
                    x0=float(np.clip(x0 / width, 0, 1)),
                    y0=float(np.clip(y0 / height, 0, 1)),
                    x1=float(np.clip(x1 / width, 0, 1)),
                    y1=float(np.clip(y1 / height, 0, 1)),
                )
            )
    found.sort(key=lambda d: -d.score)
    return found


class YoloxDetector:
    """YOLOX-Nano on onnxruntime. Input is BGR 0..255, letterboxed to 416×416 with 114 grey."""

    def __init__(
        self, model_path: Path, *, spec: ModelSpec = YOLOX_NANO, score_threshold: float = 0.3
    ) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        try:
            self._session: Any = ort.InferenceSession(
                str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
            )
        except Exception as exc:  # onnxruntime raises its own exception types
            raise DetectorUnavailable(f"the model couldn't be loaded ({type(exc).__name__})") from exc
        self._input = self._session.get_inputs()[0].name
        self._size = spec.input_size
        self._threshold = score_threshold
        self._labels = frozenset(COCO_LABELS)

    @classmethod
    def load(cls, models_dir: Path, *, client: httpx.Client | None = None) -> YoloxDetector:
        return cls(ensure_model(YOLOX_NANO, models_dir, client=client))

    @property
    def labels(self) -> frozenset[str]:
        return self._labels

    def detect(self, jpeg: bytes) -> list[Detection]:
        with Image.open(io.BytesIO(jpeg)) as raw:
            img = raw.convert("RGB")
        size = self._size
        ratio = min(size / img.width, size / img.height)
        nw, nh = max(1, int(img.width * ratio)), max(1, int(img.height * ratio))
        canvas = np.full((size, size, 3), 114, dtype=np.uint8)
        canvas[:nh, :nw] = np.asarray(img.resize((nw, nh), Image.Resampling.BILINEAR))
        tensor = np.ascontiguousarray(canvas[:, :, ::-1].transpose(2, 0, 1)[None], dtype=np.float32)
        output = self._session.run(None, {self._input: tensor})[0][0]
        return decode_yolox(
            output, input_size=size, ratio=ratio, image_size=img.size, score_threshold=self._threshold
        )


def default_detector_factory(models_dir: Path) -> DetectorFactory:
    """YOLOX-Nano, downloaded into ``models_dir`` on first use.

    ``SCOUT_FOLLOW_DETECTOR=sim-cat`` swaps in the simulator's colour detector instead, so
    ``follow`` can be tried against ``scout-sim`` (YOLOX doesn't recognise its cartoon
    cat). It reports anything bright orange as a cat: never set it for a real car.
    """
    choice = os.environ.get("SCOUT_FOLLOW_DETECTOR", "").strip().lower()
    if choice == "sim-cat":
        log.warning("SCOUT_FOLLOW_DETECTOR=sim-cat: follow uses the simulator's colour detector")

        def sim() -> Detector:
            try:
                from scout_sim.detector import SimCatDetector
            except ImportError as exc:  # pragma: no cover - scout_sim ships with the server
                raise DetectorUnavailable("the simulator's detector isn't installed") from exc
            return SimCatDetector()

        return sim
    if choice not in ("", "yolox"):
        log.warning("unknown SCOUT_FOLLOW_DETECTOR %r, using YOLOX", choice[:20])

    def build() -> Detector:
        return YoloxDetector.load(models_dir)

    return build
