"""A small 2-D room for the simulated car: walls, a door, furniture and a wandering cat.

Units are centimetres and seconds. Headings are radians, counter-clockwise from +x
(maths convention); the car's ``wz`` is clockwise-positive like the UART protocol, so a
positive ``wz`` lowers the heading.

The car is a disc for collisions. Its motion comes from the four wheels' PWM duties:
each motor's speed follows the curve measured on the AD176 (``motor_speeds``), and the
mecanum forward kinematics, scaled by the speeds in ``Physics``, turn those into body
motion, with a little random error per move so dead reckoning drifts the way it does on
a real floor.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, replace
from typing import Literal

import numpy as np

Wheels = tuple[float, float, float, float]  # FL, RL, FR, RR in percent
ZERO: Wheels = (0.0, 0.0, 0.0, 0.0)
INF = math.inf


@dataclass(frozen=True)
class Box:
    """An axis-aligned block standing on the floor."""

    x0: float
    y0: float
    x1: float
    y1: float
    height: float
    color: tuple[int, int, int]
    name: str


@dataclass(frozen=True)
class Disc:
    """A round object standing on the floor (a plant pot, a table leg...)."""

    x: float
    y: float
    r: float
    height: float
    color: tuple[int, int, int]
    name: str


@dataclass(frozen=True)
class Door:
    """A door in one wall. ``wall``: 0 = x=0, 1 = x=width, 2 = y=0, 3 = y=depth."""

    wall: int
    u0: float
    u1: float
    height: float = 200.0
    open: bool = False


@dataclass
class Pose:
    x: float
    y: float
    heading: float  # radians, CCW from +x


@dataclass
class Cat:
    x: float
    y: float
    heading: float = 0.0
    r: float = 11.0  # collision and ultrasonic radius
    length: float = 42.0  # nose to tail, for the camera
    height: float = 24.0
    speed: float = 18.0  # cm/s while walking
    wander: bool = True
    walking: bool = True
    timer: float = 0.0


PwmModel = Literal["shared", "split", "ideal"]

# Wheel index sets, in the order of ``Wheels``: FL, RL, FR, RR.
_ALL = (0, 1, 2, 3)
_LEFT = (0, 1)
_RIGHT = (2, 3)


@dataclass
class Physics:
    """The simulated car's real speeds at 100 % (the server only has its calibration)."""

    forward_cm_per_s: float = 30.0
    strafe_cm_per_s: float = 20.0
    turn_deg_per_s: float = 180.0
    noise: float = 0.03  # relative speed error, drawn once per move
    echo_noise_cm: float = 0.8  # ultrasonic reading error, uniform within ±this
    # How the UNO drives its two PWM pins (firmware/scout_uno/src/core/config.h):
    # "split" (SCOUT_PWM_SPLIT 1, the default build): each pin drives one side at that
    # side's fastest wheel. "shared" (SCOUT_PWM_SPLIT 0): both pins carry the fastest
    # wheel's duty. In both, a wheel below 1/coast_divisor of its pin's fastest wheel
    # coasts. "ideal": every wheel runs at its own mixed speed, as if each had its own pin.
    pwm_model: PwmModel = "split"
    coast_divisor: int = 4  # kCoastDivisor: 4 = 25 %
    pwm5_left: bool = False  # the AD176's wiring: D5 drives the right wheels, D6 the left ones
    # The motors, as measured on the AD176: speed is proportional to the PWM duty above
    # zero_speed_pwm, and from a standstill a motor only starts at start_pwm or more
    # (below that it hums and stays put).
    zero_speed_pwm: float = 25.0
    start_pwm: int = 100
    car_radius: float = 12.0  # for collisions
    sensor_offset: float = 10.0  # ultrasonic and camera, ahead of the turning centre


