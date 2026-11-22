"""Follow: the background behaviour with fake detectors against the simulated car."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable

import numpy as np
import pytest

from scout.contracts import CarBusy, CarError
from scout.robot import Detection, DetectorUnavailable, InvalidRequest
from scout.robot.detector import COCO_LABELS
from scout.robot.robot import _check, _Checking
from scout.robot.tracker import Choice
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
    assert set(status["follow"]["last_step"]) == {"vx", "wz", "ms", "tilt", "why"}
    assert -40 <= status["follow"]["camera_tilt_deg"] <= 40  # starts level for a cat
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
    car = await harness.connect()
    heading = car.world.car.heading
    started = await harness.robot.follow("car-1", seconds=30)
    assert started.data["state"] == "searching"
    assert "is turning to look for it" in started.data["summary"]
    assert started.images == []
    await wait_for(lambda: follow_state(harness).get("state") == "ended", within=10)
    follow = (await harness.robot.status("car-1")).data["follow"]
    assert follow["ended_because"] == "it couldn't find a cat after turning 120° to look"
    assert follow["moves"] == 3 and follow["searches"] == 1 and follow["found_again"] == 0
    turned = math.degrees(heading - car.world.car.heading) % 360  # turning right lowers the heading
    assert 60 < turned < 180, turned
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
async def test_follow_searches_after_losing_the_target(harness: Harness) -> None:
    await harness.connect()
    started = await harness.robot.follow("car-1", seconds=30)
    assert started.data["state"] == "following"
    assert len(started.images) == 1  # the picture it locked on in
    assert "locked on to the biggest cat in view, number 1 in the green box" in started.data["summary"]
    await wait_for(lambda: follow_state(harness).get("state") == "searching", within=4)
    steps: list[str] = []

    def record() -> bool:
        step = follow_state(harness).get("last_step")
        if isinstance(step, dict) and (not steps or steps[-1] != step["why"]):
            steps.append(step["why"])
        return follow_state(harness).get("state") == "ended"

    await wait_for(record, within=15)
    assert (
        follow_state(harness)["ended_because"]
        == "it lost sight of the cat and didn't find it after turning 120°"
    )
    assert follow_state(harness)["last_seen"] is not None
    # It vanished in the middle of the picture, close by: it looked again, then turned. It
    # doesn't drive towards where it was, because one that close has gone past to a side.
    assert steps[0] == "search: looking again, the cat vanished in the middle of the picture"
    assert not any(why.startswith("search: forward about") for why in steps), steps
    assert steps[-1].endswith("(120° of 120°)"), steps


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
    await wait_for(lambda: follow_state(harness).get("state") == "ended", within=10)  # no dog in the room
    assert harness.robot._detector_users == 0


# -------------------------------------------------- following a particular one


async def test_find_numbers_what_it_sees_and_follow_takes_the_pick(harness: Harness) -> None:
    await harness.connect(world=cat_world())
    found = await harness.robot.find("car-1")
    assert len(found.images) == 1
    assert found.data["resolution"] == "640x480"
    assert [(f["number"], f["label"]) for f in found.data["found"]] == [(1, "cat")]
    assert found.data["found"][0]["direction"].endswith(("left", "right", "straight ahead"))
    assert "call follow with pick set to its number" in found.data["summary"]
    assert harness.robot._detector is None  # dropped straight away with no linger (tests)

    started = await harness.robot.follow("car-1", pick=1, seconds=60)
    assert started.data["state"] == "following"
    assert started.data["target"] == "cat"
    assert "locked on to the one you picked, number 1 in the green box" in started.data["summary"]
    assert "follow_view" in started.data["summary"]
    assert len(started.images) == 1
    assert started.data["in_view"][0]["following"] is True

    view = await harness.robot.follow_view("car-1")
    assert len(view.images) == 2  # what it acts on, and the cat as it was when picked
    assert view.data["result"] == "following"
    assert view.data["picture_age_s"] < 2
    following = [item for item in view.data["in_view"] if item["following"]]
    assert len(following) == 1 and following[0]["match"] > 0.6
    assert "in the green box, number 1" in view.data["summary"]
    assert "call follow with pick set to a number from this picture" in view.data["summary"]
    status = (await harness.robot.status("car-1")).data["follow"]
    assert status["picked"] is True and status["match"] > 0.6 and status["in_view"] == 1

    await harness.robot.stop("car-1")
    await wait_for(lambda: follow_state(harness).get("state") == "ended")
    ended = await harness.robot.follow_view("car-1")
    assert ended.data["result"] == "ended"
    assert ended.data["summary"].startswith("Follow ended: stop was requested.")


async def test_pick_needs_a_recent_numbered_picture(harness: Harness) -> None:
    await harness.connect(world=cat_world())
    view = await harness.robot.follow_view("car-1")
    assert view.data["result"] == "not following" and view.images == []
    with pytest.raises(InvalidRequest, match="Call find first"):
        await harness.robot.follow("car-1", pick=1)
    found = await harness.robot.find("car-1", target="dog")
    assert found.data["found"] == []
    assert found.data["summary"] == "No dog found in the picture. Turn, tilt or move and try again."
    with pytest.raises(InvalidRequest, match="There's no number 1 in the last picture: it had none"):
        await harness.robot.follow("car-1", pick=1)
    await harness.robot.find("car-1", target="cat")
    with pytest.raises(InvalidRequest, match="There's no number 2 in the last picture: it had only number 1"):
        await harness.robot.follow("car-1", pick=2)
    harness.robot.config.follow_pick_valid_s = 0.01
    await asyncio.sleep(0.05)  # Windows' monotonic clock moves in 15 ms ticks
    with pytest.raises(InvalidRequest, match="Call find first"):
        await harness.robot.follow("car-1", pick=1)
    with pytest.raises(InvalidRequest, match="find can't recognise 'unicorn'"):
        await harness.robot.find("car-1", target="unicorn")
    assert harness.robot.car("car-1").activity is None  # type: ignore[union-attr]
    assert harness.robot._detector_users == 0


# Two people side by side, the same size; the one on the right stands centred at the
# distance follow keeps, so following it the car holds still.
TWO_PEOPLE = [
    Detection("person", 0.9, 0.02, 0.1, 0.28, 0.9),
    Detection("person", 0.8, 0.37, 0.1, 0.63, 0.9),
]


@pytest.mark.detector.with_args(lambda: ScriptedDetector(lambda _jpeg: list(TWO_PEOPLE)))
async def test_follow_keeps_to_the_one_picked_and_can_switch(harness: Harness) -> None:
    await harness.connect(world=cat_world())
    found = await harness.robot.find("car-1", target="person")
    assert [f["number"] for f in found.data["found"]] == [1, 2]  # numbered left to right
    started = await harness.robot.follow("car-1", pick=2, seconds=60)
    assert [item["following"] for item in started.data["in_view"]] == [False, True]
    view = await harness.robot.follow_view("car-1")
    assert [item["following"] for item in view.data["in_view"]] == [False, True]
    assert view.data["follow"]["in_view"] == 2
    # Wrong one: switch to number 1 in follow_view's picture.
    switched = await harness.robot.follow("car-1", pick=1, seconds=60)
    assert [item["following"] for item in switched.data["in_view"]] == [True, False]
    assert switched.data["target"] == "person"
    # Taking over: any move ends it and hands the car back.
    moved = await harness.robot.move("car-1", direction="back", ms=200)
    assert moved.data["result"] == "completed"
    assert follow_state(harness)["ended_because"] == "a new command arrived"


# The person stands centred; a fifth as big, a chair that looks just like them. After
# three frames the person has gone and only the chair is left.
PERSON = Detection("person", 0.9, 0.37, 0.1, 0.63, 0.9)
CHAIR = Detection("person", 0.5, 0.10, 0.45, 0.15, 0.6)


def _person_then_chair() -> ScriptedDetector:
    seen = {"n": 0}

    def script(_jpeg: bytes) -> list[Detection]:
        seen["n"] += 1
        return [CHAIR, PERSON] if seen["n"] <= 3 else [CHAIR]

    return ScriptedDetector(script)


@pytest.mark.detector.with_args(_person_then_chair)
async def test_follow_doesnt_take_a_far_smaller_look_alike(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Everything looks the same, as a dark chair did to a person in dark clothes on the floor.
    monkeypatch.setattr("scout.robot.robot.describe", lambda _image, _det: np.full(60, 1 / 20))
    await harness.connect(world=cat_world())
    started = await harness.robot.follow("car-1", target="person", seconds=30)
    assert [item["following"] for item in started.data["in_view"]] == [False, True]  # the biggest
    await wait_for(lambda: follow_state(harness).get("state") == "searching", within=4)
    view = await harness.robot.follow_view("car-1")
    assert [item["following"] for item in view.data["in_view"]] == [False]
    assert "5.3× smaller than the target was" in view.data["in_view"][0]["note"]
    assert "the one in view that looks most like it (match 1.00) is 5.3× smaller" in view.data["summary"]
    await harness.robot.stop("car-1")


def _sighting(match: float = 0.9, *, unsure: bool = False) -> Choice:
    return Choice(0, match, unsure, f"match {match:.2f}")


def _seen(score: float) -> Detection:
    return Detection("person", score, 0.4, 0.1, 0.6, 0.9)


def check(checking: _Checking | None, choice: Choice, score: float, **kw: bool) -> _Checking | None:
    kw = {"found_again": False, "confirmed": False, **kw}
    return _check(checking, choice, _seen(score), "person", 0.85, score_drop=0.7, confirm_frames=2, **kw)


def test_when_follow_holds_back_and_for_how_long() -> None:
    assert check(None, _sighting(), 0.8) is None  # what it has been following, as before
    # Lost and found again: held until two sure sightings in a row.
    found = check(None, _sighting(), 0.8, found_again=True)
    assert found is not None and found.why == "it lost sight of the person and has found one again"
    found = check(found, _sighting(0.5), 0.8)  # not alike enough to count
    assert found is not None and found.streak == 0
    found = check(found, _sighting(), 0.8)
    assert found is not None and found.streak == 1
    assert check(found, _sighting(), 0.8) is None
    # A coat rack the detector is far less sure of: held for as long as that lasts.
    rack = check(None, _sighting(), 0.47)
    assert rack is not None and rack.why == (
        "the detector is much less sure this is a person (0.47, against 0.85 before)"
    )
    for _ in range(5):
        rack = check(rack, _sighting(), 0.47)
    assert rack is not None
    # A look-alike close by: a reason to hold unless the AI picked this one with both in view.
    assert check(None, _sighting(unsure=True), 0.8) is not None
    assert check(None, _sighting(unsure=True), 0.8, confirmed=True) is None


# The person follow locks on to, then nobody, then a coat rack where the person was that
# the detector takes for a person, though far less sure of it.
def _person_gone_then_rack() -> ScriptedDetector:
    seen = {"n": 0}

    def script(_jpeg: bytes) -> list[Detection]:
        seen["n"] += 1
        if seen["n"] <= 3:
            return [Detection("person", 0.85, 0.42, 0.2, 0.58, 0.9)]
        if seen["n"] <= 6:
            return []
        return [Detection("person", 0.45, 0.42, 0.2, 0.58, 0.9)]

    return ScriptedDetector(script)


@pytest.mark.detector.with_args(_person_gone_then_rack)
async def test_follow_holds_back_until_it_is_sure_or_the_ai_confirms(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scout.robot.robot.describe", lambda _image, _det: np.full(60, 1 / 20))
    await harness.connect(world=cat_world())
    await harness.robot.follow("car-1", target="person", seconds=30)
    await wait_for(lambda: bool(follow_state(harness).get("checking")), within=6)
    assert follow_state(harness)["checking"] == "it lost sight of the person and has found one again"
    for _ in range(5):  # far away, but it doesn't drive towards it
        step = follow_state(harness)["last_step"]
        assert isinstance(step, dict) and step["vx"] == 0, step
        await asyncio.sleep(0.2)
    assert "not driving until it's sure this is the person" in str(step["why"])
    view = await harness.robot.follow_view("car-1")
    assert view.data["follow"]["checking"] == "it lost sight of the person and has found one again"
    assert "call follow with pick set to 1 to confirm" in view.data["summary"]
    # The AI looks and confirms it: follow drives again.
    confirmed = await harness.robot.follow("car-1", pick=1, seconds=30)
    assert "isn't sure" not in confirmed.data["summary"]

    def drove() -> bool:
        step = follow_state(harness).get("last_step")
        return isinstance(step, dict) and str(step["why"]).startswith("forward")

    await wait_for(drove, within=4)
    assert not follow_state(harness).get("checking")
    await harness.robot.stop("car-1")


async def test_follow_searches_for_a_target_out_of_sight(harness: Harness) -> None:
    world = cat_world()
    # The cat is about 75° to the car's right: out of the picture, which is 81° across.
    bearing = math.degrees(math.atan2(172 - 150, 240 - 110))
    world.place_car(110, 150, bearing + 75)
    await harness.connect(world=world)
    started = await harness.robot.follow("car-1", seconds=60)
    assert started.data["state"] in ("searching", "following")
    await wait_for(lambda: follow_state(harness).get("state") == "following", within=10)
    follow = (await harness.robot.status("car-1")).data["follow"]
    assert follow["searches"] == 1 and follow["found_again"] == 1
    view = await harness.robot.follow_view("car-1")
    assert len(view.images) == 2  # it locked on to the cat it found, so it has a close-up
    await harness.robot.stop("car-1")


async def test_the_model_stays_loaded_for_a_while_after_find(harness: Harness) -> None:
    harness.robot.config.detector_linger_s = 0.3
    await harness.connect(world=cat_world())
    await harness.robot.find("car-1")
    assert harness.robot._detector is not None
    await harness.robot.find("car-1")  # reuses it and restarts the wait
    await wait_for(lambda: harness.robot._detector is None, within=2)
