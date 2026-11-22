"""Motion maths: directions, mecanum mixing, estimates, turn and orbit plans.

Conventions follow protocol/uart.md: ``vx`` forward (+) / back (−), ``vy`` right (+) /
left (−), ``wz`` clockwise (+) / counter-clockwise (−) seen from above, each in percent
of full speed. The calibration values in ``CarSettings`` are the speeds at 100 %; how
fast a wheel turns at a lower percentage follows the motor curve in ``speed_share``.
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


# How the UNO drives its two PWM pins (firmware/scout_uno/src/core/config.h). "split"
# (SCOUT_PWM_SPLIT 1, the default build): one pin per side, which is how the AD176 is
# wired. "shared" (SCOUT_PWM_SPLIT 0): both pins carry the fastest wheel's duty.
# "ideal": every wheel at its own speed, which the stock shield can't do.
PwmLayout = Literal["shared", "split", "ideal"]

# kCoastDivisor: a wheel below a quarter of the fastest wheel on its pin coasts.
COAST_DIVISOR = 4

Wheels = tuple[int, int, int, int]  # FL, RL, FR, RR in percent

# The UNO turns a wheel's percentage into a PWM duty: 0 stays 0 and 1..100 % map
# linearly onto min_pwm..255 (``percentToPwm`` in firmware/scout_uno/src/core/mecanum.cpp).
# How fast a motor then runs was measured on the AD176's floor (docs/hardware.md, "Speed
# on the floor"): turning speeds in degrees per second at four duties. The curve isn't a
# straight line: it sags around 80 % and picks up again at 255, where the pin stays on.
# So the measurements are used as they are, with straight lines between them and down to
# no speed at a duty of about 25 (where the low-speed forward and turning runs point).
# Forward and sideways runs at lower duties fit the same shape within about 2 %. From a
# standstill the motors only start at a duty of about 100, which is what min_pwm is for.
MEASURED_TURN_RATES: tuple[tuple[int, float], ...] = (
    (25, 0.0),
    (138, 99.4),  # 20 % at min_pwm 110: 85° in 0.85 s, 88° in 0.89 s (twice)
    (172, 132.0),  # 50 % at min_pwm 90: 132° in 1 s
    (226, 166.5),  # 80 % at min_pwm 110: 84° in 0.5 s, 165° in 1 s
    (255, 202.0),  # 100 %: 202° in 1 s
)
SPEED_CURVE: tuple[tuple[int, float], ...] = tuple(
    (duty, rate / MEASURED_TURN_RATES[-1][1]) for duty, rate in MEASURED_TURN_RATES
)

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


def wheel_output(vx: int, vy: int, wz: int, layout: PwmLayout = "split") -> Wheels:
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


def percent_to_pwm(percent: int, min_pwm: int) -> int:
    """The UNO's ``percentToPwm`` for one wheel's speed (the sign is ignored)."""
    p = abs(int(percent))
    if p == 0:
        return 0
    if p >= 100:
        return 255
    return min_pwm + ((p - 1) * (255 - min_pwm) + 49) // 99


def duty_share(duty: float) -> float:
    """How much of its full speed (0..1) a running motor reaches at a PWM duty (0..255),
    from ``SPEED_CURVE``."""
    d = abs(duty)
    (d0, s0) = SPEED_CURVE[0]
    if d <= d0:
        return 0.0
    for d1, s1 in SPEED_CURVE[1:]:
        if d <= d1:
            return s0 + (s1 - s0) * (d - d0) / (d1 - d0)
        d0, s0 = d1, s1
    return 1.0


def speed_share(percent: float, min_pwm: int) -> float:
    """How much of its full speed (-1..1) a wheel driven at ``percent`` reaches."""
    pwm = percent_to_pwm(round(percent), min_pwm)
    if pwm == 0:
        return 0.0
    return math.copysign(duty_share(pwm), percent)


def wheel_speeds(wheels: Wheels, min_pwm: int) -> tuple[float, float, float, float]:
    """Each wheel's real speed in percent of its full speed, from its driven percentage."""
    fl, rl, fr, rr = (100.0 * speed_share(w, min_pwm) for w in wheels)
    return fl, rl, fr, rr


