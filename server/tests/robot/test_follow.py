"""Follow: the background behaviour with fake detectors against the simulated car."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable

import pytest

from scout.contracts import CarBusy, CarError
from scout.robot import Detection, DetectorUnavailable, InvalidRequest
from scout.robot.detector import COCO_LABELS
from scout_sim import Cat, World

from .conftest import Harness, fast_physics, wait_for

pytestmark = pytest.mark.anyio


class ScriptedDetector:
    """Returns whatever ``script`` says for each frame."""

    def __init__(self, script: Callable[[bytes], list[Detection]]) -> None:
        self.script = script
        self.calls = 0

    @property
    def labels(self) -> frozenset[str]:
        return frozenset(COCO_LABELS)

    def detect(self, jpeg: bytes) -> list[Detection]:
        self.calls += 1
        return self.script(jpeg)


def cat_world() -> World:
    world = World.default(seed=2, cat=False, physics=fast_physics())
    world.place_car(110, 150, 0)
    world.cat = Cat(x=240, y=172, heading=math.pi / 2, wander=False)
    return world


def cat_distance(world: World) -> float:
    assert world.cat is not None
    return math.hypot(world.cat.x - world.car.x, world.cat.y - world.car.y)


def follow_state(harness: Harness) -> dict[str, object]:
    return harness.robot._cars["car-1"].follow or {}


async def test_follow_approaches_the_cat_and_stops_on_request(harness: Harness) -> None:
    car = await harness.connect(world=cat_world())
    start = cat_distance(car.world)
    result = await harness.robot.follow("car-1", target="kitty", seconds=60)
    assert result.data["result"] == "following started"
    assert result.data["target"] == "cat"
    await wait_for(lambda: harness.robot.car("car-1").activity == "following")  # type: ignore[union-attr]
    # Generous: CI runners render and detect far slower than a desktop.
    try:
        await wait_for(lambda: cat_distance(car.world) < start - 25, within=15)
    except AssertionError:
        raise AssertionError(
            f"no approach: {start:.0f} cm at the start, {cat_distance(car.world):.0f} cm now, "
            f"follow state {follow_state(harness)}"
        ) from None
    status = (await harness.robot.status("car-1")).data
    assert status["activity"] == "following"
    assert status["follow"]["target"] == "cat"
    assert status["follow"]["state"] == "following"
    assert status["follow"]["last_seen_s_ago"] is not None and status["follow"]["last_seen_s_ago"] < 1.5
    assert status["follow"]["moves"] > 0
    assert 0 <= status["follow"]["last_target"]["centre"] <= 1
    assert 0 < status["follow"]["last_target"]["height"] <= 1
    assert set(status["follow"]["last_step"]) == {"vx", "wz", "ms", "why"}
    assert 40 < status["follow"]["seconds_left"] <= 60
    assert "following the cat" in status["summary"]
    streams = [m for m in car.received if m.get("t") == "stream"]
    assert streams[0]["on"] is True and streams[0]["res"] == "qvga" and streams[0]["fps"] == 5
    stopped = await harness.robot.stop("car-1")
    assert stopped.data["cancelled"] == "following the cat"
    await wait_for(lambda: harness.robot.car("car-1").activity is None)  # type: ignore[union-attr]
    follow = (await harness.robot.status("car-1")).data["follow"]
    assert follow["state"] == "ended"
    assert follow["ended_because"] == "stop was requested"
    await wait_for(lambda: any(m.get("t") == "stream" and m.get("on") is False for m in car.received))
    assert cat_distance(car.world) > 11 + 12  # never ran into it
    assert harness.robot._detector is None  # the model is dropped once nobody follows


async def test_follow_ends_when_time_is_up(harness: Harness) -> None:
    car = await harness.connect(world=cat_world())
    await harness.robot.follow("car-1", seconds=5)
    await wait_for(lambda: follow_state(harness).get("state") == "ended", within=8)
    assert follow_state(harness)["ended_because"] == "the time was up"
    # The stream was re-sent before it ran out (3 s streams, re-sent every 1.5 s).
    assert sum(1 for m in car.received if m.get("t") == "stream" and m.get("on")) >= 3
    # The robot sends the stop before it marks the follow as ended, but the car may not
    # have read it yet.
    await wait_for(lambda: any(m.get("t") == "stop" for m in car.received))


@pytest.mark.detector.with_args(lambda: ScriptedDetector(lambda _jpeg: []))
async def test_follow_gives_up_when_nothing_is_seen(harness: Harness) -> None:
    await harness.connect()
    await harness.robot.follow("car-1", seconds=30)
    await wait_for(lambda: follow_state(harness).get("state") == "ended", within=4)
    assert follow_state(harness)["ended_because"] == "it couldn't see the cat for more than 0.8 seconds"
    assert harness.robot.car("car-1").activity is None  # type: ignore[union-attr]


def _flaky_cat() -> ScriptedDetector:
    seen = {"n": 0}

    def script(_jpeg: bytes) -> list[Detection]:
        seen["n"] += 1
        if seen["n"] <= 3:
            return [Detection("cat", 0.9, 0.45, 0.4, 0.55, 0.85)]  # centred and close: hold still
        return []

    return ScriptedDetector(script)


@pytest.mark.detector.with_args(_flaky_cat)
async def test_follow_ends_after_losing_the_target(harness: Harness) -> None:
    await harness.connect()
    await harness.robot.follow("car-1", seconds=30)
    await wait_for(lambda: follow_state(harness).get("state") == "ended", within=4)
    assert follow_state(harness)["ended_because"] == "it lost sight of the cat for more than 0.8 seconds"
    assert follow_state(harness)["last_seen"] is not None


async def test_motion_command_cancels_follow(harness: Harness) -> None:
    await harness.connect(world=cat_world())
    await harness.robot.follow("car-1", seconds=60)
    await wait_for(lambda: follow_state(harness).get("state") == "following")
    look = await harness.robot.look("car-1", size="small")  # looking interleaves with following
    assert look.data["resolution"] == "320x240"
    assert harness.robot.car("car-1").activity == "following"  # type: ignore[union-attr]
    result = await harness.robot.move("car-1", direction="back", ms=300)
    assert result.data["result"] == "completed"
    assert follow_state(harness)["state"] == "ended"
    assert follow_state(harness)["ended_because"] == "a new command arrived"


async def test_follow_ends_when_the_car_drops(harness: Harness) -> None:
    car = await harness.connect(world=cat_world())
    await harness.robot.follow("car-1", seconds=60)
    await wait_for(lambda: follow_state(harness).get("state") == "following")
    car.drop_connection()
    await wait_for(lambda: follow_state(harness).get("state") == "ended")
    assert follow_state(harness)["ended_because"] == "Scout disconnected"


async def test_follow_refusals(harness: Harness) -> None:
    await harness.connect(world=cat_world())
    with pytest.raises(InvalidRequest, match="can't recognise 'unicorn'"):
        await harness.robot.follow("car-1", target="unicorn")
    with pytest.raises(InvalidRequest, match="seconds"):
        await harness.robot.follow("car-1", seconds=2)
    look_around = asyncio.create_task(harness.robot.look_around("car-1", views=8))
    await wait_for(lambda: harness.robot.car("car-1").activity == "looking around")  # type: ignore[union-attr]
    with pytest.raises(CarBusy, match="busy looking around"):
        await harness.robot.follow("car-1")
    await harness.robot.stop("car-1")
    await look_around
    assert harness.robot._detector is None


def _unavailable() -> ScriptedDetector:
    raise DetectorUnavailable("the model couldn't be downloaded (ConnectError)")


@pytest.mark.detector.with_args(_unavailable)
async def test_follow_without_a_model(harness: Harness) -> None:
    await harness.connect()
    with pytest.raises(
        CarError,
        match=r"Following isn't available right now: the model couldn't be downloaded \(ConnectError\)\.",
    ):
        await harness.robot.follow("car-1")
    assert harness.robot.car("car-1").activity is None  # type: ignore[union-attr]


async def test_new_follow_replaces_the_old_one(harness: Harness) -> None:
    await harness.connect(world=cat_world())
    await harness.robot.follow("car-1", target="cat", seconds=60)
    await wait_for(lambda: follow_state(harness).get("state") == "following")
    await harness.robot.follow("car-1", target="dog", seconds=60)
    assert follow_state(harness)["target"] == "dog"
    await wait_for(lambda: follow_state(harness).get("state") == "ended", within=4)  # no dog in the room
    assert harness.robot._detector_users == 0
