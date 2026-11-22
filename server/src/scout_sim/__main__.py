"""``scout-sim``: run a simulated Scout car against a server.

    uv run scout-sim --url ws://127.0.0.1:8000/car/ws --key KEY

See docs/simulator.md.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import math
import os
import sys
from dataclasses import replace
from pathlib import Path

from scout_sim.car import Faults, SimCar, SimConfig
from scout_sim.render import RESOLUTIONS, snapshot
from scout_sim.world import Physics, World


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scout-sim",
        description="A simulated Scout car: a mecanum car in a small room with a cat, speaking the car link.",
    )
    p.add_argument(
        "--url",
        default=os.environ.get("SCOUT_SIM_URL", "ws://127.0.0.1:8000/car/ws"),
        help="the server's car link (default: %(default)s, or $SCOUT_SIM_URL)",
    )
    p.add_argument(
        "--key", default=os.environ.get("SCOUT_CAR_KEY"), help="the car's secret key (or $SCOUT_CAR_KEY)"
    )
    p.add_argument("--seed", type=int, help="random seed for the room, the cat and the noise")
    p.add_argument("--fw", default="0.1.0", help="firmware version to report (default: %(default)s)")
    p.add_argument("--camera", default="OV3660", help="camera sensor to report (default: %(default)s)")
    p.add_argument("--max-res", default="qxga", choices=list(RESOLUTIONS), help="largest camera resolution")
    p.add_argument("--no-cat", action="store_true", help="leave the cat out")
    p.add_argument("--still-cat", action="store_true", help="the cat sits still")
    p.add_argument("--door-open", action="store_true", help="the door starts open")
    p.add_argument(
        "--pwm",
        default="split",
        choices=["split", "shared", "ideal"],
        help="how the motor board drives its two PWM pins: split (one pin per side, the default "
        "firmware build), shared (both pins at one duty) or ideal (every wheel at its own speed); "
        "default: %(default)s",
    )
    p.add_argument(
        "--render", metavar="FILE", help="write one camera frame from the start pose as a JPEG and exit"
    )
    p.add_argument("--res", default="vga", choices=list(RESOLUTIONS), help="resolution for --render")
    p.add_argument("-v", "--verbose", action="store_true", help="log more")
    return p


def make_world(args: argparse.Namespace) -> World:
    physics = Physics(pwm_model=args.pwm)
    world = World.default(seed=args.seed, cat=not args.no_cat, physics=physics)
    if world.cat is not None and args.still_cat:
        world.cat.wander = False
    if world.door is not None and args.door_open:
        world.door = replace(world.door, open=True)
    return world


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    world = make_world(args)
    if args.render:
        Path(args.render).write_bytes(snapshot(world.scene(), args.res, 10))
        pose = world.car
        heading = math.degrees(pose.heading)
        print(f"wrote {args.render} ({args.res}) from x={pose.x:.0f} y={pose.y:.0f} heading={heading:.0f}°")
        return 0
    if not args.key:
        print("scout-sim: a car key is needed: --key KEY or SCOUT_CAR_KEY", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = SimConfig(
        url=args.url, key=args.key, fw=args.fw, camera=args.camera, max_res=args.max_res, seed=args.seed
    )
    car = SimCar(config, world=world, faults=Faults())
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(car.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
