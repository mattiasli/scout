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
    # motion estimates: "split" one pin per side (the default build, as the AD176 is
    # wired), "shared" both pins at one duty, "ideal" every wheel at its own speed. Pure
    # moves are estimated the same under all three.
    pwm_layout: PwmLayout = "split"

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
    # Frames in a row without the target, all taken at a standstill, before it searches.
    follow_misses: int = 2
    follow_search_deg: float = 360.0  # how far it turns looking before it gives up
    follow_frame_timeout_s: float = 5.0  # no picture from the camera for this long ends it
    follow_match_min: float = 0.45  # how alike a candidate must look to be the target
    # How many times bigger or smaller than its last sighting a candidate may be and still
    # be the target (tracker.size_change). The allowance is multiplied by follow_size_change_growth for each
    # second it has been out of sight, up to follow_size_change_max. On the floor, a dark chair 4 times
    # narrower than the person followed matched their dark clothes at 0.95.
    follow_size_change: float = 2.5
    follow_size_change_growth: float = 1.1
    follow_size_change_max: float = 4.0
    # Sure sightings in a row (not unsure, matching at least LEARN_MATCH) before follow
    # updates the target's look from them, so one wrong pick doesn't teach it a new look.
    follow_learn_after: int = 3
    # Follow drives only while it's sure the one it sees is the one it follows. After it
    # has lost it and found one again, or when the detector is less than follow_score_drop
    # times as sure of it as usual (a coat on a hook taken for a person), it only turns and
    # tilts until follow_confirm_frames sure sightings in a row, or the AI confirms with a
    # pick. follow_score_rate is how fast "as usual" follows the detector's confidence. Close
    # up, with the target cut off at an edge of the picture, the detector is less sure of it,
    # so that alone is no reason to hold back, unless follow has just found it again.
    follow_score_drop: float = 0.7
    follow_score_rate: float = 0.2
    follow_confirm_frames: int = 2
    # Once another one in view has looked about as alike as the target (matching at least
    # LEARN_MATCH and no more than follow_lookalike_margin less than the target does), with
    # the detector at least follow_lookalike_score times as sure of it as it usually is of
    # the target, finding the target again after losing it needs a pick. A box mostly
    # inside the target's (its legs, boxed as well) doesn't count. On the floor, a coat
    # rack hung with dark clothes scored at most about 0.7 times the person followed.
    follow_lookalike_margin: float = 0.15
    follow_lookalike_score: float = 0.85
    follow_start_wait_s: float = 4.0  # follow returns once it has locked on, or after this
    follow_pick_valid_s: float = 120.0  # how long find's or follow_view's numbers stay valid
    find_min_score: float = 0.35
    detector_linger_s: float = 120.0  # the model stays loaded this long after its last use
    # After each step, how long to wait once the move has ended before looking again: the
    # car coasting to a stop, plus the frame's way from the camera to the server.
    follow_settle_s: float = 0.3
    follow_min_score: float = 0.35
    # The camera's horizontal field of view, for turning a box's offset into an angle.
    # Measured on the car: a person of known height at 200 cm spans 65.5° top to bottom,
    # which makes 81° across for the 4:3 picture (docs/hardware.md, "Camera").
    camera_hfov_deg: float = 81.0
    # Real camera angle per degree of tilt asked for. Measured with the same person: tilts
    # of 0, 20 and 40 put the camera at about -1°, 17° and 34°.
    camera_tilt_scale: float = 0.88
