"""The car link: auth, hello/welcome, replacement, keepalive, robustness."""

from __future__ import annotations

import json

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from scout.contracts import CarError, CarOffline
from scout.robot import RobotConfig
from scout_sim import Faults

from .conftest import Harness, fast_config, wait_for

pytestmark = pytest.mark.anyio


async def test_hello_gets_welcome_with_settings(harness: Harness) -> None:
    harness.settings["car-1"].obst_cm = 22
    harness.settings["car-1"].name = "Rover"
    car = await harness.connect()
    assert car.welcome is not None
    assert car.welcome["car"] == "car-1"
    assert car.welcome["name"] == "Rover"
    assert car.welcome["conf"] == {
        "obst_cm": 22,
        "min_pwm": 90,
        "link_ms": 1000,
        "vflip": True,
        "hmirror": False,
    }
    assert car.uno.obst_cm == 22  # the sim applied it
    assert car.vflip is True
    summary = harness.robot.car("car-1")
    assert summary is not None and summary.online
    assert summary.firmware == "0.1.0"
    assert summary.uno_firmware == "0.1.0"
    assert summary.uno_ok
    assert summary.camera == "OV3660"
    assert summary.max_res == "qxga"
    assert summary.psram == 4 * 1024 * 1024
    assert summary.ssid == "sim-wifi"
    assert summary.connected_since is not None
    assert "cm" in summary.telemetry
    assert [s.car_id for s in harness.robot.cars()] == ["car-1"]


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic key-1"}])
async def test_refused_before_upgrade_with_403(harness: Harness, headers: dict[str, str]) -> None:
    with pytest.raises(InvalidStatus) as exc:
        async with connect(harness.url, additional_headers=headers, proxy=None):
            pass
    assert exc.value.response.status_code == 403
    assert harness.robot.cars() == []


async def test_sim_retries_after_refusal(harness: Harness) -> None:
    car = harness.sim("bad-key")
    car.start()
    await wait_for(lambda: len(car.refusals) >= 2)
    assert set(car.refusals) == {403}
    assert car.connections == 0


async def test_new_connection_replaces_old_with_4409(harness: Harness) -> None:
    first = await harness.connect()
    second = harness.sim()
    second.start()
    await wait_for(lambda: 4409 in first.close_codes)
    await wait_for(lambda: second.welcomed.is_set())
    await first.close()
    summary = harness.robot.car("car-1")
    assert summary is not None and summary.online
    result = await harness.robot.status("car-1")
    assert result.data["online"] is True


async def test_server_pings_and_car_answers(robot_config: RobotConfig, harness: Harness) -> None:
    robot_config.ping_interval_s = 0.1
    car = await harness.connect()
    await wait_for(lambda: sum(1 for m in car.received if m.get("t") == "ping") >= 3)
    assert all(isinstance(m.get("id"), int) for m in car.received if m.get("t") == "ping")
    assert any(m.get("t") == "pong" for m in car.sent)


async def test_silent_car_is_dropped(robot_config: RobotConfig, harness: Harness) -> None:
    robot_config.silence_timeout_s = 0.6
    robot_config.ping_interval_s = 0.2
    car = await harness.connect()
    car.faults.silent = True
    await wait_for(lambda: not harness.robot.car("car-1").online, within=3)  # type: ignore[union-attr]
    await wait_for(lambda: 4408 in car.close_codes)
    with pytest.raises(CarOffline, match=r"Scout is offline\. It was last seen just now\."):
        await harness.robot.look("car-1")
    car.faults.silent = False
    await wait_for(lambda: harness.robot.car("car-1").online, within=3)  # type: ignore[union-attr]


async def test_car_without_hello_is_closed(robot_config: RobotConfig, harness: Harness) -> None:
    robot_config.hello_timeout_s = 0.3
    async with connect(harness.url, additional_headers={"Authorization": "Bearer key-1"}, proxy=None) as ws:
        await ws.wait_closed()
        assert ws.close_code == 4400
    assert harness.robot.car("car-1") is None


