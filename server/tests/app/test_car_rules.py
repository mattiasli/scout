"""Per-call rules: account status, availability, the driver lock, error mapping and logging."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import anyio
import mcp_types as types
import pytest

from scout.captures import CaptureLinks
from scout.contracts import CarBusy, CarError, CarOffline
from scout.db import Store, User
from scout.driver import IDLE_S, DriverLock
from scout.images import ImageStore
from scout.service import NO_CAR, SUSPENDED, WAITING, Caller, ToolService

from .fakes import FAKE_SECRET_KEY, OWNER_GITHUB_ID, FakeRobot, add_user, write_capture

pytestmark = pytest.mark.anyio


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class World:
    store: Store
    robot: FakeRobot
    lock: DriverLock
    clock: Clock
    service: ToolService
    alice: User
    bob: User
    admin: User

    async def call(self, user: User, name: str, args: dict | None = None) -> types.CallToolResult:
        return await self.service.call(Caller(user_id=user.id, token_id=None), name, args or {})


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    store = Store(tmp_path / "scout.db", owner_github_id=OWNER_GITHUB_ID)
    robot = FakeRobot(tmp_path)
    clock = Clock()
    lock = DriverLock(clock=clock)
    service = ToolService(
        store=store,
        robot=robot,
        lock=lock,
        images=ImageStore(),
        capture_links=CaptureLinks("http://testserver", FAKE_SECRET_KEY),
        public_url="http://testserver",
    )
    store.add_car("scout-1", "Scout")
    store.set_availability("scout-1", "everyone")
    yield World(
        store=store,
        robot=robot,
        lock=lock,
        clock=clock,
        service=service,
        alice=add_user(store, 11, "alice"),
        bob=add_user(store, 12, "bob"),
        admin=add_user(store, 13, "admin", admin=True),
    )
    store.close()


def text(result: types.CallToolResult) -> str:
    first = result.content[0]
    assert isinstance(first, types.TextContent)
    return first.text


# ------------------------------------------------------------------ account status


async def test_waiting_account_is_told_to_wait(world: World) -> None:
    waiting = add_user(world.store, 20, "newbie", status="waiting")
    result = await world.call(waiting, "status")
    assert result.is_error and text(result) == WAITING
    assert world.robot.calls == []


async def test_suspended_account_is_refused(world: World) -> None:
    suspended = add_user(world.store, 21, "gone", status="suspended")
    result = await world.call(suspended, "move", {"direction": "forward", "ms": 500})
    assert result.is_error and text(result) == SUSPENDED
    assert world.robot.calls == []


async def test_status_is_checked_on_every_call(world: World) -> None:
    owner = world.store.ensure_owner()
    assert owner is not None
    assert not (await world.call(world.alice, "status")).is_error
    world.store.set_status(world.alice.id, "suspended", by=owner)
    assert text(await world.call(world.alice, "status")) == SUSPENDED


async def test_no_car_yet(tmp_path: Path) -> None:
    store = Store(tmp_path / "empty.db", owner_github_id=OWNER_GITHUB_ID)
    service = ToolService(
        store=store,
        robot=FakeRobot(tmp_path),
        lock=DriverLock(),
        images=ImageStore(),
        capture_links=CaptureLinks("http://testserver", FAKE_SECRET_KEY),
        public_url="http://testserver",
    )
    owner = store.ensure_owner()
    assert owner is not None
    result = await service.call(Caller(owner.id, None), "status", {})
    assert result.is_error and text(result) == NO_CAR
    store.close()


# ------------------------------------------------------------------- availability


async def test_admins_only_car_refuses_non_admins(world: World) -> None:
    world.store.set_availability("scout-1", "admins")
    refused = await world.call(world.alice, "look")
    assert refused.is_error and "only open to admins" in text(refused)
    assert not (await world.call(world.admin, "look")).is_error
    world.store.set_availability("scout-1", "everyone")
    assert not (await world.call(world.bob, "status")).is_error


# -------------------------------------------------------------------- driver lock


async def test_first_driver_holds_the_car(world: World) -> None:
    assert not (await world.call(world.alice, "move", {"direction": "forward", "ms": 500})).is_error
    refused = await world.call(world.bob, "turn", {"direction": "left", "degrees": 90})
    assert refused.is_error
    assert text(refused) == "Scout is being driven by alice right now. Try again in a couple of minutes."
    assert world.robot.names() == ["move"]


async def test_status_needs_no_lock_and_shows_the_driver(world: World) -> None:
    await world.call(world.alice, "look")
    result = await world.call(world.bob, "status")
    assert not result.is_error
    assert json.loads(text(result))["driver"] == {"login": "alice", "you": False}
    mine = json.loads(text(await world.call(world.alice, "status")))
    assert mine["driver"] == {"login": "alice", "you": True}


async def test_idle_driver_loses_the_car_after_two_minutes(world: World) -> None:
    await world.call(world.alice, "look")
    world.clock.now += IDLE_S - 1
    assert (await world.call(world.bob, "look")).is_error
    world.clock.now += 1
    assert not (await world.call(world.bob, "look")).is_error
    refused = await world.call(world.alice, "look")
    assert "driven by bob" in text(refused)


async def test_each_command_resets_the_idle_timer(world: World) -> None:
    await world.call(world.alice, "look")
    world.clock.now += IDLE_S - 10
    await world.call(world.alice, "set_light", {"level": 0})
    world.clock.now += IDLE_S - 10
    assert (await world.call(world.bob, "look")).is_error


async def test_a_running_command_never_counts_as_idle(world: World) -> None:
    world.robot.gate = anyio.Event()
    results: dict[str, types.CallToolResult] = {}

    async def long_follow() -> None:
        results["follow"] = await world.call(world.alice, "follow", {"seconds": 300})

    async with anyio.create_task_group() as tg:
        tg.start_soon(long_follow)
        await anyio.wait_all_tasks_blocked()
        world.clock.now += IDLE_S * 3
        # The lock is checked before the robot is called, so bob's refusal doesn't wait.
        refused = await world.call(world.bob, "look")
        assert refused.is_error and "driven by alice" in text(refused)
        world.robot.gate.set()
    assert not results["follow"].is_error
    world.clock.now += IDLE_S
    assert not (await world.call(world.bob, "look")).is_error


async def test_release_car_frees_it(world: World) -> None:
    await world.call(world.alice, "look")
    released = await world.call(world.alice, "release_car")
    assert not released.is_error and json.loads(text(released))["released"] is True
    assert not (await world.call(world.bob, "look")).is_error
    not_mine = await world.call(world.alice, "release_car")
    assert not not_mine.is_error and json.loads(text(not_mine))["released"] is False
    assert world.lock.current("scout-1") is not None


async def test_stop_rules(world: World) -> None:
    await world.call(world.alice, "move", {"direction": "forward", "ms": 500})
    refused = await world.call(world.bob, "stop")
    assert refused.is_error and "driven by alice" in text(refused)
    assert not (await world.call(world.alice, "stop")).is_error
    assert not (await world.call(world.admin, "stop")).is_error
    # An admin's stop doesn't take the car away from the driver.
    driver = world.lock.current("scout-1")
    assert driver is not None and driver.login == "alice"
    # Anyone may stop a car nobody is driving, without becoming its driver.
    world.lock.release("scout-1")
    assert not (await world.call(world.bob, "stop")).is_error
    assert world.lock.current("scout-1") is None


async def test_admins_follow_the_lock_for_driving(world: World) -> None:
    await world.call(world.alice, "look")
    assert "driven by alice" in text(await world.call(world.admin, "look"))


async def test_suspending_releases_the_lock(world: World) -> None:
    await world.call(world.alice, "look")
    world.lock.release_user(world.alice.id)
    assert world.lock.current("scout-1") is None


# ------------------------------------------------------------------ error mapping


@pytest.mark.parametrize(
    "error",
    [
        CarOffline("Scout is offline."),
        CarBusy("Scout is updating its firmware."),
        CarError("The camera failed."),
    ],
)
async def test_robot_errors_become_tool_errors(world: World, error: Exception) -> None:
    world.robot.fail = error
    result = await world.call(world.alice, "look")
    assert result.is_error and text(result) == str(error)
    entry = world.store.recent_activity(limit=1)[0]
    assert entry.action == "tool.look" and entry.outcome == f"error: {error}"


async def test_unexpected_errors_are_hidden(world: World) -> None:
    world.robot.fail = RuntimeError("internal detail /secret/path")
    result = await world.call(world.alice, "look")
    assert result.is_error and "secret" not in text(result)


async def test_unknown_tool(world: World) -> None:
    result = await world.call(world.alice, "self_destruct")
    assert result.is_error and "no tool called" in text(result)


# ---------------------------------------------------------------- tool mapping


async def test_arguments_and_defaults_reach_the_robot(world: World) -> None:
    await world.call(world.alice, "turn", {"direction": "right", "degrees": 90, "look": True})
    await world.call(world.alice, "drive", {"vx": 50, "vy": -20, "wz": 10, "ms": 700})
    await world.call(world.alice, "look_around", {})
    await world.call(world.alice, "capture_orbit", {"photos": 8})
    await world.call(world.alice, "follow", {"target": "dog"})
    await world.call(world.alice, "find", {})
    calls = dict(world.robot.calls)
    assert calls["turn"] == {
        "car_id": "scout-1",
        "direction": "right",
        "degrees": 90,
        "speed": 50,
        "look": True,
    }
    assert calls["drive"] == {"car_id": "scout-1", "vx": 50, "vy": -20, "wz": 10, "ms": 700, "look": False}
    assert calls["look_around"] == {"car_id": "scout-1", "views": 6}
    assert calls["capture_orbit"] == {
        "car_id": "scout-1",
        "user_id": world.alice.id,
        "photos": 8,
        "direction": "left",
    }
    assert calls["follow"] == {"car_id": "scout-1", "target": "dog", "seconds": 60, "pick": None}
    assert calls["find"] == {"car_id": "scout-1", "target": None, "size": "medium"}
    await world.call(world.alice, "follow", {"pick": 3})
    await world.call(world.alice, "follow_view", {})
    calls = dict(world.robot.calls)
    assert calls["follow"] == {"car_id": "scout-1", "target": "cat", "seconds": 60, "pick": 3}
    assert calls["follow_view"] == {"car_id": "scout-1"}


async def test_whole_number_floats_are_accepted(world: World) -> None:
    result = await world.call(world.alice, "move", {"direction": "back", "ms": 400.0})
    assert not result.is_error
    assert world.robot.calls[-1][1]["ms"] == 400


async def test_capture_results_carry_a_download_link(world: World) -> None:
    result = await world.call(world.alice, "capture_orbit", {})
    data = json.loads(text(result))
    assert data["capture_download"].startswith("http://testserver/captures/orbit0001.zip?exp=")
    assert len(data["image_links"]) == 2


async def test_cannot_add_photos_to_someone_elses_capture(world: World, tmp_path: Path) -> None:
    write_capture(tmp_path, "alices-capture", world.alice.id)
    refused = await world.call(world.bob, "capture_photo", {"capture_id": "alices-capture"})
    assert refused.is_error and "belongs to someone else" in text(refused)
    world.lock.release("scout-1")
    ok = await world.call(world.alice, "capture_photo", {"capture_id": "alices-capture"})
    assert not ok.is_error


# ---------------------------------------------------------------------- logging


async def test_every_call_is_logged(world: World) -> None:
    await world.call(world.alice, "move", {"direction": "left", "ms": 300})
    await world.call(world.bob, "look")
    waiting = add_user(world.store, 30, "later", status="waiting")
    await world.call(waiting, "status")
    entries = world.store.recent_activity(limit=3)
    assert [(e.login, e.action) for e in entries] == [
        ("later", "tool.status"),
        ("bob", "tool.look"),
        ("alice", "tool.move"),
    ]
    assert entries[0].outcome == f"refused: {WAITING}"
    assert entries[1].outcome.startswith("refused: Scout is being driven by alice")
    assert entries[2].outcome == "ok" and json.loads(entries[2].detail) == {"direction": "left", "ms": 300}
    assert entries[2].car_id == "scout-1"
