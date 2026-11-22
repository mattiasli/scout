"""``Robot``: the robot side of the Scout server (implements ``scout.contracts.RobotAPI``).

It accepts car connections on the car link, keeps a ``CarSummary`` per car, and turns
tool calls into car requests. One command runs at a time per car (an asyncio lock);
``stop`` bypasses the lock. Behaviours (look-around, capture orbit, follow) are
interruptible: a direct motion command or ``stop`` sets their interrupt flag and sends
the car a ``stop`` so the move in flight ends at once.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
from starlette.websockets import WebSocket

from scout.contracts import (
    ActionResult,
    CarAuthenticator,
    CarBusy,
    CarError,
    CarOffline,
    CarSettings,
    CarSettingsProvider,
    CarSummary,
    Direction,
    ImageSize,
    TurnDirection,
)
from scout.robot.captures import CaptureStore, valid_capture_id
from scout.robot.config import RobotConfig
from scout.robot.detector import (
    COCO_LABELS,
    Detection,
    Detector,
    DetectorFactory,
    DetectorUnavailable,
    default_detector_factory,
    normalise_label,
)
from scout.robot.errors import InvalidRequest, LinkClosed, ReplyTimeout, clean_text, error_for_code
from scout.robot.follow import (
    FollowStep,
    FollowTuning,
    Search,
    bearing_deg,
    exit_side,
    expected_position,
    looks_far,
    start_tilt,
)
from scout.robot.follow import plan as follow_plan
from scout.robot.imaging import GREEN, YELLOW, annotate, crop, jpeg_size, labelled_grid, preview
from scout.robot.link import (
    CLOSE_ADMIN,
    CLOSE_GOING_AWAY,
    CLOSE_REPLACED,
    CarLink,
    ImageTooLarge,
    Pending,
    Reply,
)
from scout.robot.motion import (
    DIRECTIONS,
    direction_velocity,
    estimate,
    orbit_step,
    radius_correction,
    turn_plan,
    turn_rate,
)
from scout.robot.text import ago, cm_text, heading_label, plural, reason_phrase
from scout.robot.tracker import Appearance, Choice, choose, describe, open_picture

log = logging.getLogger("scout.robot")

RESOLUTIONS: dict[str, tuple[int, int]] = {
    "qvga": (320, 240),
    "vga": (640, 480),
    "svga": (800, 600),
    "xga": (1024, 768),
    "hd": (1280, 720),
    "sxga": (1280, 1024),
    "uxga": (1600, 1200),
    "qxga": (2048, 1536),
}
SIZE_RES: dict[str, str] = {"small": "qvga", "medium": "vga", "large": "svga"}
DONE_REASONS = frozenset({"time", "obstacle", "stopped", "link", "replaced"})
EVENT_KINDS = frozenset({"uno_lost", "uno_back", "uno_boot", "camera_error"})
UNO_DONE_TIMEOUT_S = 1.5  # the car's own wait for the UNO's DONE beyond ``ms``
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")
MAX_PICK = 20  # find numbers at most this many boxes; follow's pick goes up to it
LEARN_MATCH = 0.6  # follow updates the target's look from sightings at least this alike

Cancels = Literal["all", "follow", "none"]


# -- argument checks ---------------------------------------------------------------


def _int_arg(name: str, value: object, low: int, high: int) -> int:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequest(f"{name} must be a whole number from {low} to {high}.")
    if not low <= value <= high:
        raise InvalidRequest(f"{name} must be from {low} to {high}, not {value}.")
    return value


def _choice(name: str, value: object, options: tuple[str, ...] | dict[str, Any]) -> str:
    allowed = tuple(options)
    if not isinstance(value, str) or value not in allowed:
        raise InvalidRequest(f"{name} must be one of: {', '.join(allowed)}.")
    return value


def _opt_int(value: object) -> int | None:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _cm(value: object) -> int | None:
    v = _opt_int(value)
    return v if v is not None and 0 <= v <= 1000 else None


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or len(token) > 512:
        return None
    return token


def _clean_tel(msg: dict[str, Any]) -> dict[str, Any]:
    tel: dict[str, Any] = {"cm": _cm(msg.get("cm"))}
    line = msg.get("line")
    if isinstance(line, list) and len(line) == 3 and all(_opt_int(v) is not None for v in line):
        tel["line"] = [max(0, min(1023, int(v))) for v in line]
    for key in ("tilt", "light", "rssi", "heap", "up"):
        v = _opt_int(msg.get(key))
        if v is not None:
            tel[key] = v
    for key in ("moving", "uno"):
        if isinstance(msg.get(key), bool):
            tel[key] = msg[key]
    return tel


def _clean_hello(msg: dict[str, Any]) -> dict[str, Any]:
    def text(key: str, limit: int = 32) -> str | None:
        return clean_text(msg.get(key), limit) or None

    max_res = msg.get("max_res")
    psram = _opt_int(msg.get("psram"))
    return {
        "fw": text("fw"),
        "uno_fw": text("uno_fw"),
        "camera": text("camera"),
        "psram": psram if psram is not None and psram >= 0 else 0,
        "max_res": max_res if isinstance(max_res, str) and max_res in RESOLUTIONS else None,
        "mac": text("mac"),
        "ip": text("ip", 64),
        "ssid": text("ssid", 64),
        "rssi": _opt_int(msg.get("rssi")),
        "reset": text("reset"),
    }


# -- per-car state -----------------------------------------------------------------


@dataclass
class _Behaviour:
    kind: Literal["look_around", "orbit", "follow"]
    label: str  # "following the cat": used in sentences
    activity: str  # CarSummary.activity while it runs
    interrupt: asyncio.Event = field(default_factory=asyncio.Event)
    reason: str | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)  # follow: locked on or searching
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None

    @property
    def interrupted(self) -> bool:
        return self.interrupt.is_set()

    def stop(self, reason: str) -> None:
        if not self.interrupt.is_set():
            self.reason = reason
            self.interrupt.set()


@dataclass
class _Car:
    car_id: str
    link: CarLink | None = None
    connected_since: float | None = None
    last_seen: float | None = None
    hello: dict[str, Any] = field(default_factory=dict)
    fw_header: str | None = None
    telemetry: dict[str, Any] = field(default_factory=dict)
    tel_at: float | None = None  # monotonic
    tel_event: asyncio.Event = field(default_factory=asyncio.Event)
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=20))
    uno_ok: bool = False
    settings: CarSettings | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    behaviour: _Behaviour | None = None
    follow: dict[str, Any] | None = None
    follow_view: _FollowView | None = None
    follow_reference: bytes | None = None  # a close-up of the target as follow locked on
    pickable: _Pickable | None = None
    moving: bool = False
    capturing: bool = False
    light: int | None = None
    stop_generation: int = 0
    ota_active: bool = False
    ota_state: str | None = None
    ota_last: str | None = None
    ota_from_fw: str | None = None
    ota_rebooting: bool = False

    @property
    def name(self) -> str:
        return self.settings.name if self.settings else "The car"

    @property
    def online(self) -> bool:
        return self.link is not None and not self.link.closed

    @property
    def activity(self) -> str | None:
        if self.ota_active:
            return "updating"
        if self.behaviour is not None:
            return self.behaviour.activity
        if self.capturing:
            return "capturing"
        return None


@dataclass(frozen=True)
class _Outcome:
    reason: str
    cm_start: int | None
    cm_end: int | None
    ms: int


@dataclass(frozen=True)
class _Turn:
    degrees: float
    ms: int
    speed: int
    moves: int
    cm_start: int | None
    cm_end: int | None
    reason: str


class _Cancelled(Exception):
    """A motion command that was still waiting its turn when ``stop`` was called."""


class _LatestFrame:
    """Keeps only the newest stream frame."""

    def __init__(self) -> None:
        self._frame: bytes | None = None
        self._event = asyncio.Event()

    def put(self, _request_id: int, jpeg: bytes) -> None:
        self._frame = jpeg
        self._event.set()

    async def get(self, wait: float) -> bytes | None:
        try:
            async with asyncio.timeout(wait):
                await self._event.wait()
        except TimeoutError:
            return None
        self._event.clear()
        frame, self._frame = self._frame, None
        return frame

    def clear(self) -> None:
        """Drop the frame waiting, so the next one comes after this moment."""
        self._frame = None
        self._event.clear()


@dataclass(frozen=True)
class _Pickable:
    """The latest numbered picture, from find or follow_view: what ``follow(pick=n)``
    refers to."""

    at: float  # monotonic
    frame: bytes
    detections: list[Detection]  # in number order
    tilt: int | None  # the camera tilt setting it was taken at


@dataclass(frozen=True)
class _FollowView:
    """The latest frame follow acted on, and what it made of it."""

    at: float  # wall clock
    frame: bytes
    candidates: list[Detection]
    matches: list[float | None]
    notes: list[str | None]  # why a look-alike wasn't taken
    chosen: int | None
    tilt: int
    real_tilt: float
    hfov: float


@dataclass(frozen=True)
class _FollowStart:
    target: Detection | None  # the box picked, if one was
    appearance: Appearance | None
    tilt: int | None  # the tilt setting the pick was seen at
    fresh: bool  # picked moments ago, so expect it where it was
    seen: float | None  # monotonic time of the picture it was picked from


def _box(d: Detection) -> tuple[float, float, float, float]:
    return d.x0, d.y0, d.x1, d.y1


def _box_dict(d: Detection) -> dict[str, float]:
    return {"left": round(d.x0, 2), "right": round(d.x1, 2), "top": round(d.y0, 2), "bottom": round(d.y1, 2)}


def _direction_text(bearing: float) -> str:
    if abs(bearing) < 3:
        return "straight ahead"
    return f"{abs(bearing):.0f}° {'right' if bearing > 0 else 'left'}"


def _turned_deg(step: FollowStep, settings: CarSettings) -> float:
    """About how far a follow step turns the car, clockwise positive."""
    if not step.wz or step.vx:
        return 0.0
    return math.copysign(turn_rate(abs(step.wz), settings) * step.ms / 1000, step.wz)


def _candidates(
    detector: Detector, frame: bytes, label: str, min_score: float
) -> tuple[list[Detection], list[np.ndarray]]:
    """The frame's detections of ``label`` and what each looks like (runs in a thread)."""
    found = [d for d in detector.detect(frame) if d.label == label and d.score >= min_score]
    if not found:
        return [], []
    image = open_picture(frame)
    return found, [describe(image, d) for d in found]


def _appearance_of(frame: bytes, det: Detection) -> tuple[Appearance, bytes]:
    """What a picked box looks like, and a close-up of it to show later."""
    return Appearance.of(describe(open_picture(frame), det)), crop(frame, _box(det))


