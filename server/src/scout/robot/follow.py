"""The follow controller: from a detection to one short, measured step.

It is pure logic so it can be tested without a car. The robot runs it on a stream frame
taken while the car stands still, sends the step as one bounded move, waits for the move
to end and the picture to settle, and looks again. Deciding only from pictures taken at
a standstill keeps the picture and the step in agreement. The first version steered
continuously, re-sending a turn several times a second, and on the real car (whose
slowest turn is still about 100°/s) it swung right past the person it was following.

Steps:

* **Turning.** The horizontal offset of the box centre becomes an angle through the
  camera's field of view, and the car turns ``turn_gain`` of it (60 %) as one pulse
  timed with the turn calibration (``motion.turn_rate``). Turning less than the whole
  angle and then looking again closes in without overshooting, even if the field of view
  is off by a lot. A pulse is never shorter than ``min_turn_ms``, and the shortest pulse
  is only used when it doesn't turn past the target.
* **Distance.** A size cue against a target size. A target that fits the picture uses
  its box height. One cut off at the top edge uses its width instead: a standing person
  seen from 12 cm off the floor loses the head and keeps about half the picture's height
  at any distance, but the shoulders get narrower with distance. Apparent size is
  inversely proportional to distance, so ``target / size`` is how many times the target
  distance away it is. The step drives ``drive_gain`` of the difference, at most
  ``max_step_cm``.
* **Safety.** Forward is never commanded when the ultrasonic reads within
  ``obst_cm + clearance_cm``, and a step never goes past that; nor when the box is cut
  off at the bottom edge, which means it's closer than its size suggests. The UNO's own
  obstacle stop backs this up. Backing off is blind, so it is kept short.

Every step is a pure move (a turn in place, or straight forward or back), which the
two-pin shield drives exactly. Turning comes first; the car only drives once the target
is near the middle.

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


@dataclass
class FollowTuning:
    centre_deadband_deg: float = 5.0  # this close to the middle counts as centred
    turn_gain: float = 0.6  # share of the measured angle turned per step
    turn_speed: int = 20  # percent; the slowest turn measured on the floor (about 100°/s)
    min_turn_ms: int = 40
    # Size targets. ``*_cm`` is roughly how far away that size is; it only scales the
    # steps, because every step is measured again.
    target_height: float = 0.45  # box height / picture height, target wholly in view
    target_height_cm: float = 60.0  # a cat about 25 cm tall
    target_width: float = 0.25  # box width / picture width, target cut off at the top
    target_width_cm: float = 120.0  # shoulders about 40 cm wide
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
    """One pure move, or none (``ms == 0``), and why, for status."""

    vx: int
    wz: int
    ms: int
    why: str

    @property
    def moves(self) -> bool:
        return self.ms > 0 and bool(self.vx or self.wz)


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


def bearing_deg(target: Detection, hfov_deg: float) -> float:
    """How far right (+) or left (−) of straight ahead the box centre is, in degrees."""
    offset = (target.cx - 0.5) * 2.0
    return math.degrees(math.atan(offset * math.tan(math.radians(hfov_deg / 2))))


def _hold(why: str) -> FollowStep:
    return FollowStep(0, 0, 0, f"hold: {why}")


def plan(
    target: Detection,
    cm: int | None,
    settings: CarSettings,
    *,
    hfov_deg: float,
    tuning: FollowTuning | None = None,
) -> FollowStep:
    """The next step towards ``target``, from a picture taken at a standstill."""
    t = tuning or FollowTuning()
    name = target.label

    angle = bearing_deg(target, hfov_deg)
    if abs(angle) >= t.centre_deadband_deg:
        rate = turn_rate(t.turn_speed, settings)
        ms = round(abs(angle) * t.turn_gain / rate * 1000)
        if ms < t.min_turn_ms and rate * t.min_turn_ms / 1000 <= abs(angle):
            ms = t.min_turn_ms
        if ms >= t.min_turn_ms:
            side = "right" if angle > 0 else "left"
            wz = t.turn_speed if angle > 0 else -t.turn_speed
            turned = rate * ms / 1000
            return FollowStep(
                0, wz, ms, f"turn {turned:.0f}° {side}: the {name} is {abs(angle):.0f}° off centre"
            )

    cut_top = target.y0 <= t.top_edge and target.y1 < t.bottom_edge
    if cut_top:
        size, goal, goal_cm, cue = target.width, t.target_width, t.target_width_cm, "width"
    else:
        size, goal, goal_cm, cue = target.height, t.target_height, t.target_height_cm, "height"
    if size <= 0:
        return _hold(f"the {name}'s box is empty")
    ratio = goal / size  # its distance / the target distance
    if abs(ratio - 1) <= t.size_deadband:
        return _hold(f"the {name} is centred and about the right distance away ({cue} {size:.2f})")

    step_cm = abs(ratio - 1) * goal_cm * t.drive_gain
    if ratio > 1:  # too far
        if target.y1 >= t.bottom_edge:
            return _hold(f"the {name} is cut off at the bottom of the picture, so it's closer than it looks")
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
    return FollowStep(
        -t.back_speed, 0, ms, f"back about {step_cm:.0f} cm: the {name} looks close ({cue} {size:.2f})"
    )
