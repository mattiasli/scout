"""The published tool schemas against what the real robot side accepts.

An AI that follows the schema should never get an out-of-range error from the robot, and
the robot shouldn't accept anything the schema hides. These tests call the real
``Robot`` with no car connected: its argument checks run first and raise
``InvalidRequest``; a call that passes them ends in ``CarOffline``.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from scout.contracts import CarOffline, CarSettings
from scout.tools import (
    FOLLOW_TARGETS,
    NOTE_MAX_LENGTH,
    TILT,
    TOOLS,
    TOOLS_BY_NAME,
    InvalidArguments,
    ToolSpec,
    validate_args,
)

robot_module = pytest.importorskip("scout.robot")
from scout.robot import InvalidRequest, Robot  # noqa: E402

pytestmark = pytest.mark.anyio

FIRMWARE_CONFIG = Path(__file__).resolve().parents[3] / "firmware" / "scout_uno" / "src" / "core" / "config.h"

# Arguments where the schema is deliberately narrower than the robot's own check. The
# robot passes tilt -90..90 through to the car, but the UNO firmware clamps it to ±40.
NARROWER_THAN_ROBOT = {("look", "tilt")}

ROBOT_TOOLS = [spec for spec in TOOLS if spec.name != "release_car"]  # release_car is app-only


@pytest.fixture
async def robot(tmp_path: Path) -> AsyncIterator[Robot]:
    async def authenticate(_key: str) -> str | None:
        return None

    async def settings(_car_id: str) -> CarSettings:
        return CarSettings()

    real = Robot(data_dir=tmp_path, authenticate_car=authenticate, car_settings=settings, allow_insecure=True)
    await real.start()
    try:
        yield real
    finally:
        await real.close()


def robot_params(spec: ToolSpec) -> dict[str, inspect.Parameter]:
    """The tool's keyword arguments on the robot, without car_id and the app-supplied user_id."""
    params = inspect.signature(getattr(Robot, spec.name)).parameters
    return {name: p for name, p in params.items() if name not in {"self", "car_id", "user_id"}}


def minimal_args(spec: ToolSpec) -> dict[str, Any]:
    """Valid values for the required arguments: the lowest integer, the first enum value."""
    args: dict[str, Any] = {}
    for name in spec.required:
        prop = spec.properties[name]
        args[name] = prop["enum"][0] if "enum" in prop else prop["minimum"]
    return args


async def robot_accepts(robot: Robot, spec: ToolSpec, name: str, value: Any) -> bool:
    """Run the real robot's argument checks on a call with ``name=value``.

    The other arguments are what the app would send for a minimal call (required ones at
    their lowest, defaults filled in). ``value`` goes to the robot as given, so values the
    app itself would refuse can be tried too.
    """
    kwargs = validate_args(spec, minimal_args(spec))
    kwargs[name] = value
    if "user_id" in inspect.signature(getattr(Robot, spec.name)).parameters:
        kwargs["user_id"] = 1
    try:
        await getattr(robot, spec.name)("scout-1", **kwargs)
    except InvalidRequest:
        return False
    except CarOffline:
        return True
    return True


def integer_args() -> list[tuple[str, str]]:
    return [
        (spec.name, name)
        for spec in ROBOT_TOOLS
        for name, prop in spec.properties.items()
        if prop["type"] == "integer"
    ]


def enum_args() -> list[tuple[str, str]]:
    # follow's target is checked against the detector's labels only once a car is
    # connected, so test_follow_targets_are_the_detectors_labels covers it instead.
    return [
        (spec.name, name)
        for spec in ROBOT_TOOLS
        for name, prop in spec.properties.items()
        if "enum" in prop and (spec.name, name) != ("follow", "target")
    ]


# ------------------------------------------------------------------- the arguments


@pytest.mark.parametrize("spec", ROBOT_TOOLS, ids=lambda spec: spec.name)
def test_schema_has_the_robots_arguments_and_defaults(spec: ToolSpec) -> None:
    params = robot_params(spec)
    assert set(spec.properties) == set(params)
    required = {name for name, p in params.items() if p.default is inspect.Parameter.empty}
    assert set(spec.required) == required
    for name, param in params.items():
        prop = spec.properties[name]
        if param.default in (inspect.Parameter.empty, None):
            assert "default" not in prop, f"{spec.name}.{name} has a default the robot doesn't"
        else:
            assert prop.get("default") == param.default, f"{spec.name}.{name} default differs from the robot"


