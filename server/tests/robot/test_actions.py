"""Tool actions end to end: Robot ⇄ real WebSocket ⇄ simulated car."""

from __future__ import annotations

import asyncio
import io
import json
import math
import time

import pytest
from PIL import Image

from scout.contracts import CarBusy, CarError
from scout.robot import InvalidRequest, RobotConfig
from scout_sim import Disc, Faults, SimCar, World

from .conftest import Harness, fast_physics, plain_world, wait_for

pytestmark = pytest.mark.anyio


def size_of(jpeg: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(jpeg)) as img:
        assert img.format == "JPEG"
        return img.size


def heading_deg(world: World) -> float:
    return math.degrees(world.car.heading) % 360


def angle_diff(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


# -- motion ----------------------------------------------------------------------------


async def test_move_forward_completes_with_estimates(harness: Harness) -> None:
    car = await harness.connect()
    x0 = car.world.car.x
    result = await harness.robot.move("car-1", direction="forward", ms=500, speed=60)
    data = result.data
    assert data["result"] == "completed"
    assert data["reason"] == "time"
    assert data["ms"] == 500
    # 120 cm/s × 0.71 × 0.5 s: 60 % is duty 188, which turns the motors at (188 − 25)/230
    # of full speed.
    assert data["estimated_cm"] == pytest.approx(42.5)
    assert data["distance_before_cm"] == pytest.approx(280, abs=3)
    assert data["distance_after_cm"] == pytest.approx(237.5, abs=4)
    assert car.world.car.x - x0 == pytest.approx(42.5, abs=3)
    assert "completed" in data["summary"]
    assert result.images == []


@pytest.mark.parametrize(
    ("direction", "forward", "right"),
    # Straight: 60 % runs the wheels at 71 %. Diagonal: two wheels at 84 % (88 %), the others still.
    [("left", 0.0, -35.4), ("back", -42.5, 0.0), ("forward_right", 26.5, 22.1), ("back_left", -26.5, -22.1)],
)
async def test_move_directions_match_the_car(
    harness: Harness, direction: str, forward: float, right: float
) -> None:
    car = await harness.connect()
    x0, y0 = car.world.car.x, car.world.car.y
    result = await harness.robot.move("car-1", direction=direction, ms=500, speed=60)  # type: ignore[arg-type]
    assert result.data["result"] == "completed"
    assert result.data["estimated_cm"] == pytest.approx(math.hypot(forward, right), abs=0.2)
    # The car faces +x, so forward is +x and right is −y.
    assert car.world.car.x - x0 == pytest.approx(forward, abs=3)
    assert y0 - car.world.car.y == pytest.approx(right, abs=3)


async def test_turn_right_and_left(harness: Harness) -> None:
    car = await harness.connect()
    result = await harness.robot.turn("car-1", direction="right", degrees=90, speed=50)
    assert result.data["result"] == "completed"
    assert result.data["estimated_degrees"] == 90
    assert result.data["moves"] == 1
    assert angle_diff(heading_deg(car.world), 270) < 3  # clockwise from 0°
    result = await harness.robot.turn("car-1", direction="left", degrees=45, speed=100, look=True)
    assert result.data["estimated_degrees"] == 45
    assert angle_diff(heading_deg(car.world), 315) < 3
    assert len(result.images) == 1 and size_of(result.images[0]) == (640, 480)
    assert result.data["picture"] == "640x480"


async def test_drive_raw_and_validation(harness: Harness) -> None:
    car = await harness.connect()
    result = await harness.robot.drive("car-1", vx=0, vy=0, wz=-50, ms=200)
    assert result.data["estimated"]["clockwise_deg"] == pytest.approx(-92.0)  # 720°/s × 147/230 × 0.2 s
    assert angle_diff(heading_deg(car.world), 92) < 3
    for kwargs in ({"vx": 101, "vy": 0, "wz": 0, "ms": 500}, {"vx": 0, "vy": 0, "wz": 0, "ms": 50}):
        with pytest.raises(InvalidRequest):
            await harness.robot.drive("car-1", **kwargs)
    with pytest.raises(InvalidRequest, match="ms must be from 100 to 3000"):
        await harness.robot.move("car-1", direction="forward", ms=5000)
    with pytest.raises(InvalidRequest, match="speed"):
        await harness.robot.move("car-1", direction="forward", ms=500, speed=10)
    with pytest.raises(InvalidRequest, match="direction must be one of"):
        await harness.robot.move("car-1", direction="up", ms=500)  # type: ignore[arg-type]
    with pytest.raises(InvalidRequest, match="degrees"):
        await harness.robot.turn("car-1", direction="left", degrees=0)


async def test_obstacle_stop_only_going_forward(harness: Harness) -> None:
    world = plain_world()
    world.place_car(300, 150, 0)  # the wall is 90 cm ahead of the sensor
    await harness.connect(world=world)
    result = await harness.robot.move("car-1", direction="forward", ms=3000, speed=60)
    data = result.data
    assert data["reason"] == "obstacle"
    assert data["result"].startswith("stopped early: something ")
    assert data["result"].endswith(" cm ahead")
    assert data["distance_after_cm"] < 15
    assert 800 < data["ms"] < 1500
    again = await harness.robot.move("car-1", direction="forward", ms=500)
    # Right at the threshold the echo noise decides between "never started" and "stopped
    # after one sensor reading"; either way it barely moves.
    assert again.data["reason"] == "obstacle"
    assert again.data["ms"] <= 100
    back = await harness.robot.move("car-1", direction="back", ms=300)
    assert back.data["result"] == "completed"
    side = await harness.robot.move("car-1", direction="left", ms=300)
    assert side.data["result"] == "completed"


async def test_stop_interrupts_a_running_move_at_once(harness: Harness) -> None:
    await harness.connect()
    move = asyncio.create_task(harness.robot.move("car-1", direction="forward", ms=3000, speed=30))
    await asyncio.sleep(0.4)
    started = time.monotonic()
    stopped = await harness.robot.stop("car-1")
    assert time.monotonic() - started < 0.5
    assert stopped.data["result"] == "stopped"
    assert stopped.data["was_moving"] is True
    result = await move
    assert result.data["result"] == "stopped early: stop was requested"
    assert 250 < result.data["ms"] < 900


async def test_stop_cancels_motion_waiting_its_turn(harness: Harness) -> None:
    await harness.connect()
    first = asyncio.create_task(harness.robot.move("car-1", direction="forward", ms=2000, speed=30))
    await asyncio.sleep(0.2)
    second = asyncio.create_task(harness.robot.move("car-1", direction="back", ms=2000, speed=30))
    await asyncio.sleep(0.1)
    await harness.robot.stop("car-1")
    assert (await first).data["reason"] == "stopped"
    assert (await second).data["result"] == "cancelled: stop was requested before it started"


async def test_link_loss_on_the_car_ends_the_move(harness: Harness) -> None:
    harness.settings["car-1"].link_ms = 300
    faults = Faults()
    car = await harness.connect(faults=faults)
    assert car.uno.link_ms == 300
    faults.uno_link_lost = True
    result = await harness.robot.move("car-1", direction="forward", ms=2000, speed=30)
    assert result.data["reason"] == "link"
    assert result.data["result"] == "stopped early: the motor board lost contact with the camera board"


async def test_motor_board_faults(harness: Harness) -> None:
    faults = Faults()
    await harness.connect(faults=faults)
    faults.uno_no_done = True
    with pytest.raises(CarError, match="motor board didn't confirm the end of the move"):
        await harness.robot.move("car-1", direction="forward", ms=100)
    faults.uno_no_done = False
    faults.uno_offline = True
    with pytest.raises(CarError, match="motor board isn't answering"):
        await harness.robot.move("car-1", direction="forward", ms=300)
    await wait_for(lambda: not harness.robot.car("car-1").uno_ok)  # type: ignore[union-attr]
    status = await harness.robot.status("car-1")
    assert status.data["motor_board_ok"] is False
    assert "motor board isn't answering" in status.data["summary"]
    assert any(e["kind"] == "uno_lost" for e in status.data["recent_events"])


async def test_wheel_test(harness: Harness) -> None:
    car = await harness.connect()
    x0 = car.world.car.x
    result = await harness.robot.wheel_test("car-1", bits=163, pwm5=153, pwm6=153, ms=300)
    assert result.data["result"] == "completed"
    assert result.data["bits"] == "10100011"
    assert car.world.car.x - x0 == pytest.approx(21.6, abs=2)  # all four forward at 60 %
    sent = [m for m in car.received if m.get("t") == "test"]
    assert sent[-1] | {"id": 0} == {"t": "test", "id": 0, "bits": 163, "pwm5": 153, "pwm6": 153, "ms": 300}
    with pytest.raises(InvalidRequest):
        await harness.robot.wheel_test("car-1", bits=256, pwm5=0, pwm6=0, ms=100)


# -- looking ---------------------------------------------------------------------------


async def test_look_sizes_tilt_and_light(harness: Harness) -> None:
    car = await harness.connect()
    result = await harness.robot.look("car-1", tilt=10, light=50, size="small")
    assert size_of(result.images[0]) == (320, 240)
    data = result.data
    assert data["resolution"] == "320x240"
    assert data["tilt_deg"] == 10
    assert data["light"] == 50
    assert isinstance(data["distance_cm"], int)
    assert car.light == 50 and car.uno.tilt == 10
    result = await harness.robot.look("car-1", size="large", tilt=80)
    assert size_of(result.images[0]) == (800, 600)
    assert result.data["tilt_deg"] == 40  # the UNO clamps to ±40°
    assert result.data["summary"].endswith("The camera can't tilt past 40°, so it stopped there.")
    assert car.uno.tilt == 40
    result = await harness.robot.look("car-1", size="small", tilt=-60)
    assert result.data["tilt_deg"] == -40
    result = await harness.robot.look("car-1")
    assert result.data["resolution"] == "640x480"
    with pytest.raises(InvalidRequest):
        await harness.robot.look("car-1", size="huge")  # type: ignore[arg-type]


async def test_set_light(harness: Harness) -> None:
    car = await harness.connect()
    result = await harness.robot.set_light("car-1", level=100)
    assert result.data == {"light": 100, "summary": "Scout's light is now at 100 %."}
    assert car.light == 100
    assert (await harness.robot.set_light("car-1", level=0)).data["summary"] == "Scout's light is now off."
    with pytest.raises(InvalidRequest):
        await harness.robot.set_light("car-1", level=101)


async def test_camera_error(harness: Harness) -> None:
    await harness.connect(faults=Faults(camera_error=True))
    with pytest.raises(CarError, match="camera failed to take the picture"):
        await harness.robot.look("car-1")
    await wait_for(lambda: True)
    status = await harness.robot.status("car-1")
    assert any(e["kind"] == "camera_error" for e in status.data["recent_events"])


async def test_status_online(harness: Harness) -> None:
    await harness.connect()
    data = (await harness.robot.status("car-1")).data
    assert data["online"] is True
    assert data["summary"].startswith("Scout is online. Front distance ")
    for key in ("firmware", "camera", "wifi", "distance_cm", "line_sensors", "tilt_deg", "light", "activity"):
        assert key in data
    assert data["wifi"]["ssid"] == "sim-wifi"
    assert isinstance(data["wifi"]["rssi"], int)
    assert len(data["line_sensors"]) == 3
    assert data["activity"] is None
    json.dumps(data)


async def test_look_around_composite(harness: Harness) -> None:
    car = await harness.connect()
    result = await harness.robot.look_around("car-1", views=6)
    data = result.data
    assert data["result"] == "completed"
    assert [v["heading_deg"] for v in data["views"]] == [0, 60, 120, 180, 240, 300]
    assert [v["direction"] for v in data["views"]][:2] == ["0° ahead", "60° right"]
    assert data["views"][3]["direction"] == "180° behind"
    assert all(isinstance(v["distance_cm"], int) for v in data["views"])
    assert data["turned_deg"] == 360
    assert len(result.images) == 1
    width, height = size_of(result.images[0])
    assert 600 < width <= 1280
    assert height < width
    assert angle_diff(heading_deg(car.world), 0) < 5  # back where it started
    assert sum(1 for m in car.received if m.get("t") == "snap" and m.get("res") == "vga") == 6
    assert harness.robot.car("car-1").activity is None  # type: ignore[union-attr]


async def test_look_around_can_be_stopped(harness: Harness) -> None:
    await harness.connect()
    task = asyncio.create_task(harness.robot.look_around("car-1", views=8))
    await wait_for(lambda: harness.robot.car("car-1").activity == "looking around")  # type: ignore[union-attr]
    with pytest.raises(CarBusy, match="busy looking around"):
        await harness.robot.look("car-1")
    await asyncio.sleep(0.3)
    await harness.robot.stop("car-1")
    result = await task
    assert result.data["result"] == "stopped early: stop was requested"
    assert 1 <= len(result.data["views"]) < 8
    assert len(result.images) == 1


# -- captures --------------------------------------------------------------------------


async def test_capture_photo_files_and_meta(harness: Harness) -> None:
    await harness.connect()
    first = await harness.robot.capture_photo("car-1", user_id=7)
    capture_id = first.capture_id
    assert capture_id is not None and first.data["capture_id"] == capture_id
    assert len(capture_id) >= 12
    assert first.data["photo_count"] == 1
    assert (first.data["width"], first.data["height"]) == (2048, 1536)  # the car's max_res
    assert size_of(first.images[0])[0] <= 480
    second = await harness.robot.capture_photo(
        "car-1", user_id=7, capture_id=capture_id, note="  the  front door "
    )
    assert second.capture_id == capture_id
    assert second.data["photo_count"] == 2
    folder = harness.robot.capture_dir(capture_id)
    assert folder is not None
    assert sorted(p.name for p in folder.iterdir()) == ["001.jpg", "002.jpg", "meta.json"]
    assert size_of((folder / "002.jpg").read_bytes()) == (2048, 1536)
    meta = json.loads((folder / "meta.json").read_text())
    assert set(meta) == {"capture_id", "car_id", "user_id", "created", "photos"}
    assert meta["capture_id"] == capture_id and meta["car_id"] == "car-1" and meta["user_id"] == 7
    assert meta["created"].endswith("+00:00")
    assert [p["file"] for p in meta["photos"]] == ["001.jpg", "002.jpg"]
    assert meta["photos"][1]["note"] == "the front door"
    assert meta["photos"][0]["note"] is None
    assert set(meta["photos"][0]) == {"file", "time", "note", "cm", "heading_deg", "width", "height"}
    assert (meta["photos"][0]["width"], meta["photos"][0]["height"]) == (2048, 1536)
    q = [m["q"] for m in harness.cars[0].received if m.get("t") == "snap"]
    assert q == [10, 10]
    with pytest.raises(InvalidRequest, match="belongs to someone else"):
        await harness.robot.capture_photo("car-1", user_id=8, capture_id=capture_id)
    with pytest.raises(InvalidRequest, match="isn't valid"):
        await harness.robot.capture_photo("car-1", user_id=7, capture_id="../../etc")
    with pytest.raises(InvalidRequest, match="no capture called"):
        await harness.robot.capture_photo("car-1", user_id=7, capture_id="A" * 16)


async def test_capture_dir_rejects_bad_ids(harness: Harness) -> None:
    for bad in ("", "..", "../captures", "short", "a/b" * 6, "A" * 16, "A" * 65, "abc\\defghijklmn"):
        assert harness.robot.capture_dir(bad) is None


def orbit_world(post_r: float = 4, **physics: object) -> World:
    """An empty room with a post; the car faces it from 25 cm.

    The echo has no noise here, so every reading follows from the geometry and the orbit
    makes the same decisions on every run, however busy the machine is. (With noise the
    reading on the post's far side drew 14-20 cm at random.)
    """
    world = World.empty(physics=fast_physics(echo_noise_cm=0.0, **physics), seed=1)
    world.discs = (Disc(200, 150, post_r, 60, (90, 140, 90), "post"),)
    world.place_car(200 - post_r - 25 - 10, 150, 0)
    return world


def assert_facing_post(world: World, low: float, high: float) -> None:
    dx, dy = 200 - world.car.x, 150 - world.car.y
    assert low < math.hypot(dx, dy) < high
    assert angle_diff(heading_deg(world), math.degrees(math.atan2(dy, dx))) < 12


def corrections(car: SimCar) -> list[int]:
    """The orbit's radius corrections: the only moves with a vx, signed."""
    return [m["vx"] for m in car.received if m.get("t") == "move" and m["vx"] != 0]


async def test_capture_orbit_split(robot_config: RobotConfig, harness: Harness) -> None:
    robot_config.orbit_speed = 80
    car = await harness.connect(world=orbit_world(), max_res="vga")
    result = await harness.robot.capture_orbit("car-1", user_id=3, photos=4, direction="left")
    data = result.data
    assert data["result"] == "completed", data
    assert data["photo_count"] == 4
    assert data["radius_cm"] == pytest.approx(25, abs=2)
    assert [p["heading_deg"] for p in data["photos"]] == [0, 90, 180, 270]
    assert result.capture_id == data["capture_id"]
    folder = harness.robot.capture_dir(data["capture_id"])
    assert folder is not None
    meta = json.loads((folder / "meta.json").read_text())
    assert [p["file"] for p in meta["photos"]] == ["001.jpg", "002.jpg", "003.jpg", "004.jpg"]
    assert [p["heading_deg"] for p in meta["photos"]] == [0.0, 90.0, 180.0, 270.0]
    assert meta["photos"][2]["note"] == "orbit 3/4"
    assert len(result.images) == 1 and size_of(result.images[0])[0] <= 1280
    # A left orbit goes clockwise around the post: west, north, east, then south.
    assert_facing_post(car.world, 30, 50)
    assert car.world.car.y < 150
    # The circle is centred on the post's front surface, so its far side (east) reads
    # 25 - 2 × 4 = 17 cm; the car backs onto the circle once, there.
    assert data["photos"][2]["distance_cm"] == 25
    assert corrections(car) == [-30]


async def test_capture_orbit_backs_off_a_deep_object(robot_config: RobotConfig, harness: Harness) -> None:
    """A wider post: on its far side the car comes within 14 cm of it, below the 15 cm
    obstacle distance. That is the orbit's own geometry (the circle is centred on the
    surface the car faces), not something in the way, so the car backs onto the circle
    and carries on."""
    robot_config.orbit_speed = 80
    car = await harness.connect(world=orbit_world(post_r=6), max_res="vga")
    result = await harness.robot.capture_orbit("car-1", user_id=3, photos=4, direction="left")
    data = result.data
    assert data["result"] == "completed", data
    assert data["photo_count"] == 4
    readings = [m["cm_end"] for m in car.sent if m.get("t") == "done"]
    assert min(readings) == 14 < harness.settings["car-1"].obst_cm
    # Back on the circle, within the sensor's noise.
    assert all(abs(p["distance_cm"] - 25) <= 1 for p in data["photos"]), data["photos"]
    assert corrections(car) == [-30, -30, -30]
    assert_facing_post(car.world, 30, 52)


async def test_capture_orbit_stops_when_something_comes_close(
    robot_config: RobotConfig, harness: Harness
) -> None:
    robot_config.orbit_speed = 80
    world = orbit_world()
    # A cup between the car and the post once it has gone 45° around: clear of the car's
    # path and out of the sensor's cone at the start, then 6 cm in front of it, well
    # inside half the orbit radius. Something is in the way.
    world.discs = (*world.discs, Disc(184, 162, 2, 30, (200, 200, 200), "cup"))
    await harness.connect(world=world, max_res="vga")
    result = await harness.robot.capture_orbit("car-1", user_id=3, photos=8, direction="left")
    data = result.data
    assert data["result"].startswith("stopped early: something came within "), data
    assert data["photo_count"] == 1


async def test_capture_orbit_blend_with_per_wheel_speeds(robot_config: RobotConfig, harness: Harness) -> None:
    robot_config.orbit_speed = 80
    robot_config.orbit_mode = "blend"
    car = await harness.connect(world=orbit_world(pwm_model="ideal"), max_res="vga")
    result = await harness.robot.capture_orbit("car-1", user_id=3, photos=4, direction="right")
    assert result.data["result"] == "completed"
    assert_facing_post(car.world, 30, 50)
    assert car.world.car.y > 150  # counter-clockwise: west, south, east, then north


async def test_capture_orbit_refuses_without_a_target(harness: Harness) -> None:
    await harness.connect()  # 280 cm of empty floor ahead
    with pytest.raises(CarError, match="too far to circle"):
        await harness.robot.capture_orbit("car-1", user_id=1)
    with pytest.raises(InvalidRequest):
        await harness.robot.capture_orbit("car-1", user_id=1, photos=3)


async def test_capture_orbit_keeps_photos_when_the_car_drops(
    robot_config: RobotConfig, harness: Harness
) -> None:
    robot_config.orbit_speed = 30
    car = await harness.connect(world=orbit_world(), max_res="vga")
    task = asyncio.create_task(harness.robot.capture_orbit("car-1", user_id=3, photos=8))
    await wait_for(lambda: harness.robot.car("car-1").activity == "capturing")  # type: ignore[union-attr]
    await wait_for(lambda: sum(1 for m in car.received if m.get("t") == "snap") >= 2, within=10)
    await asyncio.sleep(0.3)
    car.drop_connection()
    result = await task
    data = result.data
    assert data["result"].startswith("stopped early: Scout disconnected")
    assert 1 <= data["photo_count"] < 8
    folder = harness.robot.capture_dir(data["capture_id"])
    assert folder is not None
    assert len(json.loads((folder / "meta.json").read_text())["photos"]) == data["photo_count"]


# -- admin -----------------------------------------------------------------------------


async def test_push_settings(harness: Harness) -> None:
    car = await harness.connect()
    harness.settings["car-1"].obst_cm = 30
    harness.settings["car-1"].vflip = False
    await harness.robot.push_settings("car-1")
    assert car.uno.obst_cm == 30
    assert car.vflip is False
    conf = [m for m in car.received if m.get("t") == "conf"][-1]
    assert conf["obst_cm"] == 30 and conf["link_ms"] == 1000


async def test_stop_everything_is_quiet_when_offline(harness: Harness) -> None:
    await harness.robot.stop_everything("car-9")
    car = await harness.connect()
    await harness.robot.stop_everything("car-1")
    await wait_for(lambda: any(m.get("t") == "stream" and m.get("on") is False for m in car.received))


async def test_firmware_update_progress(harness: Harness) -> None:
    car = await harness.connect()
    states: list[str] = []

    async def watch() -> None:
        while True:
            s = harness.robot.car("car-1")
            if s is not None and s.ota_state and (not states or states[-1] != s.ota_state):
                states.append(s.ota_state)
            await asyncio.sleep(0.005)

    watcher = asyncio.create_task(watch())
    await harness.robot.update_firmware(
        "car-1", url="https://example.invalid/fw.bin", sha256="ab" * 32, size=1000
    )
    assert harness.robot.car("car-1").activity == "updating"  # type: ignore[union-attr]
    with pytest.raises(CarBusy, match="installing a firmware update"):
        await harness.robot.move("car-1", direction="forward", ms=300)
    await wait_for(lambda: car.connections == 2 and car.welcomed.is_set())
    await wait_for(lambda: (harness.robot.car("car-1").ota_state or "").startswith("finished"))  # type: ignore[union-attr]
    watcher.cancel()
    summary = harness.robot.car("car-1")
    assert summary is not None
    assert summary.ota_state == "finished: now running 0.1.1"
    assert summary.firmware == "0.1.1"
    assert summary.activity is None
    assert "downloading 0%" in states and "downloading 100%" in states
    assert "verifying" in states and "rebooting" in states
    assert car.reset_reason == "ota"


async def test_firmware_update_refusals(harness: Harness) -> None:
    faults = Faults(ota_fail=True)
    await harness.connect(faults=faults)
    with pytest.raises(InvalidRequest, match="https"):
        await harness.robot.update_firmware(
            "car-1", url="http://example.invalid/fw.bin", sha256="ab" * 32, size=1
        )
    with pytest.raises(InvalidRequest, match="sha256"):
        await harness.robot.update_firmware(
            "car-1", url="https://example.invalid/fw.bin", sha256="xyz", size=1
        )
    move = asyncio.create_task(harness.robot.move("car-1", direction="forward", ms=1000, speed=30))
    await asyncio.sleep(0.2)
    with pytest.raises(CarBusy, match="moving"):
        await harness.robot.update_firmware(
            "car-1", url="https://example.invalid/fw.bin", sha256="ab" * 32, size=10
        )
    await move
    await harness.robot.update_firmware(
        "car-1", url="https://example.invalid/fw.bin", sha256="ab" * 32, size=10
    )
    await wait_for(lambda: (harness.robot.car("car-1").ota_state or "").startswith("failed"))  # type: ignore[union-attr]
    assert harness.robot.car("car-1").ota_state == "failed: sha256 mismatch"  # type: ignore[union-attr]
    assert harness.robot.car("car-1").activity is None  # type: ignore[union-attr]
    result = await harness.robot.move("car-1", direction="back", ms=100)
    assert result.data["result"] == "completed"
