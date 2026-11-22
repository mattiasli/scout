"""Motion maths: directions, mecanum mixing, estimates, turn and orbit plans.

Conventions follow protocol/uart.md: ``vx`` forward (+) / back (−), ``vy`` right (+) /
left (−), ``wz`` clockwise (+) / counter-clockwise (−) seen from above, each in percent
of full speed. The calibration values in ``CarSettings`` are rough speeds at 100 %.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from scout.contracts import CarSettings

MAX_MOVE_MS = 3000

_D = math.sqrt(0.5)
# Unit vectors (forward, right). Diagonals are 45° so their ground speed equals ``speed``;
# on mecanum wheels they drive two diagonal wheels and leave the other two still.
DIRECTIONS: dict[str, tuple[float, float]] = {
    "forward": (1.0, 0.0),
    "back": (-1.0, 0.0),
    "right": (0.0, 1.0),
    "left": (0.0, -1.0),
    "forward_right": (_D, _D),
    "forward_left": (_D, -_D),
    "back_right": (-_D, _D),
    "back_left": (-_D, -_D),
}


def direction_velocity(direction: str, speed: int) -> tuple[int, int, int]:
    """(vx, vy, wz) for a named direction at ``speed`` percent."""
    fx, fy = DIRECTIONS[direction]
    return round(fx * speed), round(fy * speed), 0


# How the UNO drives its two PWM pins (firmware/scout_uno/src/core/config.h). "shared"
# (SCOUT_PWM_SPLIT 0, the default build): both pins carry the fastest wheel's duty.
# "split": one pin per side. "ideal": every wheel at its own speed, which the stock
# shield can't do.
PwmLayout = Literal["shared", "split", "ideal"]

# kCoastDivisor: a wheel below a quarter of the fastest wheel on its pin coasts.
COAST_DIVISOR = 4

Wheels = tuple[int, int, int, int]  # FL, RL, FR, RR in percent

_SIDES = ((0, 1), (2, 3))  # left (FL, RL) and right (FR, RR)


def wheel_mix(vx: int, vy: int, wz: int) -> Wheels:
    """Wheel speeds (FL, RL, FR, RR) in percent, as the UNO's ``mixWheels`` computes them.

    If any wheel would exceed 100 %, all four are scaled by 100/peak, truncating toward
    zero like the firmware's integer division.
    """
    raw = (vx + vy + wz, vx - vy + wz, vx - vy - wz, vx + vy - wz)
    peak = max(abs(r) for r in raw)

    def scaled(r: int) -> int:
        return r if peak <= 100 else (abs(r) * 100 // peak) * (1 if r >= 0 else -1)

    return scaled(raw[0]), scaled(raw[1]), scaled(raw[2]), scaled(raw[3])


def wheel_output(vx: int, vy: int, wz: int, layout: PwmLayout = "shared") -> Wheels:
    """What each wheel actually runs at, following the UNO's ``mecanumOutput``.

    Every driven wheel turns at the duty of the fastest wheel on its pin, and a wheel
    whose share is below a quarter of that coasts (exactly a quarter is still driven).
    Pure moves (the eight directions, turning in place) come out exactly. A blend loses
    its small part: the shared duty evens the wheels out, or the slow ones coast.
    """
    speeds = wheel_mix(vx, vy, wz)
    if layout == "ideal":
        return speeds
    groups = ((0, 1, 2, 3),) if layout == "shared" else _SIDES
    out = [0, 0, 0, 0]
    for group in groups:
        reference = max(abs(speeds[i]) for i in group)
        for i in group:
            m = abs(speeds[i])
            if m == 0 or m * COAST_DIVISOR < reference:
                continue
            out[i] = reference if speeds[i] > 0 else -reference
    return out[0], out[1], out[2], out[3]


def body_velocity(wheels: Wheels) -> tuple[float, float, float]:
    """Mecanum forward kinematics: (vx, vy, wz) in percent from (FL, RL, FR, RR)."""
    fl, rl, fr, rr = wheels
    return (fl + fr + rl + rr) / 4, (fl - fr - rl + rr) / 4, (fl - fr + rl - rr) / 4


@dataclass(frozen=True)
class Estimate:
    """Dead-reckoned displacement of one move, from the calibration settings."""

    forward_cm: float
    right_cm: float
    clockwise_deg: float

    @property
    def distance_cm(self) -> float:
        return math.hypot(self.forward_cm, self.right_cm)


def estimate(
    vx: int, vy: int, wz: int, ms: int, settings: CarSettings, *, layout: PwmLayout = "shared"
) -> Estimate:
    """Dead reckoning for one move from what the wheels actually do on the UNO."""
    bvx, bvy, bwz = body_velocity(wheel_output(vx, vy, wz, layout))
    t = ms / 100_000.0  # percent → fraction, and ms → s
    return Estimate(
        forward_cm=bvx * settings.forward_cm_per_s * t,
        right_cm=bvy * settings.strafe_cm_per_s * t,
        clockwise_deg=bwz * settings.turn_deg_per_s * t,
    )


def split_ms(total_ms: int, max_ms: int = MAX_MOVE_MS) -> list[int]:
    """Split a duration into near-equal chunks of at most ``max_ms``."""
    total_ms = max(1, int(total_ms))
    n = max(1, math.ceil(total_ms / max_ms))
    base, extra = divmod(total_ms, n)
    return [base + (1 if i < extra else 0) for i in range(n)]


def turn_rate(speed: int, settings: CarSettings) -> float:
    """Degrees per second when rotating in place at ``speed`` percent."""
    return settings.turn_deg_per_s * speed / 100.0


def turn_plan(
    degrees: float, speed: int, settings: CarSettings, *, min_ms: int = 150, min_speed: int = 20
) -> tuple[int, list[int]]:
    """(speed, chunk durations) for rotating ``degrees`` in place.

    Very short pulses barely get the motors going, so a small angle is turned more slowly
    instead (down to ``min_speed``) to keep the pulse near ``min_ms``.
    """
    total = degrees / turn_rate(speed, settings) * 1000.0
    if total < min_ms and speed > min_speed:
        speed = max(min_speed, math.ceil(speed * total / min_ms))
        total = degrees / turn_rate(speed, settings) * 1000.0
    return speed, split_ms(max(1, round(total)))


@dataclass(frozen=True)
class PlannedMove:
    vx: int
    vy: int
    wz: int
    ms: int


def _timed(vx: int, vy: int, wz: int, seconds: float) -> list[PlannedMove]:
    return [PlannedMove(vx, vy, wz, ms) for ms in split_ms(max(1, round(seconds * 1000)))]


def orbit_step(
    radius_cm: float,
    step_deg: float,
    direction: Literal["left", "right"],
    settings: CarSettings,
    *,
    speed: int,
    mode: Literal["split", "blend"] = "split",
) -> list[PlannedMove]:
    """Moves that carry the car ``step_deg`` around an object ``radius_cm`` ahead.

    Geometry: put the object at the origin and the car on a circle of radius ``R``
    facing it. Moving the car ``θ`` around the circle means a sideways travel of
    ``R·θ`` along the arc and a rotation of ``θ`` the opposite way, so it keeps facing
    the object. ``direction="left"`` strafes to the car's left and rotates clockwise
    (the car circles the object clockwise seen from above); ``"right"`` mirrors that.

    ``mode="blend"``: one move with constant ``vy`` and ``wz``. A body moving sideways at
    ``v`` while turning at ``ω`` traces a circle of radius ``v/ω`` around a point straight
    ahead, so choosing ``v/ω = R`` is an exact orbit. ``wz`` is rounded first so the
    rotation is exact; the radius error from rounding ``vy`` is about 1-2 %. It needs
    the wheels to run at different speeds, which the stock shield can't do yet: with the
    shared PWM duty every running wheel turns at the fastest one's speed, so the small
    rotation share cancels out (see ``wheel_output``).

    ``mode="split"`` (default): three pure moves the shield drives exactly. Rotate
    ``θ/2``, strafe the chord ``2R·sin(θ/2)``, rotate ``θ/2``. After the first rotation
    the car's sideways axis is parallel to the chord, so the strafe lands exactly on the
    next point of the circle, and the second rotation faces the object again. Mid-chord
    the car is ``R·(1 − cos(θ/2))`` inside the circle (about 3.4 % of ``R`` at 30°
    steps), so fewer, larger steps pass closer to the object.

    Limits: ``R`` is the ultrasonic distance to the object's front surface, so the circle
    is centred on that surface, not on the object's middle; deep objects drift in and out
    of frame (the radius correction in the robot helps). Nothing measures the sides, so
    the strafe can bump into things beside the car. The calibration speeds are rough and
    wheel slip adds up over a full circle, which is why the robot re-measures the
    distance after every step.
    """
    theta = math.radians(step_deg)
    side = -1 if direction == "left" else 1  # strafe sign
    spin = -side  # rotate the opposite way
    strafe_rate = settings.strafe_cm_per_s * speed / 100.0
    spin_rate = settings.turn_deg_per_s * speed / 100.0
    if mode == "split":
        half_s = (step_deg / 2.0) / spin_rate
        chord_s = 2.0 * radius_cm * math.sin(theta / 2.0) / strafe_rate
        return (
            _timed(0, 0, spin * speed, half_s)
            + _timed(0, side * speed, 0, chord_s)
            + _timed(0, 0, spin * speed, half_s)
        )
    # Blend: seconds each part would take at 100 %, then share ``speed`` between them.
    a = radius_cm * theta / settings.strafe_cm_per_s
    b = step_deg / settings.turn_deg_per_s
    wz_pct = max(1, round(speed * b / (a + b)))
    seconds = b * 100.0 / wz_pct
    vy_pct = min(100 - wz_pct, max(1, round(a * 100.0 / seconds)))
    return _timed(0, side * vy_pct, spin * wz_pct, seconds)


def radius_correction(
    measured_cm: int | None,
    radius_cm: float,
    settings: CarSettings,
    *,
    speed: int = 30,
    tolerance_cm: float = 4.0,
    max_cm: float = 15.0,
) -> PlannedMove | None:
    """A short forward/back move that brings the car back onto the orbit circle."""
    if measured_cm is None:
        return None
    error = measured_cm - radius_cm
    if abs(error) <= max(tolerance_cm, radius_cm * 0.08):
        return None
    error = max(-max_cm, min(max_cm, error))
    rate = settings.forward_cm_per_s * speed / 100.0
    ms = max(1, round(abs(error) / rate * 1000))
    return PlannedMove(speed if error > 0 else -speed, 0, 0, min(ms, MAX_MOVE_MS))
