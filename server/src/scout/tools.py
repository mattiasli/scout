"""The MCP tools: names, descriptions written for an AI driver, JSON schemas and validation.

The schemas are the single source of truth for argument checking on the app side:
``validate_args`` applies the same types, ranges, enums and defaults that the clients
see. The ranges come from the limits table below, which mirrors what the robot side
accepts, so an AI that follows the published schema never gets an out-of-range error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import mcp_types as types

# -- limits -------------------------------------------------------------------------
#
# What the robot side accepts, copied from the checks at the top of each tool method in
# scout/robot/robot.py (``_int_arg`` / ``_choice``) and the defaults in their signatures.
# The robot doesn't export these as constants, so this is the one copy on the app side;
# tests/app/test_tool_limits.py checks every range, enum and default here against the
# real Robot (and the tilt range against the UNO firmware) so they can't drift apart.

Range = tuple[int, int]

# Camera tilt, degrees from level, positive is up. The robot passes -90..90 through, but
# the UNO firmware clamps the servo to kTiltMinDeg..kTiltMaxDeg
# (firmware/scout_uno/src/core/config.h), so only that range does what it says.
TILT: Range = (-40, 40)
LIGHT: Range = (0, 100)  # look(light=...), set_light(level=...)
MOVE_MS: Range = (100, 3000)  # move, drive (motion.MAX_MOVE_MS is the upper end)
SPEED: Range = (20, 100)  # move, turn
MOVE_SPEED_DEFAULT = 60
TURN_SPEED_DEFAULT = 50
TURN_DEGREES: Range = (1, 360)
VELOCITY: Range = (-100, 100)  # drive vx, vy, wz
LOOK_AROUND_VIEWS: Range = (4, 8)
LOOK_AROUND_VIEWS_DEFAULT = 6
ORBIT_PHOTOS: Range = (4, 36)
ORBIT_PHOTOS_DEFAULT = 12
FOLLOW_SECONDS: Range = (5, 300)
FOLLOW_SECONDS_DEFAULT = 60
FOLLOW_TARGET_DEFAULT = "cat"
PICK: Range = (1, 20)  # robot.MAX_PICK: find numbers at most 20 boxes
FOLLOW_VIEW_WAIT: Range = (0, 30)
FOLLOW_DISTANCE: Range = (20, 500)
# scout/robot/captures.py: capture ids are 12-64 URL-safe characters.
CAPTURE_ID_PATTERN = "^[A-Za-z0-9_-]{12,64}$"
# robot.capture_photo keeps at most this much of a note (after collapsing whitespace).
NOTE_MAX_LENGTH = 500
# Picture sizes for look (robot.SIZE_RES): small qvga 320x240, medium vga 640x480,
# large svga 800x600.
LOOK_SIZES = ["small", "medium", "large"]
LOOK_SIZE_DEFAULT = "medium"
# Directions for move (scout/robot/motion.py, DIRECTIONS).
DIRECTIONS = ["forward", "back", "left", "right", "forward_left", "forward_right", "back_left", "back_right"]
SIDES = ["left", "right"]  # turn and capture_orbit directions
# What follow can recognise: the detector's labels (scout/robot/detector.py, COCO_LABELS,
# the 80 COCO classes YOLOX-Nano is trained on).
FOLLOW_TARGETS = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat",
    "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack",
    "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball",
    "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
    "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
]  # fmt: skip


def _span(limits: Range) -> str:
    return f"{limits[0]} to {limits[1]}"


INSTRUCTIONS = f"""\
Scout is a small robot car with mecanum wheels in someone's home. You see through its \
camera and drive it with short moves that stop by themselves.

