"""Robot side of the Scout server: the car link, tool actions and behaviours.

The app side uses ``Robot`` through ``scout.contracts.RobotAPI``.
"""

from scout.robot.config import RobotConfig
from scout.robot.detector import Detection, Detector, DetectorFactory, DetectorUnavailable, YoloxDetector
from scout.robot.errors import InvalidRequest
from scout.robot.robot import Robot

__all__ = [
    "Detection",
    "Detector",
    "DetectorFactory",
    "DetectorUnavailable",
    "InvalidRequest",
    "Robot",
    "RobotConfig",
    "YoloxDetector",
]
