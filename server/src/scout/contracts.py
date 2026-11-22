"""The seam between the two halves of the server.

Robot side (``scout.robot`` and ``scout_sim``): talks to cars over the car link
(protocol/car-link.md), runs behaviours such as look-around and follow, and turns tool
calls into car commands.

App side (everything else in ``scout``): the website, GitHub sign-in, accounts, tokens,
the driver lock, the activity log and the MCP endpoint. It decides *who* may do *what*
and then calls the robot side through the ``RobotAPI`` below.

The app side never imports robot internals and the robot side never deals with users,
apart from recording who owns a capture.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from starlette.websockets import WebSocket

Direction = Literal[
    "forward", "back", "left", "right", "forward_left", "forward_right", "back_left", "back_right"
]
TurnDirection = Literal["left", "right"]
ImageSize = Literal["small", "medium", "large"]  # qvga 320x240, vga 640x480, svga 800x600


class CarOffline(Exception):
    """The car isn't connected. The message is written for the AI and the website."""


class CarBusy(Exception):
    """The car can't do this right now (for example during a firmware update)."""


class CarError(Exception):
    """The car tried and failed (camera error, UNO not answering, timeout...)."""


@dataclass
class CarSettings:
    """Per-car settings, stored by the app side and editable by admins.

    The first five are sent to the car in every ``welcome``. The calibration values are
    the speeds at 100 % used to turn "turn 90 degrees" into a duration; they are tuned
    by measuring on the real floor. Slower percentages follow the motor curve in
    ``scout.robot.motion.speed_share``, which also depends on ``min_pwm``.
    """

    name: str = "Scout"
    obst_cm: int = 15
    min_pwm: int = 90
    link_ms: int = 1000
    vflip: bool = True
    hmirror: bool = False
    turn_deg_per_s: float = 180.0
    forward_cm_per_s: float = 30.0
    strafe_cm_per_s: float = 20.0

    def car_conf(self) -> dict[str, Any]:
        return {
            "obst_cm": self.obst_cm,
            "min_pwm": self.min_pwm,
            "link_ms": self.link_ms,
            "vflip": self.vflip,
            "hmirror": self.hmirror,
        }


# Looks up a car key. Returns the car id, or None if the key is unknown or revoked.
CarAuthenticator = Callable[[str], Awaitable[str | None]]
# Returns the current settings for a car id.
CarSettingsProvider = Callable[[str], Awaitable[CarSettings]]


@dataclass(frozen=True)
class CarSummary:
    car_id: str
    online: bool
    connected_since: float | None = None  # unix time
    last_seen: float | None = None
    firmware: str | None = None
    uno_firmware: str | None = None
    uno_ok: bool = False
    camera: str | None = None
    psram: int = 0
    max_res: str | None = None
    ssid: str | None = None
    rssi: int | None = None
    ip: str | None = None
    activity: str | None = None  # "following", "capturing", "updating" or None
    ota_state: str | None = None  # last firmware update progress, if any
    telemetry: dict[str, Any] = field(default_factory=dict)  # last ``tel`` message


@dataclass
class ActionResult:
    """What a tool call produced.

    ``data`` is JSON-serialisable and shown to the AI as text. ``images`` are JPEG bytes
    that the app side returns inline and also as short-lived links. ``capture_id`` is
    set when photos were saved to a capture that the app side offers as a download.
    """

    data: dict[str, Any]
    images: list[bytes] = field(default_factory=list)
    capture_id: str | None = None


class RobotAPI(Protocol):
    """Everything the app side may call. Implemented by ``scout.robot.Robot``.

    Every action raises ``CarOffline`` when the car isn't connected, ``CarBusy`` when it
    can't act right now and ``CarError`` when the car failed. Messages are plain
    sentences suitable for showing to the AI.

    A direct motion command (move, turn, drive, stop) cancels any running behaviour
    (look-around, capture orbit, follow) first.
    """

    async def start(self) -> None: ...
    async def close(self) -> None: ...

    # Car link. The app mounts this at /car/ws (ASGI websocket endpoint).
    async def handle_car_socket(self, websocket: WebSocket) -> None: ...

    # Inspection and admin.
    def cars(self) -> list[CarSummary]: ...
    def car(self, car_id: str) -> CarSummary | None: ...
    async def push_settings(self, car_id: str) -> None: ...
    async def stop_everything(self, car_id: str) -> None: ...
    async def disconnect(self, car_id: str) -> None: ...
    async def update_firmware(self, car_id: str, *, url: str, sha256: str, size: int) -> None: ...
    async def wheel_test(self, car_id: str, *, bits: int, pwm5: int, pwm6: int, ms: int) -> ActionResult: ...
    def capture_dir(self, capture_id: str) -> Path | None: ...

    # Tool actions.
    async def status(self, car_id: str) -> ActionResult: ...
    async def look(
        self, car_id: str, *, tilt: int | None = None, light: int | None = None, size: ImageSize = "medium"
    ) -> ActionResult: ...
    async def move(
        self, car_id: str, *, direction: Direction, ms: int, speed: int = 60, look: bool = False
    ) -> ActionResult: ...
    async def turn(
        self, car_id: str, *, direction: TurnDirection, degrees: int, speed: int = 50, look: bool = False
    ) -> ActionResult: ...
    async def drive(
        self, car_id: str, *, vx: int, vy: int, wz: int, ms: int, look: bool = False
    ) -> ActionResult: ...
    async def set_light(self, car_id: str, *, level: int) -> ActionResult: ...
    async def stop(self, car_id: str) -> ActionResult: ...
    async def look_around(self, car_id: str, *, views: int = 6) -> ActionResult: ...
    async def capture_photo(
        self, car_id: str, *, user_id: int, capture_id: str | None = None, note: str | None = None
    ) -> ActionResult: ...
    async def capture_orbit(
        self, car_id: str, *, user_id: int, photos: int = 12, direction: TurnDirection = "left"
    ) -> ActionResult: ...
    async def follow(self, car_id: str, *, target: str = "cat", seconds: int = 60) -> ActionResult: ...
