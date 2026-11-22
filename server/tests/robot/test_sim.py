"""The simulator on its own: physics, sensors, the UNO rules, the camera, and the car
side of the link against a scripted server."""

from __future__ import annotations

import asyncio
import io
import json
import math
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from scout.robot.motion import PwmLayout, direction_velocity, duty_share, speed_share, wheel_output
from scout.robot.motion import percent_to_pwm as server_percent_to_pwm
from scout_sim import Cat, Disc, Faults, Physics, SimCar, SimCatDetector, SimConfig, World
from scout_sim.__main__ import main
from scout_sim.render import snapshot
from scout_sim.uno import SimUno
from scout_sim.world import (
    BOX,
    CAT,
    DISC,
    WALL,
    bits_duties,
    body_velocity,
    cast_rays,
    drive_wheels,
    mix,
    motor_speeds,
    move_duties,
    percent_to_pwm,
    wheel_bits,
)

from .conftest import wait_for

# -- mixing and physics ----------------------------------------------------------------

# protocol/uart.md: "Check values (all wheels at equal speed)", as unit vectors, the way
# firmware/tests/uno/mecanum_test.cpp checks them at several speeds.
UART_CHECK_VALUES = {
    "forward": ((1, 0, 0), 163),
    "back": ((-1, 0, 0), 92),
    "strafe left": ((0, -1, 0), 106),
    "strafe right": ((0, 1, 0), 149),
    "forward-left": ((1, -1, 0), 34),
    "forward-right": ((1, 1, 0), 129),
    "back-left": ((-1, -1, 0), 72),
    "back-right": ((-1, 1, 0), 20),
    "clockwise": ((0, 0, 1), 172),
    "counter-clockwise": ((0, 0, -1), 83),
    "stop": ((0, 0, 0), 0),
}
SHARED, SPLIT, IDEAL = Physics(pwm_model="shared"), Physics(), Physics(pwm_model="ideal")


@pytest.mark.parametrize(("vector", "bits"), UART_CHECK_VALUES.values(), ids=list(UART_CHECK_VALUES))
@pytest.mark.parametrize("speed", [100, 60, 50, 1])
def test_mixing_matches_the_uart_check_values(vector: tuple[int, int, int], bits: int, speed: int) -> None:
    vx, vy, wz = (c * speed for c in vector)
    assert wheel_bits(drive_wheels(vx, vy, wz, SHARED)) == bits


@pytest.mark.parametrize("direction", ["forward", "back", "left", "right", "forward_left", "back_right"])
def test_the_servers_directions_drive_the_check_values(direction: str) -> None:
    vx, vy, wz = direction_velocity(direction, 60)  # diagonals are 42, 42
    name = {"left": "strafe left", "right": "strafe right"}.get(direction, direction.replace("_", "-"))
    assert wheel_bits(drive_wheels(vx, vy, wz, SHARED)) == UART_CHECK_VALUES[name][1]


# firmware/tests/uno/mecanum_test.cpp: mixing_formulas and wheels_over_100_are_scaled_together.
@pytest.mark.parametrize(
    ("move", "wheels"),
    [
        ((30, 20, 10), (60, 20, 0, 40)),  # FL, RL, FR, RR
        ((100, 100, 0), (100, 0, 0, 100)),
        ((100, 50, 0), (100, 33, 33, 100)),
        ((-100, 100, -100), (-33, -100, -33, 33)),  # truncated toward zero
        ((60, 0, 40), (100, 100, 20, 20)),  # exactly 100: nothing to scale
    ],
)
def test_mixing_matches_the_firmware(move: tuple[int, int, int], wheels: tuple[int, int, int, int]) -> None:
    assert mix(*move) == wheels
    assert drive_wheels(*move, IDEAL) == wheels


