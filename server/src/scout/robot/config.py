"""Timings and tuning knobs for the robot side.

The defaults suit the real car and protocol/car-link.md. Tests shrink the timings so the
suite stays fast; nothing else should need to change them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from scout.robot.motion import PwmLayout

OrbitMode = Literal["split", "blend"]


@dataclass
class RobotConfig:
    # Car link (protocol/car-link.md, "Timing and safety").
    ping_interval_s: float = 5.0
    silence_timeout_s: float = 15.0
    hello_timeout_s: float = 10.0
    max_text_bytes: int = 64 * 1024
    max_image_bytes: int = 8 * 1024 * 1024

    # Waiting for replies.
    ack_timeout_s: float = 3.0
    # Extra wait for ``done`` on top of the move time and the car's own 1.5 s UNO timeout.
    done_margin_s: float = 3.0
    snap_timeout_s: float = 10.0
    capture_timeout_s: float = 30.0
    # How long a command waits for the command before it to finish.
    command_wait_s: float = 30.0
    # How long a behaviour may take to wind down after it was interrupted.
    interrupt_wait_s: float = 5.0
    ota_progress_timeout_s: float = 180.0

    # Pictures.
    look_quality: int = 12
    capture_quality: int = 10
    composite_max_width: int = 1280
    composite_quality: int = 80
    preview_width: int = 480

    # How the UNO build drives its two PWM pins (SCOUT_PWM_SPLIT in the firmware), for the
    # motion estimates: "shared" is the default build, "split" one pin per side, "ideal"
    # every wheel at its own speed. Pure moves are estimated the same under all three.
    pwm_layout: PwmLayout = "shared"

    # Look-around.
    look_around_speed: int = 50
    settle_s: float = 0.15

    # Capture orbit. "split" uses only pure moves (rotate half a step, strafe the chord,
    # rotate the other half), which the two-PWM-pin shield drives exactly. "blend" strafes
    # and rotates in one move, a true arc, but needs per-wheel speed control.
    orbit_mode: OrbitMode = "split"
    orbit_speed: int = 40
    orbit_settle_s: float = 0.4
    orbit_min_cm: int = 20
    orbit_max_cm: int = 150
    orbit_correct_radius: bool = True
    # The ultrasonic sensor sits this far ahead of the point the car turns about, so the
    # orbit radius about that point is the front distance plus this.
    sensor_offset_cm: float = 10.0

    # Follow.
    follow_fps: int = 5
    follow_quality: int = 12
    follow_stream_secs: int = 10
    follow_stream_resend_s: float = 6.0
    follow_lost_s: float = 3.0
    follow_move_ms: int = 350
    follow_min_score: float = 0.35