- Mecanum wheels let Scout move sideways and diagonally without turning.
- The camera faces forward only and sits about 10-15 cm above the floor, so you see the \
world from a cat's height.
- The distance sensor faces forward only. Forward moves stop for obstacles; sideways and \
backward moves are blind, so keep them short.
- Every move is short and bounded ({MOVE_MS[0]}-{MOVE_MS[1]} ms). Turns are approximate \
because there is no gyroscope, so check with look.
- Pass look=true to move, turn and drive to get a picture afterwards in the same call.
- To follow a particular person or thing, call find to get the objects in view numbered, \
then follow with pick set to the right number. Check with follow_view now and then that \
it is still on the right one: call follow_view with wait_s 10 again and again until follow \
ends; it returns early when follow needs you. Pick again or call stop to take over if it's \
following the wrong one. When follow isn't sure it has the right one (status follow.checking \
says why), it only turns to keep it in view and doesn't drive until you confirm with follow \
pick set to its number.
- One person drives at a time. Call release_car when you're done; the car is also \
released after 2 minutes without commands.
- Call stop at once if anything looks wrong.
"""

LOOK_PARAM = {
    "type": "boolean",
    "default": False,
    "description": "Take a picture when the motion ends and return it with the result. Saves a round trip.",
}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    description: str
    properties: dict[str, dict[str, Any]] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    needs_lock: bool = True
    read_only: bool = False

    @property
    def input_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": self.properties,
            "additionalProperties": False,
        }
        if self.required:
            schema["required"] = list(self.required)
        return schema

    def to_tool(self) -> types.Tool:
        return types.Tool(
            name=self.name,
            title=self.title,
            description=self.description,
            input_schema=self.input_schema,
            annotations=types.ToolAnnotations(
                title=self.title,
                read_only_hint=self.read_only,
                destructive_hint=False,
                idempotent_hint=self.read_only,
                open_world_hint=True,
            ),
        )


def _int(limits: Range, description: str, default: int | None = None) -> dict[str, Any]:
    prop: dict[str, Any] = {
        "type": "integer",
        "minimum": limits[0],
        "maximum": limits[1],
        "description": description,
    }
    if default is not None:
        prop["default"] = default
    return prop


def _enum(values: list[str], description: str, default: str | None = None) -> dict[str, Any]:
    prop: dict[str, Any] = {"type": "string", "enum": values, "description": description}
    if default is not None:
        prop["default"] = default
    return prop


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="status",
        title="Car status",
        description=(
            "Check Scout without moving it: whether the car is online, what it is doing, the latest "
            "readings (front distance in cm, camera tilt, light, WiFi signal) and who is driving. "
            "Doesn't need the driver lock, so it's a good first call."
        ),
        needs_lock=False,
        read_only=True,
    ),
    ToolSpec(
        name="look",
        title="Look",
        description=(
            "Take a picture with the forward-facing camera. It sits about 10-15 cm above the floor, "
            "so you see the room from a cat's height. Optionally tilt the camera first "
            f"({_span(TILT)} degrees from level, positive is up) and set the light "
            f"({_span(LIGHT)}); both stay as set afterwards. Sizes: small 320x240, medium 640x480 "
            "(default), large 800x600. Returns the picture and the distance to whatever is "
            "straight ahead."
        ),
        properties={
            "tilt": _int(TILT, "Camera tilt in degrees from level, positive is up. Leave out to keep it."),
            "light": _int(LIGHT, f"Light level {_span(LIGHT)}, 0 is off. Leave out to keep it."),
            "size": _enum(LOOK_SIZES, "Picture size.", LOOK_SIZE_DEFAULT),
        },
        read_only=True,
    ),
    ToolSpec(
        name="move",
        title="Move",
        description=(
            "Drive in a straight line for a short, fixed time; the car stops by itself. Mecanum wheels "
            "let Scout go sideways (left, right) and diagonally without turning. Only the front has a "
            "distance sensor: forward moves stop for obstacles, but sideways and backward moves are "
            f"blind, so keep those short (300-800 ms) and look first. ms is {_span(MOVE_MS)}; start "
            "with about 500-1000 ms and check where you ended up. speed is "
            f"{_span(SPEED)} percent (default {MOVE_SPEED_DEFAULT}). Returns why the move ended "
            "(time, obstacle, stopped, ...) and the distance ahead before and after."
        ),
        properties={
            "direction": _enum(DIRECTIONS, "Which way to go, relative to where the camera points."),
            "ms": _int(MOVE_MS, "How long to drive, in milliseconds."),
            "speed": _int(SPEED, "Speed in percent of full speed.", MOVE_SPEED_DEFAULT),
            "look": LOOK_PARAM,
        },
        required=("direction", "ms"),
    ),
    ToolSpec(
        name="turn",
        title="Turn",
        description=(
            "Turn on the spot, left (counter-clockwise) or right (clockwise), by roughly the given "
            f"number of degrees ({_span(TURN_DEGREES)}). There is no gyroscope, so the angle is "
            "estimated from timing and can be off, especially on carpet: check with look (or pass "
            "look=true) and correct with a small turn if needed. speed is "
            f"{_span(SPEED)} percent (default {TURN_SPEED_DEFAULT})."
        ),
        properties={
            "direction": _enum(SIDES, "Which way to turn."),
            "degrees": _int(TURN_DEGREES, "Roughly how far to turn, in degrees."),
            "speed": _int(SPEED, "Turning speed in percent of full speed.", TURN_SPEED_DEFAULT),
            "look": LOOK_PARAM,
        },
        required=("direction", "degrees"),
    ),
    ToolSpec(
        name="drive",
        title="Drive (mixed)",
        description=(
            "Low-level mecanum control for a short, fixed time: mix forward/back (vx), sideways (vy) "
            f"and rotation (wz), each {_span(VELOCITY)} percent. vx > 0 is forward, vy > 0 is to the "
            "right, wz > 0 turns clockwise. Use it for curves or diagonals at a custom angle; for "
            "plain moves and turns, move and turn are easier. Only forward motion is watched by the "
            f"distance sensor, so keep anything else short. ms is {_span(MOVE_MS)}."
        ),
        properties={
            "vx": _int(VELOCITY, "Forward (+) or back (-), percent."),
            "vy": _int(VELOCITY, "Right (+) or left (-), percent."),
            "wz": _int(VELOCITY, "Clockwise (+) or counter-clockwise (-) rotation, percent."),
            "ms": _int(MOVE_MS, "How long to drive, in milliseconds."),
            "look": LOOK_PARAM,
        },
        required=("vx", "vy", "wz", "ms"),
    ),
    ToolSpec(
        name="set_light",
        title="Set light",
        description=(
            f"Set the light next to the camera, {_span(LIGHT)}: 0 is off, {LIGHT[1]} is full. Helps "
            "in dark rooms and under furniture. Please switch it off when you're done."
        ),
        properties={"level": _int(LIGHT, f"Light level, {_span(LIGHT)}.")},
        required=("level",),
    ),
    ToolSpec(
        name="stop",
        title="Stop",
        description=(
            "Stop the car at once and cancel anything running: a move, look_around, a capture orbit or "
            "follow. The current driver and admins can always stop the car."
        ),
    ),
    ToolSpec(
        name="look_around",
        title="Look around",
        description=(
            "Turn to the right on the spot through a full circle, taking a picture at each step "
            f"(views {_span(LOOK_AROUND_VIEWS)}, default {LOOK_AROUND_VIEWS_DEFAULT}, so "
            f"{360 // LOOK_AROUND_VIEWS_DEFAULT} degrees apart). Returns them as one labelled grid, "
            "left to right and top to bottom, each labelled with its direction from the start and "
            "the distance ahead. Use it to get your bearings or search a room. Takes several "
            "seconds. The car ends up roughly facing where it started."
        ),
        properties={
            "views": _int(
                LOOK_AROUND_VIEWS, "How many pictures to take around the circle.", LOOK_AROUND_VIEWS_DEFAULT
            )
        },
    ),
    ToolSpec(
        name="capture_photo",
        title="Capture photo",
        description=(
            "Save a high-quality photo into a capture that the user can download as a zip from the "
            "Scout website. Leave capture_id out to start a new capture, or pass the capture_id from "
            "an earlier result to add another photo to it. Returns a preview and the download link."
        ),
        properties={
            "capture_id": {
                "type": "string",
                "pattern": CAPTURE_ID_PATTERN,
                "description": "Add the photo to this earlier capture of yours (its capture_id).",
            },
            "note": {
                "type": "string",
                "maxLength": NOTE_MAX_LENGTH,
                "description": "A short note saved with the photo.",
            },
        },
    ),
    ToolSpec(
        name="capture_orbit",
        title="Capture orbit",
        description=(
            "Circle around the object in front of the camera, keeping it in view, and take evenly "
            f"spaced photos all the way round (photos {_span(ORBIT_PHOTOS)}, default "
            f"{ORBIT_PHOTOS_DEFAULT}). The photos suit photogrammetry, for example building a 3D model "
            "in Blender. First place the object in the middle of the picture, about 30-50 cm ahead "
            "(the car refuses if it is closer than about 20 cm or further than 150 cm), with free "
            "floor around it. direction left strafes to the left, circling the object clockwise seen "
            "from above. Saves everything to a downloadable capture and returns previews and the "
            "download link. Takes a while."
        ),
        properties={
            "photos": _int(ORBIT_PHOTOS, "How many photos to take around the object.", ORBIT_PHOTOS_DEFAULT),
            "direction": _enum(SIDES, "Which way to circle.", "left"),
        },
    ),
    ToolSpec(
        name="find",
        title="Find objects",
        description=(
            "Take a picture and find the objects in it with the object detector (people, cats, dogs, "
            "balls, chairs and the rest of the 80 COCO classes). Returns the picture with each one "
            "boxed and numbered left to right, and a list with each one's class, how sure the "
            "detector is, its direction from straight ahead and its box. Pass target to keep one "
            "class only. Use it before follow: look at the picture, decide which one is the person "
            f"or thing you want, and call follow with pick set to its number ({_span(PICK)}). The "
            "numbers stay valid for 2 minutes. Sizes: small 320x240, medium 640x480 (default), "
            "large 800x600. It doesn't move the car or the camera."
        ),
        properties={
            "target": _enum(FOLLOW_TARGETS, "Only find this class. Leave out to find everything."),
            "size": _enum(LOOK_SIZES, "Picture size.", LOOK_SIZE_DEFAULT),
        },
        read_only=True,
    ),
    ToolSpec(
        name="follow",
        title="Follow",
        description=(
            "Follow a particular person or thing with the camera and the wheels. Call find first and "
            f"pass pick, the number of the one to follow in find's picture ({_span(PICK)}); its class "
            "replaces target. Without pick it follows the biggest `target` in view, or turns to look "
            "for one. It remembers what the one it follows looks like (the colours of its top and "
            "bottom halves) and keeps to the candidate that looks most like it, so it doesn't switch "
            "to someone else who walks past. Two people dressed alike can still fool it: check with "
            "follow_view. It moves in short steps, each decided from a picture taken while the car "
            "stands still: it turns towards the target and tilts the camera to keep it in view (a "
            "person's head and shoulders), then drives to keep a distance: distance_cm if you give "
            "one, otherwise about 1.2 m from a person and 60 cm from anything else. The distance is "
            "judged from the target's size in the picture, taking a person to be an adult (by the "
            "width of their head and shoulders) and anything else to be about the size of a cat, so "
            "it is rough for a child, a big dog or an object. To change it while following, call "
            "follow again with pick (from follow_view) and the new distance_cm. When the target goes "
            "out of sight it searches: it "
            "turns towards the side it went out, tilts if it went out at the top or bottom, or "
            "drives a short way towards where it vanished, then turns round looking for it, up to a "
            "full circle. It drives only while it's sure it has the right one: after losing it and "
            "finding one again, or when the detector is much less sure of it than before (a coat on a "
            "hook taken for a person), it only turns and tilts until it has seen it clearly a few "
            "times or you confirm by calling follow with its number from follow_view. "
            "It never drives forward towards anything the distance sensor sees close "
            "by, and the car is slow, so it can't keep up with someone walking. It runs in the "
            "background: the call returns once it has locked on, with the picture it locked on in "
            "(the target in a green box), and it follows for up to `seconds` "
            f"({_span(FOLLOW_SECONDS)}, default {FOLLOW_SECONDS_DEFAULT}). It ends when the time is "
            "up, when a search finds nothing, or when you call stop or send any move, which also "
            "hands the car back to you. Calling follow again with a new pick switches target. Watch "
            "it by calling follow_view with wait_s 10 again and again until it ends."
        ),
        properties={
            "target": _enum(
                FOLLOW_TARGETS,
                "What to follow when there's no pick: one of the 80 COCO object classes.",
                FOLLOW_TARGET_DEFAULT,
            ),
            "seconds": _int(
                FOLLOW_SECONDS, "The longest time to follow, in seconds.", FOLLOW_SECONDS_DEFAULT
            ),
            "pick": _int(
                PICK,
                "The number of the one to follow, from the latest find or follow_view picture "
                f"({_span(PICK)}). Leave out to follow the biggest target in view.",
            ),
            "distance_cm": _int(
                FOLLOW_DISTANCE,
                f"About how far from the target to keep, in cm ({_span(FOLLOW_DISTANCE)}). Leave out "
                "for about 120 cm from a person and 60 cm from anything else. The car's obstacle "
                "distance still stops it short of anything right in front.",
            ),
        },
    ),
    ToolSpec(
        name="follow_view",
        title="Follow view",
        description=(
            "See what follow sees: the latest picture it acted on, with the one it follows in a green "
            "box marked 'following' and other candidates of the same class numbered in yellow, plus "
            "a second picture of the target as it looked when follow locked on. Compare them to "
            "check it is following the right one. If it isn't, call follow with pick set to the "
            "right number in this picture to switch, or call stop or any move to take over. Also "
            "returns how alike the target looks to the original (match, 0 to 1), whether another "
            "candidate looks almost as alike, whether it is searching, whether it is holding back from "
            "driving until it's sure (and why), and what it did last. It doesn't move the car. With "
            "wait_s it first waits up to that many seconds for something that needs you: follow "
            "holding back until you confirm, losing the target, another one close by looking almost as "
            "alike, or follow ending. It returns as soon as one happens (woke_because says which), so "
            "calling it again and again with wait_s 10 watches a follow."
        ),
        properties={
            "wait_s": _int(
                FOLLOW_VIEW_WAIT,
                f"Seconds to wait first for something that needs you ({_span(FOLLOW_VIEW_WAIT)}, "
                "default 0, which returns at once).",
                0,
            ),
        },
        read_only=True,
    ),
    ToolSpec(
        name="release_car",
        title="Release car",
        description=(
            "Stop being the driver so someone else can drive. Call it when you're done. The car is "
            "also released automatically after 2 minutes without commands."
        ),
    ),
]

TOOLS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOLS}


class InvalidArguments(Exception):
    """The arguments don't match the tool's schema. The message is written for the AI."""


def _is_int(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and value.is_integer()


def validate_args(spec: ToolSpec, args: dict[str, Any] | None) -> dict[str, Any]:
    """Check arguments against the schema and fill in defaults. Returns clean arguments."""
    args = dict(args or {})
    unknown = sorted(set(args) - set(spec.properties))
    if unknown:
        allowed = ", ".join(spec.properties) or "no arguments"
        raise InvalidArguments(f"{spec.name} doesn't take {', '.join(unknown)}. It takes: {allowed}.")
    for name in spec.required:
        if args.get(name) is None:
            raise InvalidArguments(f"{spec.name} needs {name}.")

    clean: dict[str, Any] = {}
    for name, prop in spec.properties.items():
        value = args.get(name)
        if value is None:
            if "default" in prop:
                clean[name] = prop["default"]
            continue
        kind = prop["type"]
        if kind == "integer":
            if not _is_int(value):
                raise InvalidArguments(f"{name} must be a whole number.")
            value = int(value)
            if not prop["minimum"] <= value <= prop["maximum"]:
                raise InvalidArguments(f"{name} must be between {prop['minimum']} and {prop['maximum']}.")
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise InvalidArguments(f"{name} must be true or false.")
        elif kind == "string":
            if not isinstance(value, str):
                raise InvalidArguments(f"{name} must be a string.")
            if "enum" in prop and value not in prop["enum"]:
                raise InvalidArguments(f"{name} must be one of: {', '.join(prop['enum'])}.")
            if "maxLength" in prop and len(value) > prop["maxLength"]:
                raise InvalidArguments(f"{name} can be at most {prop['maxLength']} characters.")
            if "pattern" in prop and not re.match(prop["pattern"], value):
                raise InvalidArguments(f"{name} isn't valid.")
        clean[name] = value
    return clean