# firmware/tests/uno/mecanum_test.cpp: slow_wheels_coast_below_a_quarter_of_the_fastest and
# split_layout_gives_each_side_its_own_duty.
@pytest.mark.parametrize(
    ("physics", "move", "bits"),
    [
        (SHARED, (40, 0, 30), 0x80 | 0x20),  # 70, 70, 10, 10: the right side coasts
        (SHARED, (100, 0, 60), 163),  # 100, 100, 25, 25 after scaling: exactly 25 % drives
        (SHARED, (100, 0, 61), 0x80 | 0x20),  # 100, 100, 24, 24: just below coasts
        (SHARED, (25, 0, 15), 163),  # 40, 40, 10, 10: exactly a quarter, driven
        (SHARED, (49, 0, 31), 0x80 | 0x20),  # 80, 80, 18, 18: below a quarter of 80
        (SHARED, (40, 0, 60), 0x80 | 0x20),  # backwards but slow coasts the same way
        (SHARED, (30, 0, 70), 0x80 | 0x20 | 0x04 | 0x08),  # 100, 100, -40, -40
        (SPLIT, (50, 0, 20), 163),
        (SPLIT, (40, 0, 30), 163),  # in split mode the slow side runs slowly
        (SPLIT, (60, 40, 0), 0x80 | 0x01),  # within one side the 25 % rule still applies
    ],
)
def test_coasting_matches_the_firmware(physics: Physics, move: tuple[int, int, int], bits: int) -> None:
    assert wheel_bits(drive_wheels(*move, physics)) == bits


def test_driven_wheels_run_at_their_pins_duty() -> None:
    assert drive_wheels(60, 0, 0, SHARED) == (60, 60, 60, 60)  # pure moves are exact
    assert drive_wheels(50, 0, 20, SHARED) == (70, 70, 70, 70)  # the turn share is lost
    assert drive_wheels(50, 0, 20, SPLIT) == (70, 70, 30, 30)  # each side its own duty
    left_on_d5 = Physics(pwm_model="split", pwm5_left=True)
    assert drive_wheels(50, 0, 20, left_on_d5) == (70, 70, 30, 30)  # same wheels, other pins
    # A strafe with a little rotation: all four at the shared duty, so it only strafes.
    assert body_velocity(drive_wheels(0, 38, 4, SHARED)) == pytest.approx((0, 42, 0))
    assert body_velocity(drive_wheels(0, 38, 4, IDEAL)) == pytest.approx((0, 38, 4))


def test_server_estimates_use_the_same_wheel_model() -> None:
    rng = random.Random(5)
    layouts: list[tuple[Physics, PwmLayout]] = [(SHARED, "shared"), (SPLIT, "split"), (IDEAL, "ideal")]
    for _ in range(3000):
        move = (rng.randint(-100, 100), rng.randint(-100, 100), rng.randint(-100, 100))
        for physics, layout in layouts:
            assert drive_wheels(*move, physics) == wheel_output(*move, layout), (move, layout)


def test_mixing_inverts_and_raw_test_bits() -> None:
    for v in ((60, 0, 0), (0, -40, 0), (0, 0, 30), (20, 15, -10)):
        assert body_velocity(mix(*v)) == pytest.approx(v)
    assert bits_duties(163, 153, 153) == (153, 153, 153, 153)
    assert bits_duties(106, 255, 0) == (0, 0, 255, -255)  # D6 at 0: left side still
    assert bits_duties(106, 255, 0, pwm5_left=True) == (-255, 255, 0, 0)  # D5 on the left


def test_motors_follow_the_measured_curve() -> None:
    p = Physics()
    # firmware percentToPwm: 1 % is min_pwm, 100 % is 255, linear in between.
    assert [percent_to_pwm(x, 90) for x in (0, 1, 20, 50, 99, 100)] == [0, 90, 122, 172, 253, 255]
    assert move_duties((50, -50, 0, 20), 110) == (182, -182, 0, 138)
    # The measured curve; below 100 a motor doesn't start.
    assert motor_speeds((255, -140, 99, 100), p) == pytest.approx(
        (100, -100 * duty_share(140), 0, 100 * duty_share(100))
    )
    # The server's estimates use the same curve (it leaves out the stall).
    for min_pwm in (0, 90, 110, 255):
        for x in range(101):
            assert percent_to_pwm(x, min_pwm) == server_percent_to_pwm(x, min_pwm)
            duty = percent_to_pwm(x, min_pwm)
            if duty >= p.start_pwm:
                assert motor_speeds((duty, 0, 0, 0), p)[0] == pytest.approx(100 * speed_share(x, min_pwm))


def test_car_moves_turns_and_is_blocked_by_walls() -> None:
    world = World.empty(physics=Physics(noise=0))
    world.place_car(100, 150, 0)
    world.step_car(mix(100, 0, 0), 1.0)  # 30 cm/s
    assert (world.car.x, world.car.y) == pytest.approx((130, 150))
    world.step_car(mix(0, 100, 0), 1.0)  # strafe right = −y when facing +x
    assert (world.car.x, world.car.y) == pytest.approx((130, 130))
    world.step_car(mix(0, 0, 50), 1.0)  # 90° clockwise
    assert math.degrees(world.car.heading) == pytest.approx(270)
    world.place_car(380, 150, 0)
    assert world.step_car(mix(100, 0, 0), 1.0) is False
    assert world.car.x == 380


