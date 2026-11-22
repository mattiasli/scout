"""Simulated Scout car for tests and for working without hardware."""

from scout_sim.car import Faults, SimCar, SimConfig
from scout_sim.detector import SimCatDetector
from scout_sim.world import Box, Cat, Disc, Door, Physics, World

__all__ = [
    "Box",
    "Cat",
    "Disc",
    "Door",
    "Faults",
    "Physics",
    "SimCar",
    "SimCatDetector",
    "SimConfig",
    "World",
]