@dataclass
class _Checking:
    """Follow isn't sure the one it sees is the one it follows: it turns and tilts to keep
    it in view but doesn't drive, until enough sure sightings in a row or a pick."""

    why: str
    baseline: float | None  # how sure the detector usually was of the target, before
    streak: int = 0  # sure sightings in a row since


def _check(
    checking: _Checking | None,
    choice: Choice,
    det: Detection,
    label: str,
    typical_score: float | None,
    *,
    found_again: bool,
    confirmed: bool,
    score_drop: float,
    confirm_frames: int,
) -> _Checking | None:
    """Whether follow should hold back from driving after this sighting, and why.

    ``confirmed`` means the AI picked this one and follow hasn't lost it since; a look-alike
    close by is then no reason to doubt it, because the AI saw both when it picked.
    """
    doubt = None
    if found_again:
        doubt = f"it lost sight of the {label} and has found one again"
    elif typical_score is not None and det.score < score_drop * typical_score:
        doubt = (
            f"the detector is much less sure this is a {label} ({det.score:.2f}, "
            f"against {typical_score:.2f} before)"
        )
    elif choice.unsure and not confirmed:
        doubt = choice.why
    if doubt is not None and (checking is None or found_again):
        checking = _Checking(doubt, typical_score if checking is None else checking.baseline)
    if checking is None:
        return None
    sure = (
        choice.match is not None
        and choice.match >= LEARN_MATCH
        and (confirmed or not choice.unsure)
        and (checking.baseline is None or det.score >= score_drop * checking.baseline)
    )
    checking.streak = checking.streak + 1 if sure else 0
    return None if checking.streak >= confirm_frames else checking


def _monotonic(wall: float) -> float:
    """The monotonic time of a moment ``wall`` (a ``time.time()``) in the past."""
    return time.monotonic() - max(0.0, time.time() - wall)


def _render_view(view: _FollowView) -> tuple[bytes, list[dict[str, Any]], list[Detection]]:
    """follow_view's picture (the target in green, the others numbered in yellow), its
    listing, and the detections in number order."""
    order = sorted(range(len(view.candidates)), key=lambda i: view.candidates[i].cx)
    others: list[tuple[tuple[float, float, float, float], str, tuple[int, int, int]]] = []
    chosen: list[tuple[tuple[float, float, float, float], str, tuple[int, int, int]]] = []
    listing: list[dict[str, Any]] = []
    for n, i in enumerate(order, 1):
        d = view.candidates[i]
        following = i == view.chosen
        if following:
            chosen.append((_box(d), f"{n} following", GREEN))
        else:
            others.append((_box(d), f"{n} {d.label}", YELLOW))
        match = view.matches[i]
        item: dict[str, Any] = {
            "number": n,
            "label": d.label,
            "following": following,
            "match": None if match is None else round(match, 2),
            "score": round(d.score, 2),
            "direction": _direction_text(bearing_deg(d, view.hfov, view.real_tilt)),
            "box": _box_dict(d),
        }
        if view.notes[i] is not None:
            item["note"] = view.notes[i]
        listing.append(item)
    image = annotate(view.frame, others + chosen)  # the target's box drawn last, on top
    return image, listing, [view.candidates[i] for i in order]


# -- the robot ---------------------------------------------------------------------