async def test_disconnect_closes_socket_and_car_reconnects(harness: Harness) -> None:
    car = await harness.connect()
    await harness.robot.disconnect("car-1")
    await wait_for(lambda: 1000 in car.close_codes)
    await wait_for(lambda: car.connections == 2 and car.welcomed.is_set())
    await wait_for(lambda: harness.robot.car("car-1").online)  # type: ignore[union-attr]


async def test_offline_errors_and_status(harness: Harness) -> None:
    status = await harness.robot.status("car-9")
    assert status.data["online"] is False
    assert status.data["summary"] == "Scout is offline. It hasn't connected since the server started."
    with pytest.raises(CarOffline, match="hasn't connected since the server started"):
        await harness.robot.move("car-9", direction="forward", ms=500)
    car = await harness.connect()
    car.drop_connection()
    await wait_for(lambda: not harness.robot.car("car-1").online)  # type: ignore[union-attr]
    await car.close()
    status = await harness.robot.status("car-1")
    assert status.data["online"] is False
    assert status.data["summary"].startswith("Scout is offline. It was last seen")
    for call in (
        harness.robot.look("car-1"),
        harness.robot.stop("car-1"),
        harness.robot.set_light("car-1", level=10),
        harness.robot.push_settings("car-1"),
    ):
        with pytest.raises(CarOffline):
            await call


async def test_malformed_frames_are_survived(harness: Harness) -> None:
    car = await harness.connect()
    junk: list[str | bytes] = [
        "not json",
        "[1, 2, 3]",
        '{"no_type": 1}',
        '{"t": "bogus"}',
        '{"t": "ack", "id": 99999}',
        '{"t": "done", "id": "x"}',
        '{"t": "ack", "id": true}',
        json.dumps({"t": "event", "kind": "x" * 500, "msg": 5}),
        json.dumps({"t": "tel", "cm": "far", "line": [1, 2], "tilt": True}),
        "x" * (70 * 1024),
        b"SC",
        b"XXXX\x01\x00\x00\x00\xff\xd8",
        b"SCIM\x7f\x00\x00\x00\xff\xd8\xff\xd9",
        b"SCST\x01\x00\x00\x00\xff\xd8",
    ]
    for item in junk:
        await car.send_raw(item)
    # The last frame sent is the stray SCST, so once it is counted everything was read.
    await wait_for(lambda: harness.robot.link_stats("car-1").get("unexpected_stream", 0) == 1)
    stats = harness.robot.link_stats("car-1")
    assert stats["bad_json"] == 1
    assert stats["bad_message"] >= 4
    assert stats["unknown_type"] == 1
    assert stats["unknown_id"] == 2
    assert stats["bad_frame"] == 2
    assert stats["unexpected_stream"] == 1
    result = await harness.robot.look("car-1")
    assert result.data["resolution"] == "640x480"
    assert harness.robot.car("car-1").online  # type: ignore[union-attr]


async def test_oversized_snapshot_is_refused(robot_config: RobotConfig, harness: Harness) -> None:
    robot_config.max_image_bytes = 5000
    await harness.connect()
    with pytest.raises(CarError, match="larger than the server accepts"):
        await harness.robot.look("car-1", size="large")
    assert harness.robot.link_stats("car-1")["oversized_frame"] == 1


async def test_reply_timeouts(robot_config: RobotConfig, harness: Harness) -> None:
    robot_config.snap_timeout_s = 0.5
    robot_config.ack_timeout_s = 0.5
    faults = Faults(drop={"snap"})
    car = await harness.connect(faults=faults)
    with pytest.raises(CarError, match="didn't send the picture in time"):
        await harness.robot.look("car-1")
    faults.drop = {"move"}
    with pytest.raises(CarError, match=r"didn't report back on the move in time\. It was told to stop"):
        await harness.robot.move("car-1", direction="back", ms=300)
    await wait_for(lambda: any(m.get("t") == "stop" for m in car.received))
    faults.drop = set()
    faults.delay_s = {"light": 0.3}
    result = await harness.robot.set_light("car-1", level=40)  # slow but within the timeout
    assert result.data["light"] == 40


def test_default_config_matches_car_link_timing() -> None:
    cfg = RobotConfig()
    assert cfg.ping_interval_s == 5.0
    assert cfg.silence_timeout_s == 15.0
    assert fast_config().ping_interval_s == 5.0