def mix(vx: int, vy: int, wz: int) -> Wheels:
    """The UNO's ``mixWheels``: integer percent per wheel (FL, RL, FR, RR). If any wheel
    would exceed 100 %, all four are scaled by 100/peak, truncating toward zero like the
    firmware's integer division."""
    raw = (vx + vy + wz, vx - vy + wz, vx - vy - wz, vx + vy - wz)
    peak = max(abs(r) for r in raw)

    def scaled(r: int) -> int:
        return r if peak <= 100 else (abs(r) * 100 // peak) * (1 if r >= 0 else -1)

    return float(scaled(raw[0])), float(scaled(raw[1])), float(scaled(raw[2])), float(scaled(raw[3]))


def drive_wheels(vx: int, vy: int, wz: int, physics: Physics) -> Wheels:
    """The percentage each wheel is driven at for a MOVE: the UNO's ``mecanumOutput``.

    Each pin runs at the duty of the fastest wheel it drives, so every driven wheel on it
    turns at that speed. A wheel whose share is below a quarter of that coasts; exactly a
    quarter is still driven. ``move_duties`` turns the percentages into PWM duties.
    """
    speeds = mix(vx, vy, wz)
    if physics.pwm_model == "ideal":
        return speeds
    pins: tuple[tuple[int, ...], tuple[int, ...]]
    if physics.pwm_model == "shared":
        pins = (_ALL, _ALL)
    else:
        pins = (_LEFT, _RIGHT) if physics.pwm5_left else (_RIGHT, _LEFT)
    peak5 = max(abs(speeds[i]) for i in pins[0])
    peak6 = max(abs(speeds[i]) for i in pins[1])
    out = [0.0, 0.0, 0.0, 0.0]
    for i, v in enumerate(speeds):
        reference = peak5 if i in pins[0] else peak6 if i in pins[1] else 0.0
        m = abs(v)
        if m == 0 or reference == 0 or m * physics.coast_divisor < reference:
            continue  # coasts
        out[i] = math.copysign(reference, v)
    return out[0], out[1], out[2], out[3]


def wheel_bits(wheels: Wheels) -> int:
    """The 74HC595 byte for the wheel directions (protocol/uart.md bit table)."""
    fl, rl, fr, rr = wheels
    bits = 0
    bits |= (1 << 7) if fl > 0 else (1 << 6) if fl < 0 else 0
    bits |= (1 << 5) if rl > 0 else (1 << 4) if rl < 0 else 0
    bits |= (1 << 1) if fr > 0 else (1 << 2) if fr < 0 else 0
    bits |= (1 << 0) if rr > 0 else (1 << 3) if rr < 0 else 0
    return bits


def percent_to_pwm(percent: float, min_pwm: int) -> int:
    """The UNO's ``percentToPwm``: 0 stays 0, 1..100 % map linearly onto min_pwm..255."""
    p = abs(round(percent))
    if p == 0:
        return 0
    if p >= 100:
        return 255
    return min_pwm + ((p - 1) * (255 - min_pwm) + 49) // 99


def move_duties(wheels: Wheels, min_pwm: int) -> Wheels:
    """Signed PWM duty per wheel for the percentages from ``drive_wheels``."""
    fl, rl, fr, rr = (math.copysign(percent_to_pwm(w, min_pwm), w) if w else 0.0 for w in wheels)
    return fl, rl, fr, rr


def bits_duties(bits: int, pwm5: int, pwm6: int, *, pwm5_left: bool = False) -> Wheels:
    """Signed PWM duty per wheel for a raw TEST. ``pwm5_left`` says which wheels each pin
    drives; on the real car that mapping is what TEST exists to find out."""
    left, right = (pwm5, pwm6) if pwm5_left else (pwm6, pwm5)

    def one(plus: int, minus: int, pwm: int) -> float:
        d = (1 if bits >> plus & 1 else 0) - (1 if bits >> minus & 1 else 0)
        return float(d * pwm)

    return one(7, 6, left), one(5, 4, left), one(1, 2, right), one(0, 3, right)


def motor_speeds(duties: Wheels, physics: Physics) -> Wheels:
    """Each wheel's speed in percent of its full speed, for signed PWM duties.

    Proportional to the duty above ``zero_speed_pwm``; a duty below ``start_pwm`` leaves
    the wheel still, because every move starts from a standstill.
    """

    def one(duty: float) -> float:
        m = abs(duty)
        if m < physics.start_pwm or m <= physics.zero_speed_pwm:
            return 0.0
        return math.copysign((m - physics.zero_speed_pwm) / (255 - physics.zero_speed_pwm) * 100, duty)

    fl, rl, fr, rr = (one(d) for d in duties)
    return fl, rl, fr, rr


def body_velocity(wheels: Wheels) -> tuple[float, float, float]:
    """Mecanum forward kinematics: (vx, vy, wz) in percent from (FL, RL, FR, RR)."""
    fl, rl, fr, rr = wheels
    return (fl + fr + rl + rr) / 4, (fl - fr - rl + rr) / 4, (fl - fr + rl - rr) / 4


@dataclass(frozen=True)
class Scene:
    """An immutable copy of what the camera and sensors need, safe to use in a thread."""

    width: float
    depth: float
    wall_height: float
    boxes: tuple[Box, ...]
    discs: tuple[Disc, ...]
    door: Door | None
    car: Pose
    cat: Cat | None


WALL_COLORS = ((205, 200, 185), (180, 195, 210), (190, 205, 180), (210, 195, 205))


def default_furniture(width: float, depth: float) -> tuple[tuple[Box, ...], tuple[Disc, ...], Door]:
    boxes = (
        Box(width - 90, depth - 60, width - 10, depth - 10, 45, (70, 90, 160), "sofa"),
        Box(20, depth - 50, 80, depth - 10, 90, (90, 140, 90), "cabinet"),
        Box(width / 2 - 20, 30, width / 2 + 20, 70, 35, (150, 150, 160), "box"),
    )
    discs = (Disc(width - 50, 60, 14, 40, (100, 120, 110), "plant pot"),)
    door = Door(wall=0, u0=depth / 2 - 40, u1=depth / 2 + 40)
    return boxes, discs, door


@dataclass
class World:
    width: float = 400.0
    depth: float = 300.0
    wall_height: float = 250.0
    physics: Physics = field(default_factory=Physics)
    boxes: tuple[Box, ...] = ()
    discs: tuple[Disc, ...] = ()
    door: Door | None = None
    car: Pose = field(default_factory=lambda: Pose(110.0, 150.0, 0.0))
    cat: Cat | None = None
    seed: int | None = None

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        self._move_factor = 1.0

    @classmethod
    def default(cls, *, seed: int | None = None, cat: bool = True, physics: Physics | None = None) -> World:
        world = cls(seed=seed, physics=physics or Physics())
        world.boxes, world.discs, world.door = default_furniture(world.width, world.depth)
        if cat:
            world.cat = Cat(x=290.0, y=170.0, heading=math.pi)
        return world

    @classmethod
    def empty(
        cls,
        *,
        width: float = 400.0,
        depth: float = 300.0,
        physics: Physics | None = None,
        seed: int | None = None,
    ) -> World:
        """Just the four walls, for tests that need exact geometry."""
        return cls(width=width, depth=depth, physics=physics or Physics(), seed=seed)

    def scene(self) -> Scene:
        cat = replace(self.cat) if self.cat is not None else None
        return Scene(
            self.width,
            self.depth,
            self.wall_height,
            self.boxes,
            self.discs,
            self.door,
            replace(self.car),
            cat,
        )

    # -- motion ----------------------------------------------------------------------

    def new_move(self) -> None:
        """Draw this move's speed error (called when a move starts)."""
        self._move_factor = max(0.5, 1.0 + self.rng.gauss(0.0, self.physics.noise))

    def step_car(self, wheels: Wheels, dt: float) -> bool:
        """Move the car for ``dt`` seconds. Returns False if it was blocked."""
        if dt <= 0 or wheels == ZERO:
            return True
        p = self.physics
        vx, vy, wz = body_velocity(wheels)
        k = self._move_factor
        fwd = vx / 100 * p.forward_cm_per_s * k * dt
        right = vy / 100 * p.strafe_cm_per_s * k * dt
        turn = math.radians(wz / 100 * p.turn_deg_per_s * k * dt)
        h = self.car.heading
        mid = h - turn / 2  # integrate along the arc's mid heading
        dx = fwd * math.cos(mid) + right * math.sin(mid)
        dy = fwd * math.sin(mid) - right * math.cos(mid)
        self.car.heading = (h - turn) % (2 * math.pi)
        nx, ny = self.car.x + dx, self.car.y + dy
        if self.collides(nx, ny, p.car_radius, ignore_car=True):
            return False
        self.car.x, self.car.y = nx, ny
        return True

    def collides(
        self, x: float, y: float, r: float, *, ignore_car: bool = False, ignore_cat: bool = False
    ) -> bool:
        if x - r < 0 or y - r < 0 or x + r > self.width or y + r > self.depth:
            return True
        for b in self.boxes:
            cx = min(max(x, b.x0), b.x1)
            cy = min(max(y, b.y0), b.y1)
            if (x - cx) ** 2 + (y - cy) ** 2 < r * r:
                return True
        for d in self.discs:
            if (x - d.x) ** 2 + (y - d.y) ** 2 < (r + d.r) ** 2:
                return True
        if (
            not ignore_cat
            and self.cat is not None
            and (x - self.cat.x) ** 2 + (y - self.cat.y) ** 2 < (r + self.cat.r) ** 2
        ):
            return True
        return (
            not ignore_car
            and (x - self.car.x) ** 2 + (y - self.car.y) ** 2 < (r + self.physics.car_radius) ** 2
        )

    def step_cat(self, dt: float) -> None:
        cat = self.cat
        if cat is None or not cat.wander or dt <= 0:
            return
        cat.timer -= dt
        if cat.timer <= 0:
            cat.walking = self.rng.random() < 0.6
            cat.timer = self.rng.uniform(1.5, 5.0)
            if cat.walking:
                cat.heading += self.rng.uniform(-1.2, 1.2)
        if not cat.walking:
            return
        cat.heading += self.rng.gauss(0.0, 0.6) * dt
        nx = cat.x + math.cos(cat.heading) * cat.speed * dt
        ny = cat.y + math.sin(cat.heading) * cat.speed * dt
        if self.collides(nx, ny, cat.r + 4, ignore_cat=True):
            cat.heading += math.pi * self.rng.uniform(0.5, 1.5)
            return
        cat.x, cat.y = nx, ny

    # -- sensors ---------------------------------------------------------------------

    def ultrasonic(self, *, noise: bool = True) -> int | None:
        """Front distance in cm like an HC-SR04: the nearest echo in a ±7.5° cone,
        ``None`` beyond 400 cm, with up to ``Physics.echo_noise_cm`` of error."""
        p = self.car
        r = self.physics.sensor_offset
        ox = p.x + math.cos(p.heading) * r
        oy = p.y + math.sin(p.heading) * r
        angles = p.heading + np.radians(np.linspace(-7.5, 7.5, 7))
        dist, _kind, _idx, _u = cast_rays(self.scene(), ox, oy, angles, include_cat=True)
        d = float(dist.min())
        if not math.isfinite(d) or d > 400:
            return None
        if noise and self.physics.echo_noise_cm > 0:
            e = self.physics.echo_noise_cm
            d += self.rng.uniform(-e, e)
        return max(2, round(d))

    def line_sensors(self) -> list[int]:
        """Raw line-sensor readings over a plain floor."""
        return [
            int(560 + self.rng.uniform(-12, 12)),
            int(600 + self.rng.uniform(-12, 12)),
            int(540 + self.rng.uniform(-12, 12)),
        ]

    def place_car(self, x: float, y: float, heading_deg: float) -> None:
        self.car = Pose(x, y, math.radians(heading_deg) % (2 * math.pi))


# Ray hit kinds.
WALL, BOX, DISC, CAT = 0, 1, 2, 3


def ray_walls(
    width: float, depth: float, ox: float, oy: float, dx: np.ndarray, dy: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Distance to the room walls from inside: (t, side, u). Sides: 0 x=0, 1 x=width,
    2 y=0, 3 y=depth; ``u`` is the position along that wall."""
    with np.errstate(divide="ignore", invalid="ignore"):
        tx = np.where(dx > 0, (width - ox) / dx, np.where(dx < 0, -ox / dx, np.inf))
        ty = np.where(dy > 0, (depth - oy) / dy, np.where(dy < 0, -oy / dy, np.inf))
    use_x = tx <= ty
    t = np.where(use_x, tx, ty)
    side = np.where(use_x, np.where(dx > 0, 1, 0), np.where(dy > 0, 3, 2))
    u = np.where(use_x, oy + t * dy, ox + t * dx)
    return t, side, u


def ray_box(b: Box, ox: float, oy: float, dx: np.ndarray, dy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Distance to a box from outside (inf for a miss), and whether an x face was hit."""
    with np.errstate(divide="ignore", invalid="ignore"):
        t1 = (b.x0 - ox) / dx
        t2 = (b.x1 - ox) / dx
        t3 = (b.y0 - oy) / dy
        t4 = (b.y1 - oy) / dy
        nx = np.minimum(t1, t2)
        ny = np.minimum(t3, t4)
        near = np.maximum(nx, ny)
        far = np.minimum(np.maximum(t1, t2), np.maximum(t3, t4))
        hit = (far >= near) & (near > 0)
    return np.where(hit, near, np.inf), nx >= ny


def ray_disc(
    cx: float, cy: float, r: float, ox: float, oy: float, dx: np.ndarray, dy: np.ndarray
) -> np.ndarray:
    """Distance to a disc from outside (inf for a miss)."""
    px, py = cx - ox, cy - oy
    b = px * dx + py * dy
    disc = b * b - (px * px + py * py - r * r)
    with np.errstate(invalid="ignore"):
        t = b - np.sqrt(disc)
    return np.where((disc >= 0) & (t > 0), t, np.inf)


def cast_rays(
    scene: Scene, ox: float, oy: float, angles: np.ndarray, *, include_cat: bool = False
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Nearest hit along each ray: (distance, kind, index, u).

    ``index`` is the wall side (0..3) or the object's index; ``u`` is the position along
    a wall (meaningful for wall hits only).
    """
    dx = np.cos(angles)
    dy = np.sin(angles)
    dist, side, u = ray_walls(scene.width, scene.depth, ox, oy, dx, dy)
    kind = np.full(angles.shape[0], WALL)
    index = side.astype(int)

    def take(t: np.ndarray, k: int, i: int) -> None:
        nonlocal dist, kind, index
        better = t < dist
        dist = np.where(better, t, dist)
        kind = np.where(better, k, kind)
        index = np.where(better, i, index)

    for i, b in enumerate(scene.boxes):
        take(ray_box(b, ox, oy, dx, dy)[0], BOX, i)
    for i, d in enumerate(scene.discs):
        take(ray_disc(d.x, d.y, d.r, ox, oy, dx, dy), DISC, i)
    if include_cat and scene.cat is not None:
        take(ray_disc(scene.cat.x, scene.cat.y, scene.cat.r, ox, oy, dx, dy), CAT, 0)
    return dist, kind, index, u