def body_velocity(wheels: tuple[float, float, float, float]) -> tuple[float, float, float]:
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
    vx: int, vy: int, wz: int, ms: int, settings: CarSettings, *, layout: PwmLayout = "split"
) -> Estimate:
    """Dead reckoning for one move from what the wheels actually do on the UNO."""
    bvx, bvy, bwz = body_velocity(wheel_speeds(wheel_output(vx, vy, wz, layout), settings.min_pwm))
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
    return settings.turn_deg_per_s * speed_share(speed, settings.min_pwm)


def turn_plan(
    degrees: float, speed: int, settings: CarSettings, *, min_ms: int = 150, min_speed: int = 20
) -> tuple[int, list[int]]:
    """(speed, chunk durations) for rotating ``degrees`` in place.

    Very short pulses barely get the motors going, so a small angle is turned more slowly
    instead (down to ``min_speed``) to keep the pulse near ``min_ms``.
    """
    total = degrees / turn_rate(speed, settings) * 1000.0
    while total < min_ms and speed > min_speed:
        speed -= 1
        total = degrees / turn_rate(speed, settings) * 1000.0
    return speed, split_ms(max(1, round(total)))


@dataclass(frozen=True)
class PlannedMove:
    vx: int
    vy: int
    wz: int
    ms: int


BLEND_SLACK = 5  # percent the blend orbit's fastest wheel may run below ``speed``
BLEND_TOLERANCE = 0.03  # a blend that misses the radius by more falls back to the split plan


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
    ahead, so choosing ``v/ω = R`` is an exact orbit. Wheel speed doesn't follow the
    percentage (``speed_share``), so the pair whose fastest wheel runs at ``speed`` or up
    to ``BLEND_SLACK`` percent below it that comes closest to ``R`` is searched for, and
    the move is timed so the rotation is exact; the radius error is 3 % at most. If no
    pair gets that close (at low speeds), it uses the split plan instead. It needs every
    wheel at its own speed, which the stock shield can't do: with one duty per side the
    two wheels on a side run at the faster one's speed (see ``wheel_output``).

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
    strafe_rate = settings.strafe_cm_per_s * speed_share(speed, settings.min_pwm)
    spin_rate = settings.turn_deg_per_s * speed_share(speed, settings.min_pwm)
    if mode == "split":
        half_s = (step_deg / 2.0) / spin_rate
        chord_s = 2.0 * radius_cm * math.sin(theta / 2.0) / strafe_rate
        return (
            _timed(0, 0, spin * speed, half_s)
            + _timed(0, side * speed, 0, chord_s)
            + _timed(0, 0, spin * speed, half_s)
        )
    # Blend: the sideways speed per degree of rotation has to be R in radians.
    target = radius_cm * theta / step_deg
    # The fastest wheel runs at vy + wz. Keep that within a few percent of ``speed``, so
    # the move doesn't crawl (a slow pair can match the ratio as well as a fast one).
    best: tuple[float, int, int, float] | None = None
    for fastest in range(speed, max(1, speed - BLEND_SLACK) - 1, -1):
        for wz_pct in range(1, fastest):
            vy_pct = fastest - wz_pct
            _, bvy, bwz = body_velocity(
                wheel_speeds(wheel_output(0, vy_pct, wz_pct, "ideal"), settings.min_pwm)
            )
            v = bvy / 100.0 * settings.strafe_cm_per_s
            w = bwz / 100.0 * settings.turn_deg_per_s
            if v <= 0 or w <= 0:
                continue
            error = abs(v / w - target)
            if best is None or error < best[0]:
                best = (error, vy_pct, wz_pct, w)
    if best is None or best[0] > BLEND_TOLERANCE * target:
        # Too slow to share between strafing and turning: every running wheel turns at
        # least as fast as min_pwm drives it, which limits how tight the circle can be.
        return orbit_step(radius_cm, step_deg, direction, settings, speed=speed, mode="split")
    _, vy_pct, wz_pct, w = best
    return _timed(0, side * vy_pct, spin * wz_pct, step_deg / w)


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
    rate = settings.forward_cm_per_s * speed_share(speed, settings.min_pwm)
    ms = max(1, round(abs(error) / rate * 1000))
    return PlannedMove(speed if error > 0 else -speed, 0, 0, min(ms, MAX_MOVE_MS))