class Robot:
    """Implements ``scout.contracts.RobotAPI``.

    ``config`` and ``detector_factory`` are optional extras beyond the contract: the
    timings (tests shrink them) and the object detector used by ``follow`` (tests pass
    a fake; the default downloads YOLOX-Nano into ``data_dir/models`` on first use).
    """

    def __init__(
        self,
        *,
        data_dir: Path,
        authenticate_car: CarAuthenticator,
        car_settings: CarSettingsProvider,
        allow_insecure: bool = False,
        config: RobotConfig | None = None,
        detector_factory: DetectorFactory | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        # Development mode. TLS ends at Railway's proxy, so the ASGI app can't tell ws://
        # from wss:// itself; the flag is recorded for logging and the app side.
        self.allow_insecure = allow_insecure
        self.config = config or RobotConfig()
        self._authenticate = authenticate_car
        self._car_settings = car_settings
        self._captures = CaptureStore(self.data_dir / "captures")
        self._detector_factory = detector_factory or default_detector_factory(self.data_dir / "models")
        self._detector: Detector | None = None
        self._detector_users = 0
        self._detector_lock = asyncio.Lock()
        self._detector_unload: asyncio.TimerHandle | None = None
        self._cars: dict[str, _Car] = {}
        self._links: set[CarLink] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    # -- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        self._closed = False
        if self.allow_insecure:
            log.warning("robot started in development mode (plain ws:// accepted)")

    async def close(self) -> None:
        self._closed = True
        if self._detector_unload is not None:
            self._detector_unload.cancel()
            self._detector_unload = None
        for car in self._cars.values():
            if car.behaviour is not None:
                car.behaviour.stop("the server is shutting down")
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        links = list(self._links)
        for link in links:
            await link.close(CLOSE_GOING_AWAY, "server shutting down")
        if links:
            with suppress(TimeoutError):
                async with asyncio.timeout(3.0):
                    await asyncio.gather(*(link.closed_event.wait() for link in links))

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- car link ------------------------------------------------------------------

    async def handle_car_socket(self, websocket: WebSocket) -> None:
        """The ASGI endpoint the app mounts at ``/car/ws``."""
        car_id: str | None = None
        key = _bearer(websocket.headers.get("authorization"))
        if key and not self._closed:
            try:
                car_id = await self._authenticate(key)
            except Exception:
                log.exception("car authentication failed")
        if not car_id:
            client = websocket.client.host if websocket.client else "?"
            log.info("refused a car connection from %s", client)
            await websocket.close(code=1008)  # before accept: the client gets HTTP 403
            return
        fw_header = clean_text(websocket.headers.get("x-scout-firmware"), 32) or None
        await websocket.accept()
        link = CarLink(websocket, car_id, self.config)
        self._links.add(link)
        reader = asyncio.create_task(link.receive_loop(self))
        keepalive = asyncio.create_task(link.keepalive())
        try:
            hello = await link.wait_hello()
            if hello is not None:
                await self._attach(link, hello, fw_header)
            await asyncio.wait({reader})
        finally:
            keepalive.cancel()
            reader.cancel()
            await asyncio.gather(reader, keepalive, return_exceptions=True)
            self._links.discard(link)
            self._detach(link)

    async def _attach(self, link: CarLink, hello: dict[str, Any], fw_header: str | None) -> None:
        car = self._cars.get(link.car_id)
        if car is None:
            car = self._cars[link.car_id] = _Car(link.car_id)
        old = car.link
        now = time.time()
        car.link = link
        car.hello = _clean_hello(hello)
        car.fw_header = fw_header
        car.connected_since = now
        car.last_seen = now
        car.uno_ok = car.hello.get("uno_fw") is not None
        car.telemetry = {}
        car.tel_at = None
        car.light = None
        car.moving = False
        settings = await self._load_settings(car)
        if link.last_tel is not None:  # telemetry that arrived while we were attaching
            self.on_tel(link, link.last_tel)
        log.info("car %s connected (firmware %s)", car.car_id, car.hello.get("fw"))
        if old is not None and old is not link:
            old.replaced = True
            if car.behaviour is not None:
                car.behaviour.stop(f"{settings.name} reconnected")
            await old.close(CLOSE_REPLACED, "replaced by a new connection")
        if car.ota_rebooting:
            car.ota_rebooting = False
            fw = car.hello.get("fw")
            if fw and fw != car.ota_from_fw:
                car.ota_state = f"finished: now running {fw}"
            else:
                car.ota_state = f"restarted, still running {fw or 'unknown firmware'}"
        welcome = {
            "t": "welcome",
            "car": car.car_id,
            "name": settings.name,
            "time": int(now),
            "conf": settings.car_conf(),
        }
        with suppress(LinkClosed):
            await link.send(welcome)

    def _detach(self, link: CarLink) -> None:
        car = self._cars.get(link.car_id)
        if car is None or car.link is not link:
            return
        log.info("car %s disconnected (code %s)", car.car_id, link.close_code)
        car.link = None
        car.connected_since = None
        car.moving = False
        car.capturing = False
        if car.behaviour is not None:
            car.behaviour.stop(f"{car.name} disconnected")
        if car.ota_active:
            car.ota_active = False
            if car.ota_last in ("verifying", "rebooting"):
                car.ota_rebooting = True
            else:
                car.ota_state = "interrupted: the car disconnected during the update"

    def _car_for(self, link: CarLink) -> _Car | None:
        car = self._cars.get(link.car_id)
        return car if car is not None and car.link is link else None

    # LinkHandler callbacks (synchronous, called by the link's receive loop).

    def on_activity(self, link: CarLink) -> None:
        car = self._car_for(link)
        if car is not None:
            car.last_seen = time.time()

    def on_hello(self, link: CarLink, msg: dict[str, Any]) -> None:
        car = self._car_for(link)
        if car is not None:  # a repeated hello on a live link: refresh what it says
            car.hello = _clean_hello(msg)

    def on_tel(self, link: CarLink, msg: dict[str, Any]) -> None:
        car = self._car_for(link)
        if car is None:
            return
        car.telemetry = _clean_tel(msg)
        car.tel_at = time.monotonic()
        if "uno" in car.telemetry:
            car.uno_ok = bool(car.telemetry["uno"])
        car.tel_event.set()

    def on_event(self, link: CarLink, msg: dict[str, Any]) -> None:
        car = self._car_for(link)
        if car is None:
            return
        kind = clean_text(msg.get("kind"), 24) or "unknown"
        car.events.append({"kind": kind, "msg": clean_text(msg.get("msg"), 200) or None, "time": time.time()})
        if kind == "uno_lost":
            car.uno_ok = False
        elif kind in ("uno_back", "uno_boot"):
            car.uno_ok = True
        if kind not in EVENT_KINDS:
            link.stats["unknown_event"] += 1

    # -- inspection and admin ------------------------------------------------------

    def _summary(self, car: _Car) -> CarSummary:
        hello = car.hello
        online = car.online
        rssi = car.telemetry.get("rssi", hello.get("rssi"))
        return CarSummary(
            car_id=car.car_id,
            online=online,
            connected_since=car.connected_since if online else None,
            last_seen=car.last_seen,
            firmware=hello.get("fw") or car.fw_header,
            uno_firmware=hello.get("uno_fw"),
            uno_ok=car.uno_ok if online else False,
            camera=hello.get("camera"),
            psram=hello.get("psram") or 0,
            max_res=hello.get("max_res"),
            ssid=hello.get("ssid"),
            rssi=rssi,
            ip=hello.get("ip"),
            activity=car.activity if online else None,
            ota_state=car.ota_state,
            telemetry=dict(car.telemetry),
        )

    def cars(self) -> list[CarSummary]:
        return [self._summary(c) for c in sorted(self._cars.values(), key=lambda c: c.car_id)]

    def car(self, car_id: str) -> CarSummary | None:
        car = self._cars.get(car_id)
        return self._summary(car) if car is not None else None

    def link_stats(self, car_id: str) -> dict[str, int]:
        """Counts of dropped or malformed frames on the car's current link (diagnostics)."""
        car = self._cars.get(car_id)
        return dict(car.link.stats) if car is not None and car.link is not None else {}

    def capture_dir(self, capture_id: str) -> Path | None:
        return self._captures.directory(capture_id)

    async def push_settings(self, car_id: str) -> None:
        """Send the current settings to a connected car as ``conf``."""
        car, link, settings = await self._online(car_id)
        await self._ack(car, link, "conf", "the new settings", **settings.car_conf())

    async def stop_everything(self, car_id: str) -> None:
        """Admin panic button: cancel behaviours, stop the motors and the stream.

        Unlike the tool actions it doesn't raise when the car is offline: an offline car
        has already stopped its motors.
        """
        car = self._cars.get(car_id)
        if car is None:
            return
        car.stop_generation += 1
        if car.behaviour is not None:
            car.behaviour.stop("an admin stopped everything")
        link = car.link
        if link is None or link.closed:
            return
        with suppress(LinkClosed):
            await link.fire("stop")
            await link.fire("stream", on=False)

    async def disconnect(self, car_id: str) -> None:
        car = self._cars.get(car_id)
        if car is None or car.link is None:
            return
        link = car.link
        await link.close(CLOSE_ADMIN, "disconnected by an admin")
        with suppress(TimeoutError):
            async with asyncio.timeout(3.0):
                await link.closed_event.wait()

    async def update_firmware(self, car_id: str, *, url: str, sha256: str, size: int) -> None:
        """Start an over-the-air update; progress shows in ``CarSummary.ota_state``.

        Returns once the car has accepted (or refused) the update. Raises ``CarBusy``
        while a move runs or an update is already going.
        """
        if not isinstance(url, str) or not url.startswith("https://") or len(url) > 1024:
            raise InvalidRequest("The firmware URL must be an https:// address.")
        if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
            raise InvalidRequest("sha256 must be 64 hexadecimal characters.")
        size = _int_arg("size", size, 1, 16 * 1024 * 1024)
        car, link, settings = await self._online(car_id)
        name = settings.name
        if car.ota_active:
            raise CarBusy(f"{name} is already installing a firmware update.")
        # The car refuses with "busy" too; telemetry can lag, so only our own state counts here.
        if car.moving or car.behaviour is not None:
            raise CarBusy(f"{name} is moving. Stop it first, then start the update.")
        pending = await self._send_request(car, link, "ota", url=url, sha256=sha256.lower(), size=size)
        car.ota_active = True
        car.ota_state = "requested"
        car.ota_last = None
        car.ota_from_fw = car.hello.get("fw")
        try:
            reply = await self._reply(car, pending, self.config.ack_timeout_s, "the firmware update")
        except ReplyTimeout:
            pending.close()
            car.ota_active = False
            car.ota_state = "failed: the car didn't answer"
            raise CarError(f"{name} didn't answer the firmware update request.") from None
        except (CarBusy, CarError, CarOffline) as exc:
            pending.close()
            car.ota_active = False
            car.ota_state = f"refused: {exc}"
            raise
        if self._apply_ota(car, reply):
            pending.close()
        else:
            self._spawn(self._track_ota(car, pending))

    def _apply_ota(self, car: _Car, reply: Reply) -> bool:
        """Record one OTA progress reply; True when the update has ended on the car."""
        if not isinstance(reply, dict):
            return False
        if reply.get("t") == "ack":
            car.ota_state = "accepted"
            return False
        state = clean_text(reply.get("state"), 16)
        pct = _opt_int(reply.get("pct"))
        detail = clean_text(reply.get("msg"))
        car.ota_last = state
        if state == "downloading":
            car.ota_state = f"downloading {max(0, min(100, pct))}%" if pct is not None else "downloading"
        elif state == "verifying":
            car.ota_state = "verifying"
        elif state == "rebooting":
            car.ota_state = "rebooting"
            car.ota_active = False
            car.ota_rebooting = True
            return True
        elif state == "failed":
            car.ota_state = f"failed: {detail or 'no reason given'}"
            car.ota_active = False
            return True
        else:
            car.ota_state = state or "unknown"
        return False

    async def _track_ota(self, car: _Car, pending: Pending) -> None:
        try:
            while True:
                try:
                    reply = await self._reply(car, pending, self.config.ota_progress_timeout_s, "the update")
                except ReplyTimeout:
                    car.ota_state = f"stalled: no progress for {self.config.ota_progress_timeout_s:g} s"
                    car.ota_active = False
                    return
                except CarOffline:
                    return  # _detach decides between "rebooting" and "interrupted"
                except (CarBusy, CarError) as exc:
                    car.ota_state = f"failed: {exc}"
                    car.ota_active = False
                    return
                if self._apply_ota(car, reply):
                    return
        finally:
            pending.close()

    async def wheel_test(self, car_id: str, *, bits: int, pwm5: int, pwm6: int, ms: int) -> ActionResult:
        """Admin diagnostic: raw shift-register byte and PWM duties (UART ``TEST``)."""
        bits = _int_arg("bits", bits, 0, 255)
        pwm5 = _int_arg("pwm5", pwm5, 0, 255)
        pwm6 = _int_arg("pwm6", pwm6, 0, 255)
        ms = _int_arg("ms", ms, 1, 2000)

        async def body(car: _Car, link: CarLink, settings: CarSettings) -> ActionResult:
            out = await self._timed(car, link, "test", ms, bits=bits, pwm5=pwm5, pwm6=pwm6)
            phrase = reason_phrase(out.reason, out.cm_start, out.cm_end, out.ms)
            return ActionResult(
                {
                    "result": phrase,
                    "reason": out.reason,
                    "bits": f"{bits:08b}",
                    "pwm5": pwm5,
                    "pwm6": pwm6,
                    "ms": out.ms,
                    "distance_before_cm": out.cm_start,
                    "distance_after_cm": out.cm_end,
                    "summary": f"Wheel test with shift register {bits:08b}, D5={pwm5}, D6={pwm6} "
                    f"ran {out.ms} ms: {phrase}.",
                }
            )

        return await self._motion(car_id, body)

    # -- tool actions --------------------------------------------------------------

    async def status(self, car_id: str) -> ActionResult:
        """Never raises ``CarOffline``; an offline car is reported in the data."""
        car = self._cars.get(car_id)
        probe = car or _Car(car_id)
        settings = await self._load_settings(probe, store=car is not None)
        name = settings.name
        if car is None or not car.online:
            data: dict[str, Any] = {"online": False, "name": name, "summary": self._offline_text(probe, name)}
            if car is not None:
                if car.last_seen is not None:
                    data["last_seen_s_ago"] = int(time.time() - car.last_seen)
                data["firmware"] = car.hello.get("fw")
                if car.ota_state:
                    data["firmware_update"] = car.ota_state
            return ActionResult(data)
        tel = car.telemetry
        cm = tel.get("cm")
        activity = car.activity
        doing = f" and {car.behaviour.label}" if car.behaviour else (f" and {activity}" if activity else "")
        parts = [f"{name} is online{doing}."]
        parts.append(
            f"Front distance {cm} cm." if cm is not None else "No echo ahead (nothing within range)."
        )
        if not car.uno_ok:
            parts.append("The motor board isn't answering.")
        data = {
            "online": True,
            "name": name,
            "summary": " ".join(parts),
            "firmware": car.hello.get("fw") or car.fw_header,
            "uno_firmware": car.hello.get("uno_fw"),
            "motor_board_ok": car.uno_ok,
            "camera": car.hello.get("camera"),
            "max_resolution": car.hello.get("max_res"),
            "wifi": {"ssid": car.hello.get("ssid"), "rssi": tel.get("rssi", car.hello.get("rssi"))},
            "distance_cm": cm,
            "line_sensors": tel.get("line"),
            "tilt_deg": tel.get("tilt"),
            "light": car.light if car.light is not None else tel.get("light"),
            "moving": bool(car.moving or tel.get("moving")),
            "activity": activity,
            "connected_for_s": int(time.time() - (car.connected_since or time.time())),
            "uptime_s": tel.get("up"),
        }
        if car.follow is not None:
            data["follow"] = self._follow_status(car)
        if car.ota_state:
            data["firmware_update"] = car.ota_state
        if car.events:
            now = time.time()
            data["recent_events"] = [
                {"kind": e["kind"], "msg": e["msg"], "when": ago(now - e["time"])}
                for e in list(car.events)[-5:]
            ]
        return ActionResult(data)

    async def look(
        self, car_id: str, *, tilt: int | None = None, light: int | None = None, size: ImageSize = "medium"
    ) -> ActionResult:
        """Optional tilt, then light, then a snapshot (small qvga, medium vga, large svga).

        A light level set here stays on until changed (``set_light``).
        """
        res = SIZE_RES[_choice("size", size, SIZE_RES)]
        tilt = None if tilt is None else _int_arg("tilt", tilt, -90, 90)
        light = None if light is None else _int_arg("light", light, 0, 100)
        async with self._exclusive(car_id, cancels="none") as (car, link, _settings):
            applied_tilt = car.telemetry.get("tilt")
            if tilt is not None:
                reply = await self._ack(car, link, "tilt", "the camera tilt", deg=tilt)
                new_tilt = _opt_int(reply.get("deg"))
                new_tilt = tilt if new_tilt is None else new_tilt
                if new_tilt != applied_tilt:
                    await asyncio.sleep(self.config.settle_s)
                applied_tilt = new_tilt
            if light is not None:
                await self._set_light(car, link, light)
            jpeg, w, h = await self._snap(car, link, res, self.config.look_quality)
            cm = car.telemetry.get("cm")
            level = car.light if car.light is not None else car.telemetry.get("light")
        light_text = "off" if not level else f"at {level} %"
        clamped = ""
        if tilt is not None and applied_tilt is not None and applied_tilt != tilt:
            # The motor board clamps the tilt to its safe range and reports what it applied.
            clamped = f" The camera can't tilt past {applied_tilt}°, so it stopped there."
        return ActionResult(
            {
                "resolution": f"{w}x{h}",
                "distance_cm": cm,
                "tilt_deg": applied_tilt,
                "light": level,
                "summary": f"Picture taken ({w}x{h}). Front distance {cm_text(cm)}, camera tilt "
                f"{applied_tilt if applied_tilt is not None else '?'}°, light {light_text}.{clamped}",
            },
            [jpeg],
        )

    async def move(
        self, car_id: str, *, direction: Direction, ms: int, speed: int = 60, look: bool = False
    ) -> ActionResult:
        direction = _choice("direction", direction, DIRECTIONS)  # type: ignore[assignment]
        ms = _int_arg("ms", ms, 100, 3000)
        speed = _int_arg("speed", speed, 20, 100)
        vx, vy, wz = direction_velocity(direction, speed)

        async def body(car: _Car, link: CarLink, settings: CarSettings) -> ActionResult:
            out = await self._timed(car, link, "move", ms, vx=vx, vy=vy, wz=wz)
            est = estimate(vx, vy, wz, out.ms, settings, layout=self.config.pwm_layout)
            phrase = reason_phrase(out.reason, out.cm_start, out.cm_end, out.ms)
            data = {
                "result": phrase,
                "reason": out.reason,
                "direction": direction,
                "speed": speed,
                "ms_requested": ms,
                "ms": out.ms,
                "estimated_cm": round(est.distance_cm, 1),
                "distance_before_cm": out.cm_start,
                "distance_after_cm": out.cm_end,
                "summary": f"Moved {direction.replace('_', ' ')} for {out.ms} ms (about "
                f"{est.distance_cm:.0f} cm): {phrase}. Front distance {cm_text(out.cm_start)} before, "
                f"{cm_text(out.cm_end)} after.",
            }
            return await self._maybe_look(car, link, data, look)

        return await self._motion(car_id, body)

    async def turn(
        self, car_id: str, *, direction: TurnDirection, degrees: int, speed: int = 50, look: bool = False
    ) -> ActionResult:
        """Rotate in place. The duration comes from ``turn_rate`` (calibration and motor curve)."""
        direction = _choice("direction", direction, ("left", "right"))  # type: ignore[assignment]
        degrees = _int_arg("degrees", degrees, 1, 360)
        speed = _int_arg("speed", speed, 20, 100)

        async def body(car: _Car, link: CarLink, settings: CarSettings) -> ActionResult:
            t = await self._turn(car, link, settings, degrees, speed, clockwise=direction == "right")
            phrase = reason_phrase(t.reason, t.cm_start, t.cm_end, t.ms)
            data = {
                "result": phrase,
                "reason": t.reason,
                "direction": direction,
                "degrees_requested": degrees,
                "estimated_degrees": round(t.degrees),
                "speed": t.speed,
                "ms": t.ms,
                "moves": t.moves,
                "distance_before_cm": t.cm_start,
                "distance_after_cm": t.cm_end,
                "summary": f"Turned {direction} about {t.degrees:.0f}° of {degrees}° requested: {phrase}. "
                f"Front distance now {cm_text(t.cm_end)}.",
            }
            return await self._maybe_look(car, link, data, look)

        return await self._motion(car_id, body)

    async def drive(
        self, car_id: str, *, vx: int, vy: int, wz: int, ms: int, look: bool = False
    ) -> ActionResult:
        """Raw mecanum velocities (percent): vx forward, vy right, wz clockwise."""
        vx = _int_arg("vx", vx, -100, 100)
        vy = _int_arg("vy", vy, -100, 100)
        wz = _int_arg("wz", wz, -100, 100)
        ms = _int_arg("ms", ms, 100, 3000)

        async def body(car: _Car, link: CarLink, settings: CarSettings) -> ActionResult:
            out = await self._timed(car, link, "move", ms, vx=vx, vy=vy, wz=wz)
            est = estimate(vx, vy, wz, out.ms, settings, layout=self.config.pwm_layout)
            phrase = reason_phrase(out.reason, out.cm_start, out.cm_end, out.ms)
            data = {
                "result": phrase,
                "reason": out.reason,
                "vx": vx,
                "vy": vy,
                "wz": wz,
                "ms_requested": ms,
                "ms": out.ms,
                "estimated": {
                    "forward_cm": round(est.forward_cm, 1),
                    "right_cm": round(est.right_cm, 1),
                    "clockwise_deg": round(est.clockwise_deg, 1),
                },
                "distance_before_cm": out.cm_start,
                "distance_after_cm": out.cm_end,
                "summary": f"Drove {out.ms} ms (about {est.forward_cm:.0f} cm forward, {est.right_cm:.0f} cm "
                f"right, {est.clockwise_deg:.0f}° clockwise): {phrase}.",
            }
            return await self._maybe_look(car, link, data, look)

        return await self._motion(car_id, body)

    async def set_light(self, car_id: str, *, level: int) -> ActionResult:
        level = _int_arg("level", level, 0, 100)
        async with self._exclusive(car_id, cancels="none") as (car, link, settings):
            applied = await self._set_light(car, link, level)
        text = "off" if applied == 0 else f"at {applied} %"
        return ActionResult({"light": applied, "summary": f"{settings.name}'s light is now {text}."})

    async def stop(self, car_id: str) -> ActionResult:
        """Stop the motors and cancel any behaviour. Never waits behind a running move."""
        car, link, settings = await self._online(car_id)
        name = settings.name
        car.stop_generation += 1  # motion commands still waiting their turn are dropped
        cancelled = None
        if car.behaviour is not None:
            cancelled = car.behaviour.label
            car.behaviour.stop("stop was requested")
        was_moving = bool(car.moving or car.telemetry.get("moving"))
        pending = await self._send_request(car, link, "stop")
        try:
            await self._reply(car, pending, self.config.ack_timeout_s, "the stop")
        except ReplyTimeout:
            raise CarError(
                f"{name} didn't confirm the stop. Every move is time-limited, so it stops by itself "
                "within 3 seconds."
            ) from None
        finally:
            pending.close()
        summary = f"{name} stopped."
        if cancelled:
            summary += f" It was {cancelled}; that is cancelled."
        data: dict[str, Any] = {"result": "stopped", "was_moving": was_moving, "summary": summary}
        if cancelled:
            data["cancelled"] = cancelled
        return ActionResult(data)

    async def look_around(self, car_id: str, *, views: int = 6) -> ActionResult:
        """Turn a full circle to the right, taking a vga picture every ``360/views`` degrees.

        Returns one labelled grid (direction relative to the start, front distance) and
        ends roughly facing the way it started.
        """
        views = _int_arg("views", views, 4, 8)
        try:
            async with self._behaviour(car_id, "look_around", "looking around", "looking around") as (
                car,
                link,
                settings,
                beh,
            ):
                return await self._look_around(car, link, settings, beh, views)
        except _Cancelled:
            return self._cancelled_result(car_id)

    async def capture_photo(
        self, car_id: str, *, user_id: int, capture_id: str | None = None, note: str | None = None
    ) -> ActionResult:
        """Save a full-resolution photo to a capture (a new one when ``capture_id`` is None)."""
        if capture_id is not None and not valid_capture_id(capture_id):
            raise InvalidRequest("That capture_id isn't valid. Leave it empty to start a new capture.")
        if note is not None:
            if not isinstance(note, str):
                raise InvalidRequest("note must be text.")
            note = " ".join(note.split())[:500] or None
        cfg = self.config
        async with self._exclusive(car_id, cancels="none") as (car, link, _settings):
            if capture_id is not None:
                await asyncio.to_thread(self._captures.check_owner, capture_id, car_id, user_id)
            res = self._max_res(car)
            car.capturing = True
            try:
                jpeg, w, h = await self._snap(car, link, res, cfg.capture_quality, cfg.capture_timeout_s)
            finally:
                car.capturing = False
            cm = car.telemetry.get("cm")
        if capture_id is None:
            capture_id = await asyncio.to_thread(self._captures.create, car_id, user_id)
        entry, count = await asyncio.to_thread(
            self._captures.add_photo, capture_id, jpeg, width=w, height=h, note=note, cm=cm
        )
        small = await asyncio.to_thread(preview, jpeg, max_width=cfg.preview_width)
        return ActionResult(
            {
                "capture_id": capture_id,
                "photo": entry["file"],
                "photo_count": count,
                "width": w,
                "height": h,
                "distance_cm": cm,
                "summary": f"Saved photo {count} ({w}x{h}) to capture {capture_id}. The picture shown is a "
                "small preview; the full-size photos are in the capture download.",
            },
            [small],
            capture_id=capture_id,
        )

    async def capture_orbit(
        self, car_id: str, *, user_id: int, photos: int = 12, direction: TurnDirection = "left"
    ) -> ActionResult:
        """Circle the object in front, saving a full-resolution photo every ``360/photos``°.

        Meant for photogrammetry (Blender). The radius is the front distance when it
        starts. Each step is planned by ``scout.robot.motion.orbit_step`` (see there for
        the geometry and its limits), then the car stops, settles about 400 ms and takes
        the photo. After each step it re-measures the distance and nudges itself back
        onto the circle. It aborts cleanly, keeping the photos taken so far, if
        something comes close, a move is cut short, or the car disconnects.

        ``direction="left"`` strafes left (clockwise around the object seen from above).
        Each photo's ``heading_deg`` in meta.json is its angle around the object from
        the first photo.
        """
        photos = _int_arg("photos", photos, 4, 36)
        orbit_dir: TurnDirection = (
            "left" if _choice("direction", direction, ("left", "right")) == "left" else "right"
        )
        try:
            async with self._behaviour(car_id, "orbit", "circling an object for a capture", "capturing") as (
                car,
                link,
                settings,
                beh,
            ):
                return await self._orbit(car, link, settings, beh, user_id, photos, orbit_dir)
        except _Cancelled:
            return self._cancelled_result(car_id)

    async def find(
        self, car_id: str, *, target: str | None = None, size: ImageSize = "medium"
    ) -> ActionResult:
        """A picture with the objects the detector finds in it boxed and numbered left to
        right; ``target`` keeps one class only. The numbers are what ``follow(pick=...)``
        takes, for ``follow_pick_valid_s``."""
        cfg = self.config
        res = SIZE_RES[_choice("size", size, SIZE_RES)]
        label = None
        if target is not None:
            if not isinstance(target, str) or normalise_label(target) not in COCO_LABELS:
                raise InvalidRequest(
                    f"find can't recognise {target!r}. It knows the COCO object classes, for example "
                    "person, cat, dog, teddy bear, sports ball, chair or bottle."
                )
            label = normalise_label(target)
        async with self._exclusive(car_id, cancels="none") as (car, link, _settings):
            jpeg, w, h = await self._snap(car, link, res, cfg.look_quality)
            tilt = _opt_int(car.telemetry.get("tilt"))
            cm = car.telemetry.get("cm")
        detector = await self._acquire_detector("Finding objects")
        try:
            detections = await asyncio.to_thread(detector.detect, jpeg)
        finally:
            self._release_detector()
        found = sorted(
            (d for d in detections if d.score >= cfg.find_min_score and (label is None or d.label == label)),
            key=lambda d: d.cx,
        )[:MAX_PICK]
        boxes = [((d.x0, d.y0, d.x1, d.y1), f"{n} {d.label}", YELLOW) for n, d in enumerate(found, 1)]
        image = await asyncio.to_thread(annotate, jpeg, boxes, min_width=w)
        car.pickable = _Pickable(time.monotonic(), jpeg, found, tilt)
        real_tilt = (tilt or 0) * cfg.camera_tilt_scale
        listing = [
            {
                "number": n,
                "label": d.label,
                "score": round(d.score, 2),
                "direction": _direction_text(bearing_deg(d, cfg.camera_hfov_deg, real_tilt)),
                "box": _box_dict(d),
            }
            for n, d in enumerate(found, 1)
        ]
        what = label or "object"
        if found:
            counts = ", ".join(f"{n} {d['label']} ({d['direction']})" for n, d in enumerate(listing, 1))
            summary = (
                f"Found {plural(len(found), what)}, numbered left to right: {counts}. To follow one, call "
                f"follow with pick set to its number (within {cfg.follow_pick_valid_s / 60:g} minutes)."
            )
        else:
            summary = f"No {what} found in the picture. Turn, tilt or move and try again."
        return ActionResult(
            {
                "resolution": f"{w}x{h}",
                "found": listing,
                "distance_cm": cm,
                "tilt_deg": tilt,
                "summary": summary,
            },
            images=[image],
        )

    async def follow(
        self, car_id: str, *, target: str = "cat", seconds: int = 60, pick: int | None = None
    ) -> ActionResult:
        """Start following in the background; returns once it has locked on or started
        looking, with the picture it locked on in.

        ``pick`` follows that numbered box from the latest ``find`` or ``follow_view``
        picture, and its class replaces ``target``; otherwise it takes the biggest
        ``target`` in view. Progress shows in ``status`` and ``follow_view``. It ends when
        the time is up, when a search for the lost target finds nothing, on ``stop`` or a
        direct motion command, or when the car disconnects.
        """
        cfg = self.config
        seconds = _int_arg("seconds", seconds, 5, 300)
        if pick is not None:
            pick = _int_arg("pick", pick, 1, MAX_PICK)
        if not isinstance(target, str) or not target.strip():
            raise InvalidRequest("target must name what to follow, such as cat, dog or person.")
        label = normalise_label(target)
        car, link, settings = await self._online(car_id)
        name = settings.name
        if car.ota_active:
            raise CarBusy(f"{name} is installing a firmware update.")
        running = car.behaviour
        if running is not None and running.kind != "follow":
            raise CarBusy(f"{name} is busy {running.label}. Call stop to cancel it first.")
        picked = car.pickable if pick is not None else None
        chosen: Detection | None = None
        if pick is not None:
            if picked is None or time.monotonic() - picked.at > cfg.follow_pick_valid_s:
                raise InvalidRequest(
                    "pick takes a number from a find or follow_view picture of the last "
                    f"{cfg.follow_pick_valid_s / 60:g} minutes. Call find first."
                )
            count = len(picked.detections)
            if pick > count:
                had = "none" if count == 0 else ("only number 1" if count == 1 else f"numbers 1 to {count}")
                raise InvalidRequest(f"There's no number {pick} in the last picture: it had {had}.")
            chosen = picked.detections[pick - 1]
            label = chosen.label
        detector = await self._acquire_detector("Following")
        try:
            if label not in detector.labels:
                raise InvalidRequest(
                    f"{name} can't recognise {target.strip()!r}. It knows the COCO object classes, "
                    "for example cat, dog, person, teddy bear, sports ball, chair or bottle."
                )
            appearance: Appearance | None = None
            reference: bytes | None = None
            if picked is not None and chosen is not None:
                appearance, reference = await asyncio.to_thread(_appearance_of, picked.frame, chosen)
            if running is not None:
                await self._interrupt(car, f"a new follow of the {label} started")
            if car.link is not link or link.closed:
                raise CarOffline(f"{name} disconnected.")
        except BaseException:
            self._release_detector()
            raise
        beh = _Behaviour("follow", f"following the {label}", "following")
        car.behaviour = beh
        car.follow = {
            "target": label,
            "picked": pick is not None,
            "state": "starting",
            "started": time.time(),
            "seconds": seconds,
            "last_seen": None,
            "frames": 0,
            "moves": 0,
            "obstacle_stops": 0,
            "ended_because": None,
        }
        car.follow_view = None
        car.follow_reference = reference
        start = _FollowStart(
            target=chosen,
            appearance=appearance,
            tilt=picked.tilt if picked is not None else None,
            fresh=picked is not None and time.monotonic() - picked.at < 10,
            seen=picked.at if picked is not None else None,
        )
        beh.task = self._spawn(self._follow_loop(car, link, settings, beh, detector, label, seconds, start))
        with suppress(TimeoutError):
            async with asyncio.timeout(cfg.follow_start_wait_s):
                await beh.ready.wait()
        return await self._follow_started(car, label, seconds)

    async def _follow_started(self, car: _Car, label: str, seconds: int) -> ActionResult:
        info = car.follow or {}
        name = car.name
        state = info.get("state")
        view = car.follow_view
        data: dict[str, Any] = {
            "result": "following started",
            "target": label,
            "seconds": seconds,
            "state": state,
        }
        images: list[bytes] = []
        check = (
            " Check now and then with follow_view that it's the right one. To switch, call follow with "
            "pick set to a number from that picture; to take over, call stop or any move."
        )
        if view is not None and view.chosen is not None:
            image, listing, order = await asyncio.to_thread(_render_view, view)
            car.pickable = _Pickable(_monotonic(view.at), view.frame, order, view.tilt)
            images.append(image)
            data["in_view"] = listing
            number = next(item["number"] for item in listing if item["following"])
            how = "the one you picked" if info.get("picked") else f"the biggest {label} in view"
            summary = (
                f"{name} locked on to {how}, number {number} in the green box, and follows it for up to "
                f"{seconds} seconds. It keeps to that one by its colours and searches if it goes out of "
                "sight."
            )
            if info.get("checking"):
                summary += (
                    f" It isn't sure yet ({info['checking']}), so it only turns to keep it in view and "
                    f"doesn't drive. If number {number} is the right one, call follow with pick set to "
                    f"{number} to confirm."
                )
        elif state == "searching":
            summary = (
                f"{name} can't see the {label} and is turning to look for it, for up to {seconds} seconds."
            )
        elif state == "ended":
            summary = f"{name} stopped following straight away: {info.get('ended_because')}."
            check = ""
        else:
            summary = f"{name} started following the {label} for up to {seconds} seconds."
        data["summary"] = summary + check
        return ActionResult(data, images=images)

    async def follow_view(self, car_id: str) -> ActionResult:
        """The latest picture follow acted on, with the target and other candidates boxed
        and numbered, and the target as it was when follow locked on."""
        car = self._cars.get(car_id)
        if car is None or car.follow is None:
            name = car.name if car is not None else "The car"
            return ActionResult(
                {
                    "result": "not following",
                    "summary": f"{name} hasn't followed anything yet. Use find to see what's in view, "
                    "then follow with pick.",
                }
            )
        status = self._follow_status(car)
        view = car.follow_view
        if view is None:
            return ActionResult(
                {
                    "result": "no picture yet",
                    "follow": status,
                    "summary": f"{car.name} hasn't looked yet. Try again in a moment.",
                }
            )
        image, listing, order = await asyncio.to_thread(_render_view, view)
        car.pickable = _Pickable(_monotonic(view.at), view.frame, order, view.tilt)
        images = [image]
        if car.follow_reference is not None:
            images.append(car.follow_reference)
        label = status["target"]
        age = round(time.time() - view.at, 1)
        target = next((item for item in listing if item["following"]), None)
        state = status["state"]
        if state == "ended":
            summary = f"Follow ended: {status.get('ended_because')}. The picture is the last one it acted on."
        elif target is not None:
            summary = (
                f"{car.name} is following the {label} in the green box, number {target['number']}"
                + (f" (match {target['match']:.2f})" if target["match"] is not None else "")
                + f", in a picture from {age:g} s ago."
            )
            if status.get("checking"):
                summary += (
                    f" It isn't driving until it's sure this is the right one, as {status['checking']}; "
                    "it only turns and tilts to keep it in view. Compare the green box with the second "
                    f"picture: if it's the right one, call follow with pick set to {target['number']} to "
                    "confirm."
                )
            elif status.get("unsure"):
                summary += f" It isn't sure: {status['unsure']}."
        else:
            last = (status.get("last_step") or {}).get("why", "")
            summary = f"{car.name} can't see the {label} it follows in this picture from {age:g} s ago"
            if listing:
                summary += f": {status.get('choice')}"
            summary += f". {last}."
        if len(images) > 1:
            summary += f" The second picture is the {label} as it was when follow locked on."
        if state != "ended":
            summary += (
                " If it's following the wrong one, call follow with pick set to a number from this picture, "
                "or call stop or any move to take over."
            )
        return ActionResult(
            {
                "result": state,
                "follow": status,
                "picture_age_s": age,
                "in_view": listing,
                "summary": summary,
            },
            images=images,
        )

    # -- building blocks -----------------------------------------------------------

    async def _load_settings(self, car: _Car, *, store: bool = True) -> CarSettings:
        try:
            settings = await self._car_settings(car.car_id)
        except Exception:
            log.exception("loading settings for car %s failed", car.car_id)
            settings = car.settings or CarSettings()
        if store:
            car.settings = settings
        return settings

    def _offline_text(self, car: _Car, name: str) -> str:
        if car.last_seen is None:
            return f"{name} is offline. It hasn't connected since the server started."
        return f"{name} is offline. It was last seen {ago(time.time() - car.last_seen)}."

    async def _online(self, car_id: str) -> tuple[_Car, CarLink, CarSettings]:
        car = self._cars.get(car_id)
        if car is None:
            probe = _Car(car_id)
            settings = await self._load_settings(probe, store=False)
            raise CarOffline(self._offline_text(probe, settings.name))
        settings = await self._load_settings(car)
        link = car.link
        if link is None or link.closed:
            raise CarOffline(self._offline_text(car, settings.name))
        return car, link, settings

    @asynccontextmanager
    async def _exclusive(
        self, car_id: str, *, cancels: Cancels
    ) -> AsyncIterator[tuple[_Car, CarLink, CarSettings]]:
        """Run one command at a time per car.

        ``cancels="all"``: a direct motion command; interrupts any behaviour first.
        ``"follow"``: a behaviour; interrupts a follow, refuses during the others.
        ``"none"``: refuses during look-around or an orbit, interleaves with a follow.
        """
        car, link, settings = await self._online(car_id)
        name = settings.name
        if car.ota_active:
            raise CarBusy(f"{name} is installing a firmware update. Try again once it has restarted.")
        beh = car.behaviour
        if beh is not None:
            if cancels == "all" or (cancels == "follow" and beh.kind == "follow"):
                await self._interrupt(car, "a new command arrived")
            elif beh.kind != "follow":
                raise CarBusy(
                    f"{name} is busy {beh.label}. Call stop to cancel it, or wait until it finishes."
                )
        generation = car.stop_generation
        try:
            async with asyncio.timeout(self.config.command_wait_s):
                await car.lock.acquire()
        except TimeoutError:
            raise CarBusy(f"{name} is still busy with an earlier command. Try again in a moment.") from None
        try:
            if car.link is not link or link.closed:
                raise CarOffline(f"{name} disconnected.")
            if cancels != "none" and car.stop_generation != generation:
                raise _Cancelled()
            yield car, link, settings
        finally:
            car.lock.release()

    @asynccontextmanager
    async def _behaviour(
        self, car_id: str, kind: Literal["look_around", "orbit"], label: str, activity: str
    ) -> AsyncIterator[tuple[_Car, CarLink, CarSettings, _Behaviour]]:
        async with self._exclusive(car_id, cancels="follow") as (car, link, settings):
            beh = _Behaviour(kind, label, activity)
            car.behaviour = beh
            try:
                yield car, link, settings, beh
            finally:
                if car.behaviour is beh:
                    car.behaviour = None
                beh.finished.set()

    async def _motion(
        self, car_id: str, body: Callable[[_Car, CarLink, CarSettings], Awaitable[ActionResult]]
    ) -> ActionResult:
        try:
            async with self._exclusive(car_id, cancels="all") as (car, link, settings):
                return await body(car, link, settings)
        except _Cancelled:
            return self._cancelled_result(car_id)

    def _cancelled_result(self, car_id: str) -> ActionResult:
        car = self._cars.get(car_id)
        name = car.name if car else "The car"
        return ActionResult(
            {
                "result": "cancelled: stop was requested before it started",
                "summary": f"{name} didn't do this because stop was called while it was waiting its turn.",
            }
        )

    async def _interrupt(self, car: _Car, reason: str) -> str | None:
        """Interrupt the running behaviour, stop the car and wait for it to wind down."""
        beh = car.behaviour
        if beh is None:
            return None
        beh.stop(reason)
        link = car.link
        if link is not None and not link.closed:
            with suppress(LinkClosed):
                await link.fire("stop")
        try:
            async with asyncio.timeout(self.config.interrupt_wait_s):
                await beh.finished.wait()
        except TimeoutError:
            log.warning("behaviour %s on car %s didn't wind down in time", beh.kind, car.car_id)
            if beh.task is not None and not beh.task.done():
                beh.task.cancel()
        return beh.label

    async def _send_request(self, car: _Car, link: CarLink, t: str, **fields: Any) -> Pending:
        try:
            return await link.request(t, **fields)
        except LinkClosed:
            raise CarOffline(f"{car.name} disconnected.") from None

    async def _reply(self, car: _Car, pending: Pending, wait: float, what: str) -> Reply:
        """Next reply to ``pending``; ``err`` replies become the matching exception."""
        try:
            item = await pending.next(wait)
        except TimeoutError:
            raise ReplyTimeout() from None
        except LinkClosed:
            if pending.link.replaced:
                raise CarError(
                    f"{car.name} reconnected in the middle of {what}, so it didn't finish."
                ) from None
            raise CarOffline(f"{car.name} disconnected during {what}.") from None
        except ImageTooLarge:
            raise CarError(f"{car.name} sent a picture larger than the server accepts.") from None
        if isinstance(item, dict) and item.get("t") == "err":
            raise error_for_code(item.get("code"), item.get("msg"), car.name)
        return item

    async def _ack(self, car: _Car, link: CarLink, t: str, what: str, **fields: Any) -> dict[str, Any]:
        pending = await self._send_request(car, link, t, **fields)
        try:
            try:
                reply = await self._reply(car, pending, self.config.ack_timeout_s, what)
            except ReplyTimeout:
                raise CarError(f"{car.name} didn't confirm {what} in time.") from None
        finally:
            pending.close()
        if not isinstance(reply, dict) or reply.get("t") != "ack":
            raise CarError(f"{car.name} answered {what} with something unexpected.")
        return reply

    async def _set_light(self, car: _Car, link: CarLink, level: int) -> int:
        reply = await self._ack(car, link, "light", "the light", level=level)
        applied = _opt_int(reply.get("level"))
        car.light = level if applied is None else applied
        return car.light

    async def _snap(
        self, car: _Car, link: CarLink, res: str, quality: int, wait: float | None = None
    ) -> tuple[bytes, int, int]:
        pending = await self._send_request(car, link, "snap", res=res, q=quality)
        try:
            try:
                reply = await self._reply(car, pending, wait or self.config.snap_timeout_s, "the picture")
                while isinstance(reply, dict) and reply.get("t") == "ack":
                    reply = await self._reply(car, pending, wait or self.config.snap_timeout_s, "the picture")
            except ReplyTimeout:
                raise CarError(f"{car.name} didn't send the picture in time.") from None
        finally:
            pending.close()
        if not isinstance(reply, bytes):
            raise CarError(f"{car.name} answered the picture request with something unexpected.")
        try:
            width, height = jpeg_size(reply)
        except ValueError:
            raise CarError(f"{car.name} sent a picture that can't be read.") from None
        return reply, width, height

    async def _safety_stop(self, link: CarLink) -> None:
        with suppress(LinkClosed):
            await link.fire("stop")

    async def _timed(self, car: _Car, link: CarLink, t: str, ms: int, **fields: Any) -> _Outcome:
        """A ``move`` or ``test``: wait for ``ack``, then for ``done``."""
        name = car.name
        car.moving = True
        pending: Pending | None = None
        try:
            pending = await self._send_request(car, link, t, ms=ms, **fields)
            try:
                reply = await self._reply(car, pending, self.config.ack_timeout_s, "the move")
                if isinstance(reply, dict) and reply.get("t") == "ack":
                    wait = ms / 1000 + UNO_DONE_TIMEOUT_S + self.config.done_margin_s
                    reply = await self._reply(car, pending, wait, "the move")
            except ReplyTimeout:
                await self._safety_stop(link)
                raise CarError(
                    f"{name} didn't report back on the move in time. It was told to stop."
                ) from None
            if not isinstance(reply, dict) or reply.get("t") != "done":
                await self._safety_stop(link)
                raise CarError(f"{name} answered the move with something unexpected. It was told to stop.")
        finally:
            car.moving = False
            if pending is not None:
                pending.close()
        reason = reply.get("reason")
        if reason == "error":
            raise CarError(f"{name}'s motor board didn't confirm the end of the move.")
        return _Outcome(
            reason=reason if isinstance(reason, str) and reason in DONE_REASONS else "unknown",
            cm_start=_cm(reply.get("cm_start")),
            cm_end=_cm(reply.get("cm_end")),
            ms=max(0, _opt_int(reply.get("ms")) or 0),
        )

    async def _turn(
        self,
        car: _Car,
        link: CarLink,
        settings: CarSettings,
        degrees: float,
        speed: int,
        *,
        clockwise: bool,
        beh: _Behaviour | None = None,
    ) -> _Turn:
        speed, chunks = turn_plan(degrees, speed, settings)
        wz = speed if clockwise else -speed
        total = 0
        moves = 0
        cm_start: int | None = None
        cm_end: int | None = None
        reason = "time"
        for chunk in chunks:
            if beh is not None and beh.interrupted:
                reason = "stopped"
                break
            out = await self._timed(car, link, "move", chunk, vx=0, vy=0, wz=wz)
            moves += 1
            total += out.ms
            if moves == 1:
                cm_start = out.cm_start
            cm_end = out.cm_end
            reason = out.reason
            if out.reason != "time":
                break
        return _Turn(total / 1000 * turn_rate(speed, settings), total, speed, moves, cm_start, cm_end, reason)

    async def _maybe_look(self, car: _Car, link: CarLink, data: dict[str, Any], look: bool) -> ActionResult:
        if not look:
            return ActionResult(data)
        await asyncio.sleep(self.config.settle_s)
        jpeg, w, h = await self._snap(car, link, "vga", self.config.look_quality)
        data["picture"] = f"{w}x{h}"
        return ActionResult(data, [jpeg])

    def _max_res(self, car: _Car) -> str:
        res = car.hello.get("max_res")
        return res if isinstance(res, str) and res in RESOLUTIONS else "svga"

    async def _fresh_cm(self, car: _Car, max_age: float = 1.5) -> int | None:
        """The front distance from telemetry no older than ``max_age`` (waits up to 2 s)."""
        if car.tel_at is None or time.monotonic() - car.tel_at > max_age:
            car.tel_event.clear()
            with suppress(TimeoutError):
                async with asyncio.timeout(2.0):
                    await car.tel_event.wait()
        cm = car.telemetry.get("cm")
        return cm if isinstance(cm, int) else None

    # -- behaviours ----------------------------------------------------------------

    async def _look_around(
        self, car: _Car, link: CarLink, settings: CarSettings, beh: _Behaviour, views: int
    ) -> ActionResult:
        cfg = self.config
        step = 360.0 / views
        heading = 0.0
        cm: int | None = car.telemetry.get("cm")
        tiles: list[tuple[bytes, str]] = []
        shots: list[dict[str, Any]] = []
        stopped: str | None = None
        for i in range(views):
            if i > 0:
                t = await self._turn(
                    car, link, settings, step, cfg.look_around_speed, clockwise=True, beh=beh
                )
                heading += t.degrees
                cm = t.cm_end
                if t.reason != "time":
                    stopped = reason_phrase(t.reason, t.cm_start, t.cm_end, t.ms)
                    break
                await asyncio.sleep(cfg.settle_s)
            if beh.interrupted:
                break
            jpeg, _w, _h = await self._snap(car, link, "vga", cfg.look_quality)
            # Label with the planned heading: the estimate only differs by millisecond rounding.
            nominal = i * step
            tiles.append((jpeg, f"{heading_label(nominal)} · {cm_text(cm)}"))
            shots.append(
                {
                    "view": i + 1,
                    "heading_deg": round(nominal),
                    "direction": heading_label(nominal),
                    "distance_cm": cm,
                }
            )
        if beh.interrupted:
            stopped = f"stopped early: {beh.reason}"
        if stopped is None and 360.0 - heading > 2.0:
            t = await self._turn(
                car, link, settings, 360.0 - heading, cfg.look_around_speed, clockwise=True, beh=beh
            )
            heading += t.degrees
            if t.reason != "time":
                stopped = reason_phrase(t.reason, t.cm_start, t.cm_end, t.ms)
        images: list[bytes] = []
        if tiles:
            columns = math.ceil(len(tiles) / 2) if len(tiles) > 2 else len(tiles)
            images.append(
                await asyncio.to_thread(
                    labelled_grid,
                    tiles,
                    columns=columns,
                    max_width=cfg.composite_max_width,
                    quality=cfg.composite_quality,
                )
            )
        result = stopped or "completed"
        name = settings.name
        if stopped is None:
            summary = (
                f"{name} turned a full circle to the right and took {plural(len(tiles), 'picture')}, "
                f"one every {step:.0f}°. The grid reads left to right, top to bottom; each label is the "
                "direction relative to where it started and the front distance. It is facing roughly "
                "the way it started."
            )
        else:
            why = result.removeprefix("stopped early: ")
            summary = f"{name} took {plural(len(tiles), 'picture')} and then stopped: {why}."
        return ActionResult(
            {"result": result, "views": shots, "turned_deg": round(heading), "summary": summary}, images
        )

    async def _orbit(
        self,
        car: _Car,
        link: CarLink,
        settings: CarSettings,
        beh: _Behaviour,
        user_id: int,
        photos: int,
        direction: TurnDirection,
    ) -> ActionResult:
        cfg = self.config
        name = settings.name
        radius = await self._fresh_cm(car)
        nearest = max(cfg.orbit_min_cm, settings.obst_cm + 5)
        if radius is None:
            raise CarError(
                f"{name} can't measure the distance to anything in front, so it can't circle it. Face the "
                f"object from {nearest}-{cfg.orbit_max_cm} cm away and try again."
            )
        if radius < nearest:
            raise CarError(
                f"{name} is only {radius} cm from the object; it needs at least {nearest} cm to circle it."
            )
        if radius > cfg.orbit_max_cm:
            raise CarError(
                f"The nearest thing in front is {radius} cm away, too far to circle "
                f"(at most {cfg.orbit_max_cm} cm)."
            )
        step = 360.0 / photos
        res = self._max_res(car)
        capture_id: str | None = None
        thumbs: list[tuple[bytes, str]] = []
        saved: list[dict[str, Any]] = []
        stopped: str | None = None
        cm: int | None = radius  # the distance where each photo is taken
        try:
            for i in range(photos):
                if i > 0:
                    stopped, cm = await self._orbit_move(car, link, settings, beh, radius, step, direction)
                    if stopped:
                        break
                await asyncio.sleep(cfg.orbit_settle_s)
                if beh.interrupted:
                    stopped = f"stopped early: {beh.reason}"
                    break
                car.capturing = True
                try:
                    jpeg, w, h = await self._snap(car, link, res, cfg.capture_quality, cfg.capture_timeout_s)
                finally:
                    car.capturing = False
                if capture_id is None:
                    capture_id = await asyncio.to_thread(self._captures.create, car.car_id, user_id)
                entry, _count = await asyncio.to_thread(
                    self._captures.add_photo,
                    capture_id,
                    jpeg,
                    width=w,
                    height=h,
                    note=f"orbit {i + 1}/{photos}",
                    cm=cm,
                    heading_deg=i * step,
                )
                saved.append({"photo": entry["file"], "heading_deg": round(i * step, 1), "distance_cm": cm})
                thumbs.append(
                    (await asyncio.to_thread(preview, jpeg, max_width=320), f"{i + 1} · {i * step:.0f}°")
                )
        except (CarOffline, CarError) as exc:
            if not saved:
                raise
            stopped = f"stopped early: {exc}"
            if isinstance(exc, CarError) and car.link is link and not link.closed:
                await self._safety_stop(link)
        images: list[bytes] = []
        if thumbs:
            images.append(
                await asyncio.to_thread(
                    labelled_grid, thumbs, columns=min(6, len(thumbs)), max_width=cfg.composite_max_width,
                    tile_width=320, quality=cfg.composite_quality,
                )
            )  # fmt: skip
        result = stopped or "completed"
        if stopped is None:
            summary = (
                f"{name} circled the object at about {radius} cm and saved {plural(len(saved), 'photo')} to "
                f"capture {capture_id}, one every {step:.0f}°. The picture is a contact sheet; the full-size "
                "photos are in the capture download."
            )
        else:
            summary = (
                f"{name} saved {plural(len(saved), 'photo')} of {photos} to capture {capture_id} and then "
                f"stopped: {result.removeprefix('stopped early: ')}."
            )
        return ActionResult(
            {
                "result": result,
                "capture_id": capture_id,
                "photo_count": len(saved),
                "photos_planned": photos,
                "radius_cm": radius,
                "step_deg": round(step, 1),
                "direction": direction,
                "photos": saved,
                "summary": summary,
            },
            images,
            capture_id=capture_id,
        )

    async def _orbit_move(
        self,
        car: _Car,
        link: CarLink,
        settings: CarSettings,
        beh: _Behaviour,
        radius: int,
        step: float,
        direction: TurnDirection,
    ) -> tuple[str | None, int | None]:
        """One orbit step: (why the orbit must stop or None, the front distance after it).

        The distance is the motor board's reading at the end of the step's last move; the
        car stands still after it, so it is the distance where the next photo is taken.
        """
        cfg = self.config
        plan = orbit_step(
            radius + cfg.sensor_offset_cm,
            step,
            direction,
            settings,
            speed=cfg.orbit_speed,
            mode=cfg.orbit_mode,
        )
        out: _Outcome | None = None
        for mv in plan:
            if beh.interrupted:
                return f"stopped early: {beh.reason}", None
            out = await self._timed(car, link, "move", mv.ms, vx=mv.vx, vy=mv.vy, wz=mv.wz)
            if out.reason != "time":
                return reason_phrase(out.reason, out.cm_start, out.cm_end, out.ms), out.cm_end
        cm = out.cm_end if out is not None else None
        # The circle is centred on the object's front surface, so the far side of anything
        # with depth comes closer than ``radius``; the correction backs the car onto the
        # circle. Only a reading below half the radius can't be that, so something has come
        # in between. After correcting, nothing may stay within the obstacle distance.
        if cm is not None and cm < radius // 2:
            return f"stopped early: something came within {cm} cm", cm
        if cfg.orbit_correct_radius and not beh.interrupted:
            fix = radius_correction(cm, radius, settings)
            if fix is not None:
                out = await self._timed(car, link, "move", fix.ms, vx=fix.vx, vy=fix.vy, wz=fix.wz)
                if out.reason != "time":
                    return reason_phrase(out.reason, out.cm_start, out.cm_end, out.ms), out.cm_end
                cm = out.cm_end
        if cm is not None and cm < max(settings.obst_cm, radius // 2):
            return f"stopped early: something came within {cm} cm", cm
        if beh.interrupted:
            return f"stopped early: {beh.reason}", cm
        return None, cm

    def _follow_status(self, car: _Car) -> dict[str, Any]:
        f = car.follow or {}
        now = time.time()
        out: dict[str, Any] = {"target": f.get("target"), "state": f.get("state")}
        if f.get("state") != "ended":
            out["seconds_left"] = max(0, int(f.get("started", now) + f.get("seconds", 0) - now))
        last = f.get("last_seen")
        out["last_seen_s_ago"] = None if last is None else round(now - last, 1)
        out["frames"] = f.get("frames", 0)
        out["moves"] = f.get("moves", 0)
        if f.get("tilt") is not None:
            out["tilts"] = f.get("tilts", 0)
            out["camera_tilt_deg"] = f["tilt"]
        out["picked"] = bool(f.get("picked"))
        if "in_view" in f:
            out["in_view"] = f["in_view"]  # how many of the target's class the last frame had
            out["choice"] = f.get("choice")
        if f.get("match") is not None:
            out["match"] = f["match"]  # how alike the last sighting looked, 0 to 1
        if f.get("unsure"):
            out["unsure"] = f["unsure"]
        if f.get("checking"):
            # Why it only turns and tilts, not driving, until it's sure or a pick confirms.
            out["checking"] = f["checking"]
        if f.get("search"):
            out["search"] = f["search"]
        if f.get("searches"):
            out["searches"] = f["searches"]
            out["found_again"] = f.get("found_again", 0)
        if f.get("last_target"):
            # The box in 0..1 of the picture (left and right across, top and bottom down).
            out["last_target"] = f["last_target"]
        if f.get("last_step"):
            # The step the last frame gave, with why; ms 0 means it held still.
            out["last_step"] = f["last_step"]
        if f.get("obstacle_stops"):
            out["obstacle_stops"] = f["obstacle_stops"]
        if f.get("ended_because"):
            out["ended_because"] = f["ended_because"]
        return out

    async def _acquire_detector(self, what: str) -> Detector:
        async with self._detector_lock:
            if self._detector_unload is not None:
                self._detector_unload.cancel()
                self._detector_unload = None
            if self._detector is None:
                try:
                    self._detector = await asyncio.to_thread(self._detector_factory)
                except DetectorUnavailable as exc:
                    raise CarError(f"{what} isn't available right now: {exc}.") from exc
                except Exception as exc:
                    log.exception("loading the object detector failed")
                    raise CarError(
                        f"{what} isn't available right now: the object detector failed to load."
                    ) from exc
            self._detector_users += 1
            return self._detector

    def _release_detector(self) -> None:
        """The model stays in memory while something uses it and for a while after (a
        find is usually followed by a follow), then it is dropped."""
        self._detector_users = max(0, self._detector_users - 1)
        if self._detector_users > 0:
            return
        if self._detector_unload is not None:
            self._detector_unload.cancel()
            self._detector_unload = None
        linger = self.config.detector_linger_s
        if linger <= 0 or self._closed:
            self._detector = None
            return
        self._detector_unload = asyncio.get_running_loop().call_later(linger, self._unload_detector)

    def _unload_detector(self) -> None:
        self._detector_unload = None
        if self._detector_users == 0:
            self._detector = None

    async def _follow_loop(
        self,
        car: _Car,
        link: CarLink,
        settings: CarSettings,
        beh: _Behaviour,
        detector: Detector,
        label: str,
        seconds: int,
        start: _FollowStart,
    ) -> None:
        cfg = self.config
        name = settings.name
        info = car.follow if car.follow is not None else {}
        tuning = FollowTuning(search_deg=cfg.follow_search_deg)
        frames = _LatestFrame()
        started = time.monotonic()
        deadline = started + seconds
        appearance = start.appearance
        target = start.target  # the latest sighting
        target_tilt = start.tilt  # the tilt setting it was seen at
        seen_at = start.seen if start.seen is not None else started  # when, monotonic
        expected = (target.cx, target.cy) if target is not None and start.fresh else None
        drift = 0.0  # how far the target had just moved across the picture by itself
        sure = 0  # sure sightings in a row
        typical_score = target.score if target is not None else None  # the detector's, usually
        confirmed = target is not None  # the AI picked it, and follow hasn't lost it since
        checking: _Checking | None = None
        search: Search | None = None
        misses = 0
        last_frame = started
        fatal: list[str] = []
        reason = "it ended"

        def on_reply(reply: Reply) -> None:
            if not isinstance(reply, dict):
                return
            if reply.get("t") == "done" and reply.get("reason") == "obstacle":
                info["obstacle_stops"] = info.get("obstacle_stops", 0) + 1
            elif reply.get("t") == "err" and reply.get("code") in ("uno", "busy"):
                fatal.append(str(error_for_code(reply.get("code"), reply.get("msg"), name)))

        try:
            # Start from a known tilt: follow steers the camera up and down from there, and
            # needs the tilt to turn a box into a direction. A picked target is looked for
            # at the tilt it was seen at.
            tilt = start.tilt if start.tilt is not None else start_tilt(label, tuning)
            reply = await self._ack(car, link, "tilt", "the camera tilt", deg=tilt)
            applied = _opt_int(reply.get("deg"))
            info["tilt"] = tilt if applied is None else applied
            await asyncio.sleep(cfg.settle_s)
            link.stream_sink = frames.put
            stream: dict[str, Any] = {
                "res": "qvga",
                "q": cfg.follow_quality,
                "fps": cfg.follow_fps,
                "secs": cfg.follow_stream_secs,
            }
            await self._ack(car, link, "stream", "starting the video stream", on=True, **stream)
            stream_sent = last_frame = time.monotonic()
            while True:
                now = time.monotonic()
                if beh.interrupted:
                    reason = beh.reason or "it was interrupted"
                    break
                if link.closed or car.link is not link:
                    reason = f"{name} disconnected"
                    break
                if fatal:
                    reason = fatal[0]
                    break
                if now >= deadline:
                    reason = "the time was up"
                    break
                if now - last_frame > cfg.follow_frame_timeout_s:
                    reason = "the camera stopped sending pictures"
                    break
                if now - stream_sent >= cfg.follow_stream_resend_s:
                    await link.fire("stream", on=True, **stream)
                    stream_sent = now
                frame = await frames.get(wait=min(0.5, max(0.1, 2.0 / cfg.follow_fps)))
                if frame is None:
                    continue
                last_frame = time.monotonic()
                info["frames"] = info.get("frames", 0) + 1
                try:
                    candidates, descriptions = await asyncio.to_thread(
                        _candidates, detector, frame, label, cfg.follow_min_score
                    )
                except Exception:
                    log.exception("object detection failed")
                    continue
                lost_s = max(0.0, time.monotonic() - seen_at)
                choice = choose(
                    candidates,
                    descriptions,
                    appearance,
                    expected,
                    min_match=cfg.follow_match_min,
                    last=target,
                    max_size_change=min(
                        cfg.follow_size_change_max,
                        cfg.follow_size_change * cfg.follow_size_change_growth**lost_s,
                    ),
                    typical_score=typical_score,
                )
                real_tilt = info["tilt"] * cfg.camera_tilt_scale
                car.follow_view = _FollowView(
                    at=time.time(),
                    frame=frame,
                    candidates=candidates,
                    matches=list(choice.matches) if choice.matches else [None] * len(candidates),
                    notes=list(choice.notes) if choice.notes else [None] * len(candidates),
                    chosen=choice.index,
                    tilt=info["tilt"],
                    real_tilt=real_tilt,
                    hfov=cfg.camera_hfov_deg,
                )
                info["in_view"] = len(candidates)
                info["choice"] = choice.why
                det: Detection | None = None
                if choice.index is not None:
                    det = candidates[choice.index]
                    found_again = search is not None
                    if found_again:
                        info["found_again"] = info.get("found_again", 0) + 1
                    if appearance is None:  # nothing picked: it locks on to the biggest
                        appearance = Appearance.of(descriptions[choice.index])
                        car.follow_reference = await asyncio.to_thread(crop, frame, _box(det))
                    else:
                        checking = _check(
                            checking,
                            choice,
                            det,
                            label,
                            typical_score,
                            found_again=found_again,
                            confirmed=confirmed,
                            score_drop=cfg.follow_score_drop,
                            confirm_frames=cfg.follow_confirm_frames,
                        )
                    if checking is not None:
                        sure = 0  # nothing learnt from what may be something else
                    else:
                        if typical_score is None:
                            typical_score = det.score
                        else:
                            typical_score += cfg.follow_score_rate * (det.score - typical_score)
                        if choice.match is not None and choice.match >= LEARN_MATCH and not choice.unsure:
                            sure += 1
                            if sure >= cfg.follow_learn_after:
                                appearance.learn(descriptions[choice.index])
                        else:
                            sure = 0
                    info["checking"] = None if checking is None else checking.why
                    drift = det.cx - expected[0] if expected is not None else 0.0
                    target, target_tilt, search, misses = det, info["tilt"], None, 0
                    seen_at = time.monotonic()
                    info.pop("search", None)
                    info["state"] = "following"
                    info["last_seen"] = time.time()
                    info["match"] = None if choice.match is None else round(choice.match, 2)
                    info["unsure"] = choice.why if choice.unsure else None
                    info["last_target"] = {**_box_dict(det), "centre": round(det.cx, 2),
                                           "width": round(det.width, 2), "height": round(det.height, 2),
                                           "score": round(det.score, 2)}  # fmt: skip
                    beh.ready.set()
                    step = follow_plan(
                        det,
                        car.telemetry.get("cm"),
                        settings,
                        hfov_deg=cfg.camera_hfov_deg,
                        tilt_deg=info["tilt"],
                        tilt_scale=cfg.camera_tilt_scale,
                        tuning=tuning,
                        hold=None
                        if checking is None
                        else f"not driving until it's sure this is the {label}, as {checking.why}",
                    )
                else:
                    misses += 1
                    sure = 0
                    if misses < cfg.follow_misses:
                        continue  # one empty frame can be a missed detection: look again
                    if search is None:
                        confirmed = False  # what it finds next may be something else
                        if target is None:
                            search = Search(label, "scan")
                        else:
                            # Its direction and distance from the tilt it was last seen at.
                            seen_tilt = (
                                info["tilt"] if target_tilt is None else target_tilt
                            ) * cfg.camera_tilt_scale
                            bearing = bearing_deg(target, cfg.camera_hfov_deg, seen_tilt)
                            far = looks_far(
                                target, hfov_deg=cfg.camera_hfov_deg, tilt_deg=seen_tilt, tuning=tuning
                            )
                            search = Search(label, exit_side(target, drift=drift), bearing, far)
                        info["searches"] = info.get("searches", 0) + 1
                    info["state"] = "searching"
                    beh.ready.set()
                    found_step = search.next_step(
                        car.telemetry.get("cm"),
                        settings,
                        tilt_deg=info["tilt"],
                        tilt_scale=cfg.camera_tilt_scale,
                        tuning=tuning,
                    )
                    if found_step is None:
                        turned = f"{search.turned_deg:.0f}°"
                        if target is None:
                            reason = f"it couldn't find a {label} after turning {turned} to look"
                        else:
                            reason = f"it lost sight of the {label} and didn't find it after turning {turned}"
                        break
                    step = found_step
                    info["search"] = {"went_out": search.side, "turned_deg": round(search.turned_deg)}
                    misses, expected = 0, None
                info["last_step"] = {
                    "vx": step.vx,
                    "wz": step.wz,
                    "ms": step.ms,
                    "tilt": step.tilt,
                    "why": step.why,
                }
                if not step.acts:
                    if det is not None:
                        expected = (det.cx, det.cy)
                    continue
                try:
                    async with asyncio.timeout(1.0):
                        await car.lock.acquire()
                except TimeoutError:
                    continue
                try:
                    if beh.interrupted or link.closed:
                        continue
                    if step.tilt is not None:
                        reply = await self._ack(car, link, "tilt", "the camera tilt", deg=step.tilt)
                        applied = _opt_int(reply.get("deg"))
                        info["tilt"] = step.tilt if applied is None else applied
                        info["tilts"] = info.get("tilts", 0) + 1
                    if step.moves:
                        await link.fire("move", on_reply, vx=step.vx, vy=0, wz=step.wz, ms=step.ms)
                        info["moves"] = info.get("moves", 0) + 1
                finally:
                    car.lock.release()
                if det is not None:  # where it should be once the car has turned
                    expected = expected_position(
                        det,
                        cfg.camera_hfov_deg,
                        tilt_before=real_tilt,
                        tilt_after=info["tilt"] * cfg.camera_tilt_scale,
                        turned_deg=_turned_deg(step, settings),
                    )
                # Look again only once the car has stopped and the picture has settled:
                # a frame taken while moving shows where the target was, not where it is.
                busy_s = max(step.ms / 1000, cfg.settle_s if step.tilt is not None else 0.0)
                with suppress(TimeoutError):
                    async with asyncio.timeout(busy_s + cfg.follow_settle_s):
                        await beh.interrupt.wait()
                frames.clear()
                last_frame = time.monotonic()
        except LinkClosed:
            reason = f"{name} disconnected"
        except (CarError, CarOffline, CarBusy) as exc:
            reason = str(exc)
        except asyncio.CancelledError:
            reason = beh.reason or "it was cancelled"
            raise
        except Exception:
            log.exception("follow failed")
            reason = "of an internal error"
        finally:
            link.stream_sink = None
            if not link.closed:
                with suppress(LinkClosed):
                    await link.fire("stream", on=False)
                    await link.fire("stop")
            info["state"] = "ended"
            info["ended_because"] = reason
            if car.behaviour is beh:
                car.behaviour = None
            beh.ready.set()
            beh.finished.set()
            self._release_detector()
            log.info("follow on car %s ended: %s", car.car_id, reason)
