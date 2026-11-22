"""The simulated UNO: the motor rules from protocol/uart.md.

* Every MOVE and TEST is time-bounded and ends by itself after ``ms``.
* A new MOVE or TEST replaces the running one (``replaced``).
* STOP ends the running one at once (``stopped``).
* A MOVE with ``vx > 0`` doesn't start, or stops, while the front distance is below
  ``obst_cm`` (``obstacle``). Nothing else checks the distance; TEST never does.
* While anything runs, ``link_ms`` without a frame from the host stops it (``link``).

The host side (the simulated ESP32) calls ``host_frame`` whenever it would have sent a
UART frame, including the PING it sends every 250 ms.
"""

from __future__ import annotations

from dataclasses import dataclass

from scout_sim.world import ZERO, Wheels, World, bits_duties, drive_wheels, motor_speeds, move_duties

# firmware/scout_uno/src/core/config.h: kTiltMinDeg, kTiltMaxDeg.
TILT_MIN = -40
TILT_MAX = 40


@dataclass
class Run:
    request_id: int  # the car-link request id it answers to
    kind: str  # "move" or "test"
    vx: int
    ms: int
    duties: Wheels  # signed PWM duty per wheel
    started: float
    cm_start: int | None


@dataclass(frozen=True)
class Done:
    request_id: int
    kind: str
    reason: str  # time, obstacle, stopped, link, replaced
    cm_start: int | None
    cm_end: int | None
    ms: int


class SimUno:
    fw = "0.1.0"

    def __init__(self, world: World, now: float) -> None:
        self.world = world
        self.obst_cm = 15
        self.min_pwm = 90
        self.link_ms = 1000
        self.tilt = 0
        self.run: Run | None = None
        self.booted = now
        self.last_host = now
        self._last_tick = now

    @property
    def moving(self) -> bool:
        return self.run is not None

    @property
    def duties(self) -> Wheels:
        return self.run.duties if self.run is not None else ZERO

    def host_frame(self, now: float) -> None:
        self.last_host = now

    def set_tilt(self, deg: int) -> int:
        self.tilt = max(TILT_MIN, min(TILT_MAX, deg))
        return self.tilt

    def _end(self, reason: str, now: float, cm_end: int | None = None) -> Done:
        """End the run. ``cm_end`` is the reading that ended it, if one did: the UNO
        reports the same held reading it has just checked."""
        run = self.run
        assert run is not None
        self.run = None
        ran = run.ms if reason == "time" else min(run.ms, round((now - run.started) * 1000))
        cm = cm_end if cm_end is not None else self.world.ultrasonic()
        return Done(run.request_id, run.kind, reason, run.cm_start, cm, max(0, ran))

    def _start(self, run: Run, now: float) -> list[Done]:
        out = [self._end("replaced", now)] if self.run is not None else []
        if run.kind == "move" and run.vx > 0 and run.cm_start is not None and run.cm_start < self.obst_cm:
            out.append(Done(run.request_id, run.kind, "obstacle", run.cm_start, run.cm_start, 0))
            return out
        self.world.new_move()
        self.run = run
        self._last_tick = now
        return out

    def move(self, request_id: int, vx: int, vy: int, wz: int, ms: int, now: float) -> list[Done]:
        self.host_frame(now)
        duties = move_duties(drive_wheels(vx, vy, wz, self.world.physics), self.min_pwm)
        return self._start(Run(request_id, "move", vx, ms, duties, now, self.world.ultrasonic()), now)

    def test(self, request_id: int, bits: int, pwm5: int, pwm6: int, ms: int, now: float) -> list[Done]:
        self.host_frame(now)
        duties = bits_duties(bits, pwm5, pwm6, pwm5_left=self.world.physics.pwm5_left)
        return self._start(Run(request_id, "test", 0, ms, duties, now, self.world.ultrasonic()), now)

    def stop(self, now: float) -> Done | None:
        self.host_frame(now)
        return self._end("stopped", now) if self.run is not None else None

    def halt(self) -> None:
        """Motors off without a report (the host is gone, nobody to tell)."""
        self.run = None

    def tick(self, now: float) -> list[Done]:
        """Advance the motors to ``now``; returns the run that ended, if any."""
        dt = now - self._last_tick
        self._last_tick = now
        run = self.run
        if run is None:
            return []
        end = run.started + run.ms / 1000
        speeds = motor_speeds(run.duties, self.world.physics)
        self.world.step_car(speeds, min(dt, max(0.0, end - (now - dt))))
        if now >= end:
            return [self._end("time", now)]
        if run.kind == "move" and run.vx > 0:
            cm = self.world.ultrasonic()
            if cm is not None and cm < self.obst_cm:
                return [self._end("obstacle", now, cm)]
        if (now - self.last_host) * 1000 > self.link_ms:
            return [self._end("link", now)]
        return []