def test_ultrasonic_and_ray_kinds() -> None:
    world = World.empty(physics=Physics(noise=0))
    world.place_car(100, 150, 0)
    assert world.ultrasonic(noise=False) == 290  # 400 − 100 − 10 cm sensor offset
    world.place_car(100, 150, 90)
    assert world.ultrasonic(noise=False) == 140
    far = World.empty(width=900)
    far.place_car(40, 150, 0)
    assert far.ultrasonic() is None  # nothing within 400 cm
    world.place_car(100, 150, 0)
    world.discs = (Disc(200, 150, 10, 40, (0, 0, 0), "post"),)
    assert world.ultrasonic(noise=False) == 80
    world.cat = Cat(x=150, y=150, heading=0, wander=False)
    assert world.ultrasonic(noise=False) == 29  # the cat's near side
    home = World.default(seed=1)
    home.place_car(110, 150, 0)
    angles = np.radians([0.0, 90.0, 180.0, 270.0])
    _dist, kind, _i, _u = cast_rays(home.scene(), 110, 150, angles, include_cat=True)
    assert set(kind.tolist()) <= {WALL, BOX, DISC, CAT}
    _dist, kind, _i, _u = cast_rays(home.scene(), 200, 120, np.radians([-90.0]), include_cat=False)
    assert kind.tolist() == [BOX]  # the box at (180..220, 30..70)


def test_cat_wanders_inside_the_room() -> None:
    world = World.default(seed=3)
    cat = world.cat
    assert cat is not None
    start = (cat.x, cat.y)
    for _ in range(3000):
        world.step_cat(0.05)
        assert not world.collides(cat.x, cat.y, cat.r, ignore_cat=True, ignore_car=True)
    assert (cat.x, cat.y) != start


# -- the UNO rules ---------------------------------------------------------------------


def uno_facing_wall(gap_cm: float) -> SimUno:
    world = World.empty(physics=Physics(noise=0), seed=7)
    world.place_car(400 - 10 - gap_cm, 150, 0)
    return SimUno(world, now=0.0)


def test_obstacle_rule_applies_only_going_forward() -> None:
    uno = uno_facing_wall(10)
    [done] = uno.move(1, 50, 0, 0, 1000, now=0.0)
    assert (done.reason, done.ms) == ("obstacle", 0)
    assert done.cm_start in (9, 10, 11)  # the echo has ±0.8 cm of noise
    assert not uno.moving
    for i, (vx, vy, wz) in enumerate(((-50, 0, 0), (0, 50, 0), (0, 0, 50)), start=2):
        assert uno.move(i, vx, vy, wz, 1000, now=0.0) == [] or uno.moving
        assert uno.moving
        uno.stop(now=0.0)
    assert uno.test(9, 163, 200, 200, 500, now=0.0) == []  # TEST never checks the distance
    assert uno.moving


def test_obstacle_stops_a_running_move() -> None:
    uno = uno_facing_wall(60)
    assert uno.move(1, 100, 0, 0, 3000, now=0.0) == []
    t, done = 0.0, []
    while not done:
        t += 0.02
        uno.host_frame(t)
        done = uno.tick(t)
    assert done[0].reason == "obstacle"
    assert done[0].cm_end is not None and done[0].cm_end < 15  # the reading that stopped it
    assert 1400 < done[0].ms < 1700  # 45 cm at 30 cm/s


def test_replacement_stop_time_and_link() -> None:
    uno = uno_facing_wall(200)
    uno.move(1, 0, 0, 50, 2000, now=0.0)
    [replaced] = uno.move(2, 0, 0, -50, 2000, now=0.5)
    assert (replaced.request_id, replaced.reason, replaced.ms) == (1, "replaced", 500)
    stopped = uno.stop(now=0.7)
    assert stopped is not None and (stopped.request_id, stopped.reason, stopped.ms) == (2, "stopped", 200)
    assert uno.stop(now=0.8) is None
    uno.move(3, -30, 0, 0, 400, now=1.0)
    uno.host_frame(1.2)
    [done] = uno.tick(1.45)
    assert (done.reason, done.ms) == ("time", 400)
    uno.link_ms = 300
    uno.move(4, -30, 0, 0, 3000, now=2.0)
    assert uno.tick(2.2) == []
    [lost] = uno.tick(2.35)
    assert lost.reason == "link"
    assert uno.set_tilt(90) == 40 and uno.set_tilt(-90) == -40  # kTiltMinDeg / kTiltMaxDeg


