"""The follow controller: from a detection to one short, measured step.

It is pure logic so it can be tested without a car. The robot runs it on a stream frame
taken while the car stands still, sends the step (a camera tilt, a bounded move, or
both), waits for the move to end and the picture to settle, and looks again. Deciding
only from pictures taken at a standstill keeps the picture and the step in agreement.
The first version steered continuously, re-sending a turn several times a second, and
on the real car (whose slowest turn is still about 100°/s) it swung right past the
person it was following.

Steps:

* **Turning.** The box centre becomes a direction through the camera's field of view
  and its tilt, and the car turns ``turn_gain`` of the angle (60 %) as one pulse timed
  with the turn calibration (``motion.turn_rate``). Turning less than the whole angle
  and then looking again closes in without overshooting, even if the field of view is
  off by a lot. A pulse is never shorter than ``min_turn_ms``, and the shortest pulse is
  only used when it doesn't turn past the target.
* **Tilting.** The camera follows the target up and down. A box wholly in the picture
  is kept in the middle. One cut off at the bottom (a person seen from 12 cm off the
  floor, at any distance worth following) has its top kept near the top of the picture,
  so a person's head and shoulders stay in view. One cut off at the top tilts up a step
  to find its top. The servo goes where it is told, so a tilt takes most of the error at
  once. It starts at 30° up for a person and level for anything else.
* **Distance.** A size cue against a target size. A person is judged by box width, the
  width of their head and shoulders. Anything else is judged by box height while it is
  wholly in the picture, and by width once it's cut off at the top or bottom, because
  its height then says more about the tilt than the distance. The width is corrected
  for how high the camera looks. Apparent size is inversely proportional to distance,
  so ``target / size`` is how many times the target distance away it is. The step
  drives ``drive_gain`` of the difference, at most ``max_step_cm``.
* **Safety.** Forward is never commanded when the ultrasonic reads within
  ``obst_cm + clearance_cm``, and a step never goes past that. The UNO's own obstacle
  stop backs this up. Backing off is blind, so it is kept short.

Every move is a pure one (a turn in place, or straight forward or back), which the
two-pin shield drives exactly. Turning comes first; the car only drives once the target
is near the middle. A tilt can go with either.

The target is the detection ``pick_target`` chooses: the biggest of the right class at
the start, then whichever is nearest its last position. It follows a class, not a
particular cat or person.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from scout.contracts import CarSettings
from scout.robot.detector import Detection
from scout.robot.motion import speed_share, turn_rate

ASPECT = 0.75  # picture height / width: every size the car takes is 4:3


@dataclass
class FollowTuning:
    centre_deadband_deg: float = 5.0  # this close to the middle counts as centred
    turn_gain: float = 0.6  # share of the measured angle turned per step
    turn_speed: int = 20  # percent; the slowest turn measured on the floor (about 100°/s)
    min_turn_ms: int = 40
    # Tilt. The UNO clamps the servo to kTiltMinDeg..kTiltMaxDeg
    # (firmware/scout_uno/src/core/config.h).
    start_tilt_person: int = 30
    start_tilt_other: int = 0
    tilt_min: int = -40
    tilt_max: int = 40
    tilt_gain: float = 0.8
    tilt_deadband_deg: float = 4.0
    tilt_step_deg: int = 10  # up, when the target is cut off at the top
    top_aim: float = 0.2  # where the top of a box cut off at the bottom is kept
    # Size targets. ``*_cm`` is roughly how far away that size is; it only scales the
    # steps, because every step is measured again.
    target_height: float = 0.45  # box height / picture height, target wholly in view
    target_height_cm: float = 60.0  # a cat about 25 cm tall
    # Box width / picture width, corrected for the camera's tilt. Measured on the floor:
    # a person standing at 200 cm was 0.15 wide (0.159 corrected, camera about 15° up),
    # which makes 0.26 about 1.2 m.
    target_width: float = 0.26
    target_width_cm: float = 120.0
    size_deadband: float = 0.15  # within ±15 % of the target distance is close enough
    drive_gain: float = 0.6
    drive_speed: int = 40
    back_speed: int = 30
    max_step_cm: float = 30.0
    max_back_cm: float = 20.0
    min_drive_ms: int = 100
    top_edge: float = 0.02  # a box starting above this is cut off at the top
    bottom_edge: float = 0.97  # one ending below this is cut off at the bottom
    clearance_cm: int = 10


@dataclass(frozen=True)
class FollowStep:
    """A camera tilt to set (or None), one pure move or none (``ms == 0``), and why."""

    vx: int
    wz: int
    ms: int
    why: str
    tilt: int | None = None

    @property
    def moves(self) -> bool:
        return self.ms > 0 and bool(self.vx or self.wz)

    @property
    def acts(self) -> bool:
        return self.moves or self.tilt is not None


def start_tilt(label: str, tuning: FollowTuning | None = None) -> int:
    """The camera tilt to start following ``label`` with, in degrees up from level."""
    t = tuning or FollowTuning()
    return t.start_tilt_person if label == "person" else t.start_tilt_other


def pick_target(
    detections: list[Detection], label: str, previous: Detection | None, *, min_score: float
) -> Detection | None:
    """The detection to follow: the one nearest the last position, else the biggest."""
    candidates = [d for d in detections if d.label == label and d.score >= min_score]
    if not candidates:
        return None
    if previous is not None:
        return min(candidates, key=lambda d: (d.cx - previous.cx) ** 2 + (d.cy - previous.cy) ** 2)
    return max(candidates, key=lambda d: d.width * d.height)


def _direction(x: float, y: float, hfov_deg: float, tilt_deg: float) -> tuple[float, float]:
    """Where a point of the picture is: degrees right of straight ahead, and up from level."""
    tx = math.tan(math.radians(hfov_deg / 2))
    right = (x - 0.5) * 2 * tx  # at unit depth along the camera's axis
    up = (0.5 - y) * 2 * tx * ASPECT
    t = math.radians(tilt_deg)
    level_up = up * math.cos(t) + math.sin(t)  # the camera's frame turned down by the tilt
    level_ahead = math.cos(t) - up * math.sin(t)
    yaw = math.degrees(math.atan2(right, level_ahead))
    elevation = math.degrees(math.atan2(level_up, math.hypot(right, level_ahead)))
    return yaw, elevation


def bearing_deg(target: Detection, hfov_deg: float, tilt_deg: float = 0.0) -> float:
    """How far right (+) or left (−) of straight ahead the box centre is, in degrees."""
    return _direction(target.cx, target.cy, hfov_deg, tilt_deg)[0]


def _above_middle_deg(y: float, hfov_deg: float) -> float:
    """How far above (+) the middle of the picture a row is, in degrees."""
    return math.degrees(math.atan((0.5 - y) * 2 * math.tan(math.radians(hfov_deg / 2)) * ASPECT))


def _tilt(
    target: Detection, tilt_deg: float, tilt_scale: float, hfov_deg: float, t: FollowTuning
) -> tuple[int | None, str]:
    """The tilt setting that puts the target where it belongs in the picture, and why."""
    name = target.label
    if target.y0 <= t.top_edge:
        goal = tilt_deg + t.tilt_step_deg
        reason = f"the {name}'s top is above the picture"
    else:
        if target.y1 >= t.bottom_edge:
            row, aim, reason = target.y0, t.top_aim, f"keeping the {name}'s top near the top of the picture"
        else:
            row, aim, reason = target.cy, 0.5, f"keeping the {name} in the middle of the picture"
        change = _above_middle_deg(row, hfov_deg) - _above_middle_deg(aim, hfov_deg)
        if abs(change) < t.tilt_deadband_deg:
            return None, ""
        goal = tilt_deg + change * t.tilt_gain / tilt_scale
    new = max(t.tilt_min, min(t.tilt_max, round(goal)))
    if new == round(tilt_deg):
        return None, ""  # at the end of the servo's range
    direction = "up" if new > tilt_deg else "down"
    return new, f"tilt {direction} to {new}°, {reason}"


def _hold(why: str) -> FollowStep:
    return FollowStep(0, 0, 0, f"hold: {why}")


def plan(
    target: Detection,
    cm: int | None,
    settings: CarSettings,
    *,
    hfov_deg: float,
    tilt_deg: float = 0.0,
    tilt_scale: float = 1.0,
    tuning: FollowTuning | None = None,
) -> FollowStep:
    """The next step towards ``target``, from a picture taken at a standstill with the
    camera tilt set to ``tilt_deg``. The camera's real angle is ``tilt_deg * tilt_scale``
    (a hobby servo turns a little more or less than it's told); the step's tilt is a
    setting again."""
    t = tuning or FollowTuning()
    move = _move(target, cm, settings, hfov_deg, tilt_deg * tilt_scale, t)
    tilt, tilt_why = _tilt(target, tilt_deg, tilt_scale, hfov_deg, t)
    if tilt is None:
        return move
    return FollowStep(move.vx, move.wz, move.ms, f"{tilt_why}; {move.why}", tilt)


def _move(
    target: Detection,
    cm: int | None,
    settings: CarSettings,
    hfov_deg: float,
    tilt_deg: float,
    t: FollowTuning,
) -> FollowStep:
    name = target.label
    angle = bearing_deg(target, hfov_deg, tilt_deg)
    if abs(angle) >= t.centre_deadband_deg:
        rate = turn_rate(t.turn_speed, settings)
        ms = round(abs(angle) * t.turn_gain / rate * 1000)
        if ms < t.min_turn_ms and rate * t.min_turn_ms / 1000 <= abs(angle):
            ms = t.min_turn_ms
        if ms >= t.min_turn_ms:
            side = "right" if angle > 0 else "left"
            wz = t.turn_speed if angle > 0 else -t.turn_speed
            turned = rate * ms / 1000
            why = f"turn {turned:.0f}° {side}: the {name} is {abs(angle):.0f}° off centre"
            return FollowStep(0, wz, ms, why)

    cut_off = target.y0 <= t.top_edge or target.y1 >= t.bottom_edge
    if name == "person" or cut_off:
        # Apparent width shrinks with the distance along the line of sight and grows away
        # from the middle of the picture. Undoing both leaves the distance across the floor.
        _, elevation = _direction(0.5, target.cy, hfov_deg, tilt_deg)
        off_middle = _above_middle_deg(target.cy, hfov_deg)
        size = target.width * math.cos(math.radians(off_middle)) / math.cos(math.radians(elevation))
        goal, goal_cm, cue = t.target_width, t.target_width_cm, "width"
    else:
        size, goal, goal_cm, cue = target.height, t.target_height, t.target_height_cm, "height"
    if size <= 0:
        return _hold(f"the {name}'s box is empty")
    ratio = goal / size  # its distance / the target distance
    if abs(ratio - 1) <= t.size_deadband:
        return _hold(f"the {name} is centred and about the right distance away ({cue} {size:.2f})")

    step_cm = abs(ratio - 1) * goal_cm * t.drive_gain
    if ratio > 1:  # too far
        if cm is not None:
            room = cm - (settings.obst_cm + t.clearance_cm)
            if room <= 0:
                return _hold(f"something is {cm} cm ahead")
            step_cm = min(step_cm, room)
        step_cm = min(step_cm, t.max_step_cm)
        rate = settings.forward_cm_per_s * speed_share(t.drive_speed, settings.min_pwm)
        ms = max(t.min_drive_ms, round(step_cm / rate * 1000))
        why = f"forward about {step_cm:.0f} cm: the {name} looks far ({cue} {size:.2f})"
        return FollowStep(t.drive_speed, 0, ms, why)
    step_cm = min(step_cm, t.max_back_cm)
    rate = settings.forward_cm_per_s * speed_share(t.back_speed, settings.min_pwm)
    ms = max(t.min_drive_ms, round(step_cm / rate * 1000))
    why = f"back about {step_cm:.0f} cm: the {name} looks close ({cue} {size:.2f})"
    return FollowStep(-t.back_speed, 0, ms, why)