# ---------------------------------------------------------------------- the ranges


@pytest.mark.parametrize(("tool", "name"), integer_args())
async def test_integer_bounds_are_the_robots(robot: Robot, tool: str, name: str) -> None:
    spec = TOOLS_BY_NAME[tool]
    low, high = spec.properties[name]["minimum"], spec.properties[name]["maximum"]

    # Both ends of the published range pass the app's checks and the real robot's.
    for value in (low, high):
        validate_args(spec, {**minimal_args(spec), name: value})
        assert await robot_accepts(robot, spec, name, value), f"robot refuses {tool}.{name}={value}"

    # Just outside, the app refuses before anything reaches the robot...
    for value in (low - 1, high + 1):
        with pytest.raises(InvalidArguments):
            validate_args(spec, {**minimal_args(spec), name: value})

    # ...and the robot refuses too, so the schema doesn't hide anything it accepts.
    if (tool, name) not in NARROWER_THAN_ROBOT:
        for value in (low - 1, high + 1):
            assert not await robot_accepts(robot, spec, name, value), (
                f"robot accepts {tool}.{name}={value}, outside the schema"
            )


@pytest.mark.parametrize(("tool", "name"), enum_args())
async def test_enum_values_are_the_robots(robot: Robot, tool: str, name: str) -> None:
    spec = TOOLS_BY_NAME[tool]
    for value in spec.properties[name]["enum"]:
        assert await robot_accepts(robot, spec, name, value), f"robot refuses {tool}.{name}={value!r}"
    assert not await robot_accepts(robot, spec, name, "sideways-ish")


def test_follow_targets_are_the_detectors_labels() -> None:
    detector = pytest.importorskip("scout.robot.detector")
    assert sorted(FOLLOW_TARGETS) == sorted(detector.COCO_LABELS)
    assert len(set(FOLLOW_TARGETS)) == len(FOLLOW_TARGETS)
    # The robot looks targets up after normalising them; ours are already normal.
    assert all(detector.normalise_label(target) == target for target in FOLLOW_TARGETS)
    assert TOOLS_BY_NAME["follow"].properties["target"]["default"] in FOLLOW_TARGETS


def test_tilt_is_the_firmwares_range() -> None:
    if not FIRMWARE_CONFIG.is_file():
        pytest.skip("the firmware sources aren't next to the server")
    text = FIRMWARE_CONFIG.read_text(encoding="utf-8")
    low = re.search(r"kTiltMinDeg\s*=\s*(-?\d+)", text)
    high = re.search(r"kTiltMaxDeg\s*=\s*(-?\d+)", text)
    assert low and high
    assert (int(low.group(1)), int(high.group(1))) == TILT
    tilt = TOOLS_BY_NAME["look"].properties["tilt"]
    assert (tilt["minimum"], tilt["maximum"]) == TILT


async def test_capture_ids_and_notes(robot: Robot) -> None:
    spec = TOOLS_BY_NAME["capture_photo"]
    for good in ("a" * 12, "-_" * 32, "Ab3-_xYz9_Q-"):
        validate_args(spec, {"capture_id": good})
        assert await robot_accepts(robot, spec, "capture_id", good)
    for bad in ("a" * 11, "a" * 65, "has space 123", "dots.not.okay"):
        with pytest.raises(InvalidArguments):
            validate_args(spec, {"capture_id": bad})
        assert not await robot_accepts(robot, spec, "capture_id", bad)
    validate_args(spec, {"note": "n" * NOTE_MAX_LENGTH})
    assert await robot_accepts(robot, spec, "note", "n" * NOTE_MAX_LENGTH)
    with pytest.raises(InvalidArguments):
        validate_args(spec, {"note": "n" * (NOTE_MAX_LENGTH + 1)})


# ---------------------------------------------------------------- the descriptions


@pytest.mark.parametrize(("tool", "name"), integer_args())
def test_descriptions_state_the_ranges(tool: str, name: str) -> None:
    spec = TOOLS_BY_NAME[tool]
    prop = spec.properties[name]
    span = f"{prop['minimum']} to {prop['maximum']}"
    text = f"{spec.description} {prop['description']}"
    if (tool, name) in {("drive", "vx"), ("drive", "vy"), ("drive", "wz")}:
        assert f"each {span} percent" in spec.description
    else:
        assert span in text, f"{tool} doesn't tell the AI that {name} is {span}"
    if "default" in prop:
        assert f"default {prop['default']}" in text, f"{tool} doesn't mention the default {name}"