# -- the camera ------------------------------------------------------------------------


def as_array(jpeg: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(jpeg)) as img:
        return np.asarray(img.convert("RGB"), dtype=np.float32)


@pytest.mark.parametrize(
    ("res", "size"), [("qvga", (320, 240)), ("svga", (800, 600)), ("qxga", (2048, 1536))]
)
def test_frame_sizes(res: str, size: tuple[int, int]) -> None:
    world = World.default(seed=1)
    with Image.open(io.BytesIO(snapshot(world.scene(), res, 12, seed=1))) as img:
        assert img.format == "JPEG" and img.size == size


def test_frames_show_the_cat_and_follow_the_sensor_settings() -> None:
    world = World.default(seed=1, cat=False)
    world.place_car(110, 150, 0)
    world.cat = Cat(x=200, y=150, heading=math.pi / 2, wander=False)
    detector = SimCatDetector()
    upright = snapshot(world.scene(), "qvga", 12, seed=5)
    [cat] = detector.detect(upright)
    assert cat.label == "cat" and 0.4 < cat.cx < 0.6
    raw = snapshot(world.scene(), "qvga", 12, vflip=False, seed=5)  # no vflip: upside down
    assert np.abs(as_array(raw)[::-1] - as_array(upright)).mean() < 6
    assert np.abs(as_array(raw) - as_array(upright)).mean() > 15
    world.place_car(110, 150, 180)  # facing away from it
    assert detector.detect(snapshot(world.scene(), "qvga", 12, seed=5)) == []
    world.place_car(110, 150, 0)
    dark = as_array(snapshot(world.scene(), "qvga", 12, seed=5)).mean()
    lit = as_array(snapshot(world.scene(), "qvga", 12, light=100, seed=5)).mean()
    tilted = snapshot(world.scene(), "qvga", 12, tilt_deg=40, seed=5)
    assert lit > dark + 5
    assert np.abs(as_array(tilted) - as_array(upright)).mean() > 10


