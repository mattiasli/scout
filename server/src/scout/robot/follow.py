"""The follow controller: from a detection to a short, gentle move.

It is pure logic so it can be tested without a car. The robot runs it about five times a
second on the latest stream frame and sends each command as a bounded move (about
350 ms) that the next command replaces, which gives continuous motion that stops by
itself within a fraction of a second if the server goes quiet.

Control:

* Horizontal offset of the box centre (−1 left edge .. +1 right edge) → ``wz``.
* Box height as a share of the frame, a stand-in for distance → ``vx``: too small means
  too far (drive forward), too big means too close (back off).
* Forward is never commanded when the ultrasonic says the front is within
  ``obst_cm + clearance_cm``; the UNO's own obstacle stop backs this up.

By default it never blends turning and driving in one move. The stock shield has two
PWM pins for four wheels, so the firmware approximates blended moves: every running
wheel gets the fastest wheel's duty, which cancels a small turn share, and a wheel below
25 % of the fastest coasts (``motion.wheel_output``). Pure moves come out exactly. It
turns in place when the target is well off centre and drives straight otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass

from scout.robot.detector import Detection


@dataclass
class FollowTuning:
    target_height: float = 0.45  # box height / frame height to hold
    height_deadband: float = 0.08
    centre_deadband: float = 0.15
    rotate_first: float = 0.35  # turn in place when the target is this far off centre
    turn_gain: float = 45.0
    min_turn: int = 25
    max_turn: int = 40
    forward_gain: float = 120.0
    min_drive: int = 25
    max_forward: int = 45
    max_back: int = 30
    clearance_cm: int = 10
    blend: bool = False


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


def _clamp(value: float, low: int, high: int) -> int:
    return int(max(low, min(high, round(value))))


def command(
    target: Detection, cm: int | None, obst_cm: int, tuning: FollowTuning | None = None
) -> tuple[int, int, int]:
    """(vx, vy, wz) for one follow step; (0, 0, 0) means hold still."""
    t = tuning or FollowTuning()
    offset = (target.cx - 0.5) * 2.0
    wz = 0
    if abs(offset) >= t.centre_deadband:
        magnitude = _clamp(abs(offset) * t.turn_gain, t.min_turn, t.max_turn)
        wz = magnitude if offset > 0 else -magnitude

    error = t.target_height - target.height
    vx = 0
    if abs(error) >= t.height_deadband:
        if error > 0:
            vx = _clamp(error * t.forward_gain, t.min_drive, t.max_forward)
        else:
            vx = -_clamp(-error * t.forward_gain, t.min_drive, t.max_back)
    if target.y1 > 0.97 and vx > 0:
        vx = 0  # cut off at the bottom edge: it's closer than the box suggests
    if vx > 0 and cm is not None and cm < obst_cm + t.clearance_cm:
        vx = 0

    if not t.blend and vx and wz:
        if abs(offset) >= t.rotate_first:
            vx = 0
        else:
            wz = 0
    return vx, 0, wz
