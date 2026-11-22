"""A real server for the robot tests: ``Robot`` in a Starlette app on uvicorn (127.0.0.1,
random port), with simulated cars connecting over real WebSockets."""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute

from scout.contracts import CarSettings
from scout.robot import Robot, RobotConfig
from scout.robot.detector import DetectorFactory
from scout_sim import Faults, Physics, SimCar, SimCatDetector, SimConfig, World

KEYS = {"key-1": "car-1", "key-2": "car-2"}

# The simulated car runs four times faster than the defaults so the suite stays quick; the
# server's calibration says the same.
FAST = {"forward_cm_per_s": 120.0, "strafe_cm_per_s": 100.0, "turn_deg_per_s": 720.0}


def fast_settings(**overrides: Any) -> CarSettings:
    return CarSettings(**{**FAST, **overrides})


def fast_physics(**overrides: Any) -> Physics:
    return Physics(**{**FAST, "noise": 0.0, **overrides})


def fast_config(**overrides: Any) -> RobotConfig:
    cfg = RobotConfig(
        hello_timeout_s=2.0,
        ack_timeout_s=1.5,
        done_margin_s=1.5,
        snap_timeout_s=3.0,
        capture_timeout_s=5.0,
        command_wait_s=10.0,
        interrupt_wait_s=3.0,
        ota_progress_timeout_s=5.0,
        settle_s=0.02,
        orbit_settle_s=0.05,
        follow_search_deg=120.0,
        follow_frame_timeout_s=2.0,
        follow_start_wait_s=2.0,
        detector_linger_s=0.0,
        follow_stream_secs=3,
        follow_stream_resend_s=1.5,
        camera_tilt_scale=1.0,  # the simulated servo turns exactly as far as it's told
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def plain_world(**physics: Any) -> World:
    """The default furnished room, no cat, the car at (110, 150) facing +x."""
    world = World.default(seed=1, cat=False, physics=fast_physics(**physics))
    world.place_car(110, 150, 0)
    return world


async def wait_for(predicate: Callable[[], object], within: float = 5.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + within
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(interval)


class Harness:
    def __init__(self, robot: Robot, url: str, settings: dict[str, CarSettings]) -> None:
        self.robot = robot
        self.url = url
        self.settings = settings
        self.cars: list[SimCar] = []

    def sim(
        self, key: str = "key-1", *, world: World | None = None, faults: Faults | None = None, **cfg: Any
    ) -> SimCar:
        options: dict[str, Any] = {
            "backoff_initial_s": 0.05,
            "backoff_max_s": 0.3,
            "snap_delay_scale": 0.25,
            "ota_step_s": 0.05,
            "reboot_s": 0.2,
            "seed": 1,
        }
        options.update(cfg)
        car = SimCar(SimConfig(url=self.url, key=key, **options), world=world or plain_world(), faults=faults)
        self.cars.append(car)
        return car

    async def connect(self, key: str = "key-1", **kwargs: Any) -> SimCar:
        car = self.sim(key, **kwargs)
        car.start()
        car_id = KEYS[key]
        await wait_for(
            lambda: car.welcomed.is_set() and (s := self.robot.car(car_id)) is not None and s.online
        )
        await wait_for(lambda: bool((s := self.robot.car(car_id)) and s.telemetry))
        return car

    async def close(self) -> None:
        for car in self.cars:
            await car.close()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "detector(factory): the object detector factory the robot uses")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def robot_config() -> RobotConfig:
    return fast_config()


@pytest.fixture
def detector_factory(request: pytest.FixtureRequest) -> DetectorFactory:
    marker = request.node.get_closest_marker("detector")
    return marker.args[0] if marker is not None else SimCatDetector  # type: ignore[no-any-return]


@pytest.fixture
async def harness(
    tmp_path: Path, robot_config: RobotConfig, detector_factory: DetectorFactory
) -> AsyncIterator[Harness]:
    settings = {car_id: fast_settings() for car_id in KEYS.values()}

    async def authenticate(key: str) -> str | None:
        return KEYS.get(key)

    async def car_settings(car_id: str) -> CarSettings:
        return settings.get(car_id, CarSettings())

    robot = Robot(
        data_dir=tmp_path,
        authenticate_car=authenticate,
        car_settings=car_settings,
        allow_insecure=True,
        config=robot_config,
        detector_factory=detector_factory,
    )
    await robot.start()
    app = Starlette(routes=[WebSocketRoute("/car/ws", robot.handle_car_socket)])
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning", ws_ping_interval=None))
    serve = asyncio.create_task(server.serve(sockets=[sock]))
    await wait_for(lambda: server.started or serve.done())
    h = Harness(robot, f"ws://127.0.0.1:{port}/car/ws", settings)
    try:
        yield h
    finally:
        await h.close()
        await robot.close()
        server.should_exit = True
        await serve
        sock.close()