def test_render_cli_writes_a_frame(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "frame.jpg"
    assert main(["--render", str(out), "--res", "qvga", "--seed", "4", "--pwm", "split"]) == 0
    with Image.open(out) as img:
        assert img.size == (320, 240)
    assert "wrote" in capsys.readouterr().out


def test_cli_needs_a_key(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("SCOUT_CAR_KEY", raising=False)
    assert main(["--url", "ws://127.0.0.1:9/car/ws"]) == 2
    assert "car key is needed" in capsys.readouterr().err


# -- the car link, against a scripted server -------------------------------------------


class Script:
    """One connected car, seen from a hand-written server."""

    def __init__(self, ws: ServerConnection) -> None:
        self.ws = ws

    async def send(self, **msg: Any) -> None:
        await self.ws.send(json.dumps(msg))

    async def next(self, *, skip_tel: bool = True, wait: float = 3.0) -> dict[str, Any] | bytes:
        while True:
            async with asyncio.timeout(wait):
                raw = await self.ws.recv()
            if isinstance(raw, bytes):
                return raw
            msg: dict[str, Any] = json.loads(raw)
            if skip_tel and msg.get("t") in ("tel", "event"):
                continue
            return msg

    async def reply(self, **msg: Any) -> dict[str, Any]:
        await self.send(**msg)
        out = await self.next()
        assert isinstance(out, dict), out
        return out


@asynccontextmanager
async def scripted(
    *, refuse: bool = False
) -> AsyncIterator[tuple[str, asyncio.Queue[Script], list[Headers]]]:
    scripts: asyncio.Queue[Script] = asyncio.Queue()
    headers: list[Headers] = []

    def check(_conn: ServerConnection, request: Request) -> Response | None:
        headers.append(request.headers)
        return _conn.respond(HTTPStatus.FORBIDDEN, "no\n") if refuse else None

    async def handler(ws: ServerConnection) -> None:
        await scripts.put(Script(ws))
        await ws.wait_closed()

    async with serve(
        handler, "127.0.0.1", 0, process_request=check, ping_interval=None, compression=None
    ) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        yield f"ws://127.0.0.1:{port}/car/ws", scripts, headers


def sim_car(url: str, world: World | None = None, **cfg: Any) -> SimCar:
    options: dict[str, Any] = {
        "backoff_initial_s": 0.05,
        "backoff_max_s": 0.2,
        "snap_delay_scale": 0.1,
        "seed": 1,
    }
    options.update(cfg)
    return SimCar(SimConfig(url=url, key="k-1", **options), world=world or World.default(seed=1, cat=False))


@pytest.mark.anyio
async def test_hello_and_telemetry_match_the_spec() -> None:
    async with scripted() as (url, scripts, headers):
        car = sim_car(url, tel_interval_s=0.1)
        car.start()
        try:
            s = await scripts.get()
            hello = await s.next()
            assert isinstance(hello, dict)
            assert set(hello) == {
                "t",
                "fw",
                "uno_fw",
                "camera",
                "psram",
                "max_res",
                "mac",
                "ip",
                "ssid",
                "rssi",
                "reset",
            }
            assert hello["t"] == "hello" and hello["reset"] == "poweron" and hello["max_res"] == "qxga"
            assert headers[0]["Authorization"] == "Bearer k-1"
            assert headers[0]["X-Scout-Firmware"] == "0.1.0"
            tel = await s.next(skip_tel=False)
            assert isinstance(tel, dict) and tel["t"] == "tel"
            assert set(tel) == {"t", "cm", "line", "tilt", "moving", "light", "rssi", "heap", "uno", "up"}
            conf = {"obst_cm": 30, "min_pwm": 100, "link_ms": 800, "vflip": True, "hmirror": True}
            await s.send(t="welcome", car="car-1", name="Rover", time=1795300000, conf=conf, extra="ignored")
            await wait_for(car.welcomed.is_set)
            assert (car.uno.obst_cm, car.uno.min_pwm, car.uno.link_ms) == (30, 100, 800)
            assert car.vflip and car.hmirror
        finally:
            await car.close()


@pytest.mark.anyio
async def test_requests_get_the_replies_the_spec_says() -> None:
    async with scripted() as (url, scripts, _headers):
        car = sim_car(url)
        car.start()
        try:
            s = await scripts.get()
            await s.next()  # hello
            assert await s.reply(t="dance", id=1) == {
                "t": "err",
                "id": 1,
                "code": "unknown_type",
                "msg": "dance",
            }
            err = await s.reply(t="move", id=2, vx=500, vy=0, wz=0, ms=100)
            assert (err["t"], err["id"], err["code"]) == ("err", 2, "args")
            await s.send(t="move", vx=10, vy=0, wz=0, ms=100)  # no id: ignored
            await s.send(t="ping", id=0)  # ids are positive: ignored
            assert await s.reply(t="ping", id=3) == {"t": "pong", "id": 3}
            assert await s.reply(t="tilt", id=4, deg=90) == {"t": "ack", "id": 4, "deg": 40}
            assert await s.reply(t="light", id=5, level=30) == {"t": "ack", "id": 5, "level": 30}
            assert car.light == 30
            await s.send(t="snap", id=6, res="qvga", q=12)
            frame = await s.next()
            assert isinstance(frame, bytes) and frame[:8] == b"SCIM" + (6).to_bytes(4, "little")
            with Image.open(io.BytesIO(frame[8:])) as img:
                assert img.size == (320, 240)
            assert (await s.reply(t="snap", id=7, res="huge"))["code"] == "args"
            assert (await s.reply(t="conf", id=8, obst_cm=3))["code"] == "args"
            assert await s.reply(t="conf", id=9, obst_cm=20) == {"t": "ack", "id": 9}
            assert car.uno.obst_cm == 20
            assert await s.reply(t="move", id=10, vx=0, vy=0, wz=50, ms=200) == {"t": "ack", "id": 10}
            done = await s.next()
            assert isinstance(done, dict)
            assert (done["t"], done["id"], done["reason"], done["ms"]) == ("done", 10, "time", 200)
            assert {"cm_start", "cm_end"} <= set(done)
            assert await s.reply(t="move", id=11, vx=0, vy=0, wz=-50, ms=2000) == {"t": "ack", "id": 11}
            busy = await s.reply(
                t="ota", id=12, url="https://example.invalid/fw.bin", sha256="ab" * 32, size=1
            )
            assert (busy["t"], busy["code"]) == ("err", "busy")
            replies = [
                await s.reply(t="move", id=13, vx=-20, vy=0, wz=0, ms=150),
                await s.next(),
                await s.next(),
            ]
            by_id = {(m["id"], m["t"]): m for m in replies if isinstance(m, dict)}
            assert by_id[(11, "done")]["reason"] == "replaced"
            assert by_id[(13, "done")]["reason"] == "time"
            assert (13, "ack") in by_id
            assert await s.reply(t="stream", id=14, on=True, res="qvga", q=20, fps=10, secs=5) == {
                "t": "ack",
                "id": 14,
            }
            frames = [await s.next() for _ in range(3)]
            assert all(isinstance(f, bytes) and f[:8] == b"SCST" + (14).to_bytes(4, "little") for f in frames)
            await s.send(t="stream", id=15, on=False)
            while isinstance(reply := await s.next(), bytes):
                pass
            assert reply == {"t": "ack", "id": 15}
            with pytest.raises(TimeoutError):
                await s.next(wait=0.4)  # no more frames
            assert await s.reply(t="stop", id=16) == {"t": "ack", "id": 16}
        finally:
            await car.close()


@pytest.mark.anyio
async def test_uno_and_camera_faults_on_the_wire() -> None:
    async with scripted() as (url, scripts, _headers):
        car = sim_car(url)
        car.faults = Faults(uno_offline=True, camera_error=True)
        car.start()
        try:
            s = await scripts.get()
            hello = await s.next()
            assert isinstance(hello, dict) and hello["uno_fw"] is None
            assert (await s.reply(t="move", id=1, vx=10, vy=0, wz=0, ms=100))["code"] == "uno"
            assert (await s.reply(t="snap", id=2, res="vga"))["code"] == "camera"
            car.faults = Faults(uno_no_done=True)
            assert await s.reply(t="move", id=3, vx=0, vy=0, wz=20, ms=100) == {"t": "ack", "id": 3}
            done = await s.next()
            err = await s.next()
            assert isinstance(done, dict) and (done["reason"], done["ms"]) == ("error", 0)
            assert isinstance(err, dict) and (err["t"], err["id"], err["code"]) == ("err", 3, "timeout")
        finally:
            await car.close()


@pytest.mark.anyio
async def test_refusals_are_retried_with_backoff() -> None:
    async with scripted(refuse=True) as (url, _scripts, headers):
        car = sim_car(url)
        car.start()
        try:
            await wait_for(lambda: len(car.refusals) >= 3)
            assert set(car.refusals) == {403} and car.connections == 0
            assert len(headers) >= 3
        finally:
            await car.close()


@pytest.mark.anyio
async def test_drop_stops_the_motors_and_light_then_reconnects() -> None:
    async with scripted() as (url, scripts, _headers):
        car = sim_car(url)
        car.start()
        try:
            s = await scripts.get()
            await s.next()
            await s.reply(t="light", id=1, level=80)
            await s.reply(t="move", id=2, vx=0, vy=0, wz=30, ms=3000)
            assert car.uno.moving
            await s.ws.close(1001, "going away")
            again = await scripts.get()
            assert not car.uno.moving and car.light == 0
            hello = await again.next()
            assert isinstance(hello, dict) and hello["t"] == "hello"
            assert car.connections == 2 and car.close_codes == [1001]  # hello came, so it counted
        finally:
            await car.close()


@pytest.mark.anyio
async def test_car_leaves_a_silent_server() -> None:
    async with scripted() as (url, scripts, _headers):
        car = sim_car(url, server_silence_s=0.4)
        car.start()
        try:
            first = await scripts.get()
            await first.next()
            await scripts.get()  # it gave up on the first connection and came back
            assert first.ws.close_code == 1000
            await wait_for(lambda: car.connections == 2)
        finally:
            await car.close()


@pytest.mark.anyio
async def test_ota_progress_and_reboot() -> None:
    async with scripted() as (url, scripts, _headers):
        car = sim_car(url, ota_step_s=0.01, reboot_s=0.05)
        car.start()
        try:
            s = await scripts.get()
            await s.next()
            await s.send(t="ota", id=1, url="https://example.invalid/fw.bin", sha256="ab" * 32, size=1000)
            states = []
            while True:
                msg = await s.next()
                assert isinstance(msg, dict) and msg["t"] == "ota" and msg["id"] == 1
                states.append((msg["state"], msg.get("pct")))
                if msg["state"] in ("rebooting", "failed"):
                    break
            assert states[0] == ("downloading", 0) and ("downloading", 100) in states
            assert states[-2:] == [("verifying", 100), ("rebooting", 100)]
            again = await scripts.get()
            hello = await again.next()
            assert isinstance(hello, dict) and (hello["fw"], hello["reset"]) == ("0.1.1", "ota")
        finally:
            await car.close()
