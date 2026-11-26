#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5"]
# ///
"""Bench tool for the Scout UNO firmware, over the UNO's USB serial port.

    uv run tools/bench.py --port COM8 ping
    uv run tools/bench.py --port COM8 monitor
    uv run tools/bench.py --port COM8 move forward 40 800
    uv run tools/bench.py --port COM8 wheels

Unplug the cable to the ESP32-CAM first: the UNO's serial pins are shared between USB and
that link. Opening the port resets the UNO, so settings from an earlier run are gone; use
--conf KEY=VALUE to set them for this run. Ctrl+C always sends STOP before the tool exits.
The protocol is protocol/uart.md.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scoutlink
from scoutlink import Frame

BAUD = 115200
READ_TIMEOUT = 0.02  # seconds one serial read may wait
PING_INTERVAL = 0.25  # keep-alive while a command runs; the UNO's watchdog is 1 s by default
BOOT_TIMEOUT = 4.0  # the bootloader waits about 1 s before the sketch starts
REPLY_TIMEOUT = 1.0
DONE_GRACE = 1.5  # wait this much longer than a move's ms for its DONE (the ESP32 does too)
DEFAULT_OBST_CM = 15

GUIDE_PWM = 150  # a moderate duty for the guided wheel tests
GUIDE_MS = 1500

# Unit (vx, vy, wz) for each named direction: vx forward, vy right, wz clockwise.
DIRECTIONS: dict[str, tuple[int, int, int]] = {
    "forward": (1, 0, 0),
    "back": (-1, 0, 0),
    "left": (0, -1, 0),
    "right": (0, 1, 0),
    "forward_left": (1, -1, 0),
    "forward_right": (1, 1, 0),
    "back_left": (-1, -1, 0),
    "back_right": (-1, 1, 0),
    "cw": (0, 0, 1),
    "ccw": (0, 0, -1),
}


@dataclass(frozen=True)
class Wheel:
    key: str  # what the operator types
    motor: str
    name: str
    forward_bit: int  # 74HC595 value that drives it forward
    back_bit: int


# protocol/uart.md: bit 7 M1+, 6 M1-, 5 M2+, 4 M2-, 3 M4-, 2 M3-, 1 M3+, 0 M4+.
WHEELS = (
    Wheel("fl", "M1", "front-left", 0x80, 0x40),
    Wheel("rl", "M2", "rear-left", 0x20, 0x10),
    Wheel("fr", "M3", "front-right", 0x02, 0x04),
    Wheel("rr", "M4", "rear-right", 0x01, 0x08),
)
WHEEL_BY_KEY = {w.key: w for w in WHEELS}
ALL_WHEELS = frozenset(WHEEL_BY_KEY)
LEFT_WHEELS = frozenset({"fl", "rl"})
RIGHT_WHEELS = frozenset({"fr", "rr"})


def move_args(direction: str, speed: int) -> tuple[int, int, int]:
    """(vx, vy, wz) for a named direction where `speed` is the wheel speed in percent.

    A diagonal adds two components on the same wheels, so each gets half the speed.
    """
    ux, uy, uz = DIRECTIONS[direction]
    part = (speed + 1) // 2 if ux and uy else speed
    return ux * part, uy * part, uz * part


def wheel_speeds(vx: int, vy: int, wz: int) -> dict[str, int]:
    """The firmware's mecanum mix (protocol/uart.md), scaled so no wheel exceeds 100 %."""
    raw = {"fl": vx + vy + wz, "rl": vx - vy + wz, "fr": vx - vy - wz, "rr": vx + vy - wz}
    peak = max(abs(v) for v in raw.values())
    if peak > 100:
        raw = {k: int(v * 100 / peak) for k, v in raw.items()}
    return raw


def expected_bits(vx: int, vy: int, wz: int) -> int:
    """The 74HC595 byte the firmware drives for a MOVE, in its shared-PWM mode."""
    speeds = wheel_speeds(vx, vy, wz)
    peak = max(abs(v) for v in speeds.values())
    bits = 0
    for key, speed in speeds.items():
        if speed == 0 or abs(speed) * 4 < peak:
            continue  # below 25 % of the fastest wheel: coasts
        wheel = WHEEL_BY_KEY[key]
        bits |= wheel.forward_bit if speed > 0 else wheel.back_bit
    return bits


def bit_line(value: int) -> str:
    """'M1+' etc. for a single-bit 74HC595 value."""
    for wheel in WHEELS:
        if value == wheel.forward_bit:
            return wheel.motor + "+"
        if value == wheel.back_bit:
            return wheel.motor + "-"
    return "?"


# ---------------------------------------------------------------- printing


def describe_reset(flags: int) -> str:
    names = [
        name
        for bit, name in ((0, "power-on"), (1, "reset pin"), (2, "brown-out"), (3, "watchdog"))
        if flags & (1 << bit)
    ]
    return ", ".join(names) if names else "none reported; the stock bootloader clears them"


LASER_CODES = {"-1": "none", "-2": "not found", "-3": "not a VL53L1X"}


def format_tel(frame: Frame) -> str:
    if len(frame.args) not in (7, 8):  # 7 from UNO firmware without the laser sensor
        return f"TEL   (unexpected) {','.join(frame.args)}"
    cm, left, center, right, tilt, move, rx_errors = frame.args[:7]
    distance = "none" if cm == "-1" else f"{cm} cm"
    running = f"seq {move}" if move != "0" else "-"
    text = (
        f"TEL   front {distance:>7}   line A0/A1/A2 {left:>4} {center:>4} {right:>4}   "
        f"tilt {tilt:>3}   moving {running}   rx errors {rx_errors}"
    )
    if len(frame.args) == 8:
        laser = frame.args[7]
        text += f"   laser {LASER_CODES.get(laser, laser + ' cm')}"
    return text


def format_done(frame: Frame) -> str:
    if len(frame.args) != 4:
        return f"DONE  seq {frame.seq} (unexpected) {','.join(frame.args)}"
    reason, cm_start, cm_end, ran = frame.args
    return f"DONE  seq {frame.seq} {reason:<4}  front {cm_start} -> {cm_end} cm  ran {ran} ms"


def format_boot(frame: Frame) -> str:
    fw = frame.args[0] if frame.args else "?"
    try:
        flags = int(frame.args[1])
        detail = f"{flags} ({describe_reset(flags)})"
    except (IndexError, ValueError):
        detail = "?"
    return f"BOOT  firmware {fw}, reset flags {detail}"


# ---------------------------------------------------------------- the link


class SerialPort(Protocol):
    @property
    def in_waiting(self) -> int: ...

    def read(self, size: int = 1) -> bytes: ...

    def write(self, data: bytes) -> int | None: ...

    def close(self) -> None: ...


class LinkError(Exception):
    pass


@dataclass
class MotionResult:
    reply: Frame  # ACK or ERR
    done: Frame | None  # DONE, or None if the request was rejected

    @property
    def reason(self) -> str | None:
        return self.done.args[0] if self.done and self.done.args else None

    @property
    def ran_ms(self) -> int:
        return int(self.done.args[3]) if self.done and len(self.done.args) == 4 else 0


class Link:
    """Frames over a serial port: request/reply matching by seq, events and keep-alive."""

    def __init__(
        self,
        port: SerialPort,
        *,
        clock: Callable[[], float] = time.monotonic,
        out: Callable[[str], None] = print,
    ) -> None:
        self.port = port
        self.clock = clock
        self.out = out
        self.parser = scoutlink.Parser()
        self.seq = 0
        self.replies: dict[int, Frame] = {}
        self.done: dict[int, Frame] = {}
        self.boots: list[Frame] = []
        self.telemetry: Frame | None = None
        self.show_telemetry = False
        self.keepalive = False  # PING every PING_INTERVAL while polling
        self._last_ping = float("-inf")
        self._keepalive_seqs: set[int] = set()

    def next_seq(self) -> int:
        """Hosts number requests 1..65535 and wrap back to 1."""
        self.seq = self.seq % 65535 + 1
        return self.seq

    def send(self, type_: str, *args: int | str) -> int:
        seq = self.next_seq()
        self.port.write(scoutlink.encode(type_, seq, *args))
        return seq

    def poll(self) -> None:
        """Sends a keep-alive PING when due, then reads and handles whatever has arrived."""
        if self.keepalive and self.clock() - self._last_ping >= PING_INTERVAL:
            self._keepalive_seqs.add(self.send("PING"))
            self._last_ping = self.clock()
        data = self.port.read(self.port.in_waiting or 1)
        for frame in self.parser.feed(data) if data else []:
            self._handle(frame)

    def _handle(self, frame: Frame) -> None:
        if frame.type == "TEL":
            self.telemetry = frame
            if self.show_telemetry:
                self.out(format_tel(frame))
        elif frame.type == "DONE":
            self.done[frame.seq] = frame
            self.out(format_done(frame))
        elif frame.type == "BOOT":
            if self.keepalive:  # the session was already running
                self.out("WARNING: the UNO reset during the session (low battery when the motors start?)")
            self.boots.append(frame)
            self.out(format_boot(frame))
        elif frame.type == "PONG" and frame.seq in self._keepalive_seqs:
            self._keepalive_seqs.discard(frame.seq)
        elif frame.type in ("ACK", "ERR", "PONG"):
            self.replies[frame.seq] = frame
        else:
            self.out(f"??    unexpected {frame.type},{frame.seq},{','.join(frame.args)}")

    def wait_until(self, condition: Callable[[], bool], timeout: float) -> bool:
        deadline = self.clock() + timeout
        while not condition():
            if self.clock() >= deadline:
                return False
            self.poll()
        return True

    def pump(self, seconds: float) -> None:
        self.wait_until(lambda: False, seconds)

    def request(self, type_: str, *args: int | str, timeout: float = REPLY_TIMEOUT) -> Frame:
        """Sends a request and returns its ACK, ERR or PONG."""
        seq = self.send(type_, *args)
        if not self.wait_until(lambda: seq in self.replies, timeout):
            raise LinkError(f"no reply to {type_} (seq {seq}) within {timeout:g} s")
        return self.replies.pop(seq)

    def wait_boot(self, timeout: float) -> Frame | None:
        return self.boots[-1] if self.wait_until(lambda: bool(self.boots), timeout) else None

    def motion(self, type_: str, *args: int, ms: int) -> MotionResult:
        """Sends a MOVE or TEST and waits for its DONE."""
        reply = self.request(type_, *args)
        if reply.type != "ACK":
            return MotionResult(reply, None)
        if not self.wait_until(lambda: reply.seq in self.done, ms / 1000 + DONE_GRACE):
            raise LinkError(f"no DONE for {type_} seq {reply.seq}")
        return MotionResult(reply, self.done.pop(reply.seq))

    def stop_now(self) -> bool:
        """Best-effort STOP for the way out: written twice at once, then a short wait for an ACK."""
        seqs = []
        for _ in range(2):
            try:
                seqs.append(self.send("STOP"))
            except Exception:
                break
        if not seqs:
            return False
        try:
            return self.wait_until(lambda: any(s in self.replies for s in seqs), 0.5)
        except (Exception, KeyboardInterrupt):
            return False


# ---------------------------------------------------------------- commands


@dataclass
class Ui:
    ask: Callable[[str], str]
    out: Callable[[str], None]


def describe_reply(reply: Frame) -> str:
    return f"{reply.type}  seq {reply.seq}" + (f"  {','.join(reply.args)}" if reply.args else "")


def run_motion(link: Link, ui: Ui, type_: str, *args: int, ms: int) -> int:
    link.show_telemetry = True
    try:
        result = link.motion(type_, *args, ms=ms)
    finally:
        link.show_telemetry = False
    if result.done is None:
        ui.out(f"Rejected: {describe_reply(result.reply)}")
        return 1
    return 0


def cmd_monitor(link: Link, args: argparse.Namespace, ui: Ui) -> int:
    ui.out("Showing telemetry and events. Ctrl+C to quit.")
    link.show_telemetry = True
    link.pump(args.seconds if args.seconds is not None else float("inf"))
    return 0


def cmd_ping(link: Link, args: argparse.Namespace, ui: Ui) -> int:
    reply = link.request("PING")
    if reply.type != "PONG" or len(reply.args) != 2:
        ui.out(f"Unexpected reply: {describe_reply(reply)}")
        return 1
    ui.out(f"PONG  firmware {reply.args[0]}, up {reply.args[1]} s")
    return 0


def cmd_move(link: Link, args: argparse.Namespace, ui: Ui) -> int:
    vx, vy, wz = move_args(args.direction, args.speed)
    ui.out(
        f"MOVE  vx {vx}, vy {vy}, wz {wz} for {args.ms} ms (expect 74HC595 byte {expected_bits(vx, vy, wz)})"
    )
    return run_motion(link, ui, "MOVE", vx, vy, wz, args.ms, ms=args.ms)


def cmd_drive(link: Link, args: argparse.Namespace, ui: Ui) -> int:
    ui.out(f"MOVE  vx {args.vx}, vy {args.vy}, wz {args.wz} for {args.ms} ms")
    return run_motion(link, ui, "MOVE", args.vx, args.vy, args.wz, args.ms, ms=args.ms)


def cmd_raw(link: Link, args: argparse.Namespace, ui: Ui) -> int:
    ui.out(f"TEST  bits {args.bits}, D5 {args.pwm5}, D6 {args.pwm6} for {args.ms} ms")
    return run_motion(link, ui, "TEST", args.bits, args.pwm5, args.pwm6, args.ms, ms=args.ms)


def cmd_stop(link: Link, args: argparse.Namespace, ui: Ui) -> int:
    reply = link.request("STOP")
    ui.out(describe_reply(reply))
    link.pump(0.1)  # a DONE for a running move would follow the ACK
    return 0 if reply.type == "ACK" else 1


def cmd_tilt(link: Link, args: argparse.Namespace, ui: Ui) -> int:
    reply = link.request("TILT", args.deg)
    if reply.type == "ACK" and reply.args:
        ui.out(f"Tilt set to {reply.args[0]} degrees (asked for {args.deg})")
        return 0
    ui.out(f"Rejected: {describe_reply(reply)}")
    return 1


def cmd_conf(link: Link, args: argparse.Namespace, ui: Ui) -> int:
    reply = link.request("CONF", args.key, args.value)
    ui.out(describe_reply(reply))
    if reply.type == "ACK":
        ui.out(
            "Note: settings last until the UNO resets, and the next run of this tool resets it. "
            "Use --conf to set them for a run."
        )
        return 0
    return 1


# ---------------------------------------------------------------- guided tests

STAND_WARNING = """
WARNING: the wheels will turn.
Put the car on a stand with all four wheels off the ground, and keep fingers, cables
and loose things away from them. Press Ctrl+C at any time to stop.
"""


def confirm_stand(ui: Ui) -> bool:
    ui.out(STAND_WARNING)
    answer = ui.ask("Is the car on a stand with the wheels off the ground? Type 'yes' to go on: ")
    if answer.strip().lower() != "yes":
        ui.out("Cancelled.")
        return False
    return True


def ask_choice(ui: Ui, prompt: str, choices: dict[str, str]) -> str:
    """Asks until the answer is one of `choices` (keys); returns the mapped value."""
    while True:
        answer = ui.ask(prompt).strip().lower()
        if answer in choices:
            return choices[answer]
        ui.out(f"Please answer one of: {', '.join(choices)}")


WHEEL_PROMPT = "Which wheel turned? fl, rl, fr, rr (front/rear, left/right), none, several, or again: "
WHEEL_CHOICES = {**{k: k for k in WHEEL_BY_KEY}, "none": "none", "several": "several", "again": "again"}
DIRECTION_PROMPT = "Which way? f = forward (the way that drives the car forward), b = back, ? = not sure: "
DIRECTION_CHOICES = {"f": "forward", "forward": "forward", "b": "back", "back": "back", "?": "?"}


def parse_wheel_set(text: str) -> frozenset[str] | None:
    words = text.replace(",", " ").lower().split()
    if words == ["all"]:
        return ALL_WHEELS
    if words == ["none"]:
        return frozenset()
    if words and all(w in WHEEL_BY_KEY for w in words):
        return frozenset(words)
    return None


def ask_wheel_set(ui: Ui) -> frozenset[str] | None:
    """A set of wheels, or None for 'run it again'."""
    while True:
        answer = ui.ask("Which wheels turned? Any of fl rl fr rr, or all, none, again: ").strip().lower()
        if answer == "again":
            return None
        wheels = parse_wheel_set(answer)
        if wheels is not None:
            return wheels
        ui.out("Please list wheels like 'fl rl', or answer all, none or again.")


def wheel_names(wheels: frozenset[str]) -> str:
    if not wheels:
        return "none"
    return ", ".join(w.name for w in WHEELS if w.key in wheels)


@dataclass
class WheelCheck:
    bit: int
    wheel: Wheel  # the wheel this bit should drive
    direction: str  # "forward" or "back"
    seen_wheel: str  # a wheel key, "none" or "several"
    seen_direction: str  # "forward", "back", "?" or "" when no single wheel turned

    @property
    def ok(self) -> bool:
        return self.seen_wheel == self.wheel.key and self.seen_direction == self.direction

    def seen(self) -> str:
        if self.seen_wheel in WHEEL_BY_KEY:
            return f"{WHEEL_BY_KEY[self.seen_wheel].name} {self.seen_direction}"
        return self.seen_wheel


def wheel_table(checks: list[WheelCheck]) -> list[str]:
    lines = [f"{'bit':>3} {'value':>5}  {'line':<4}  {'expected':<20} {'seen':<20} result"]
    for c in checks:
        expected = f"{c.wheel.name} {c.direction}"
        lines.append(
            f"{c.bit.bit_length() - 1:>3} {c.bit:>5}  {bit_line(c.bit):<4}  "
            f"{expected:<20} {c.seen():<20} {'ok' if c.ok else 'MISMATCH'}"
        )
    return lines


def guided_wheels(link: Link, args: argparse.Namespace, ui: Ui) -> int:
    if not confirm_stand(ui):
        return 1
    ui.out("Each step drives one 74HC595 line alone. Watch all four wheels.")
    checks: list[WheelCheck] = []
    steps = [(w, d, b) for w in WHEELS for d, b in (("forward", w.forward_bit), ("back", w.back_bit))]
    for number, (wheel, direction, bit) in enumerate(steps, 1):
        while True:
            ui.out(
                f"\nStep {number}/{len(steps)}: driving line {bit_line(bit)} (byte {bit}) "
                f"at PWM {args.pwm} for {args.ms} ms ..."
            )
            result = link.motion("TEST", bit, args.pwm, args.pwm, args.ms, ms=args.ms)
            if result.done is None:
                ui.out(f"Rejected: {describe_reply(result.reply)}")
                return 1
            seen_wheel = ask_choice(ui, WHEEL_PROMPT, WHEEL_CHOICES)
            if seen_wheel != "again":
                break
        seen_direction = (
            ask_choice(ui, DIRECTION_PROMPT, DIRECTION_CHOICES) if seen_wheel in WHEEL_BY_KEY else ""
        )
        checks.append(WheelCheck(bit, wheel, direction, seen_wheel, seen_direction))

    ui.out("")
    for line in wheel_table(checks):
        ui.out(line)
    if all(c.ok for c in checks):
        ui.out("\nThe bit map matches protocol/uart.md.")
        return 0
    ui.out("\nMISMATCH: the bit map in protocol/uart.md (and the firmware) doesn't match this car.")
    ui.out("Check the motor plugs first; if they are right, the table above shows what to change.")
    return 1


def pwm_report(d5: frozenset[str], d6: frozenset[str]) -> list[str]:
    lines = [f"D5 drives: {wheel_names(d5)}", f"D6 drives: {wheel_names(d6)}"]
    config = "firmware/scout_uno/src/core/config.h"
    if d5 == LEFT_WHEELS and d6 == RIGHT_WHEELS:
        lines.append(
            "Each pin drives one side, D5 the left. "
            f"Per-side speed: set SCOUT_PWM_SPLIT 1 and SCOUT_PWM5_LEFT 1 in {config}."
        )
    elif d5 == RIGHT_WHEELS and d6 == LEFT_WHEELS:
        lines.append(
            "Each pin drives one side, D5 the right. "
            f"Per-side speed: set SCOUT_PWM_SPLIT 1 and SCOUT_PWM5_LEFT 0 in {config}."
        )
    elif d5 == ALL_WHEELS and d6 == ALL_WHEELS:
        lines.append("Both pins reach every wheel. Keep the shared mode (SCOUT_PWM_SPLIT 0).")
    elif not d5 or not d6:
        lines.append("A pin drove no wheel. Check the wiring, or repeat with a higher --pwm.")
    elif not d5 & d6 and d5 | d6 == ALL_WHEELS:
        lines.append(
            "Each pin drives two wheels, but not one side each. "
            "Keep the shared mode, or set kPwmLayout in mecanum.h to these sets."
        )
    else:
        lines.append(
            "Unexpected result (a wheel on both pins, or a wheel on neither). "
            "Keep the shared mode and repeat the test."
        )
    return lines


def guided_pwmmap(link: Link, args: argparse.Namespace, ui: Ui) -> int:
    if not confirm_stand(ui):
        return 1
    ui.out("All four wheels get 'forward' on the 74HC595, but only one speed pin is driven at a time.")
    seen: dict[str, frozenset[str]] = {}
    forward = expected_bits(100, 0, 0)
    for pin, pwm5, pwm6 in (("D5", args.pwm, 0), ("D6", 0, args.pwm)):
        while True:
            ui.out(f"\nDriving all wheels forward with PWM {args.pwm} on {pin} only, for {args.ms} ms ...")
            result = link.motion("TEST", forward, pwm5, pwm6, args.ms, ms=args.ms)
            if result.done is None:
                ui.out(f"Rejected: {describe_reply(result.reply)}")
                return 1
            wheels = ask_wheel_set(ui)
            if wheels is not None:
                seen[pin] = wheels
                break
    ui.out("")
    for line in pwm_report(seen["D5"], seen["D6"]):
        ui.out(line)
    return 0


def guided_obstacle(link: Link, args: argparse.Namespace, ui: Ui) -> int:
    if not confirm_stand(ui):
        return 1
    obst = args.obst_cm
    hand = max(obst // 2, 3)
    ok = True

    ui.ask(
        f"\n1/2: Hold a hand about {hand} cm in front of the ultrasonic sensor and keep it there. "
        "Press Enter when ready: "
    )
    link.pump(0.5)
    ui.out(format_tel(link.telemetry) if link.telemetry else "(no telemetry yet)")
    result = link.motion("MOVE", args.speed, 0, 0, 3000, ms=3000)
    if result.reason == "OBST" and result.ran_ms == 0:
        ui.out("ok: the forward move never started (DONE OBST, ran 0 ms).")
    elif result.reason == "OBST":
        ui.out(f"ok, but late: the move started and stopped after {result.ran_ms} ms. Was the hand in place?")
    else:
        ui.out(f"FAIL: expected DONE OBST, got {result.reason or describe_reply(result.reply)}.")
        ok = False

    ui.ask(
        "\n2/2: Take your hand away. After Enter the wheels turn forward for up to 5 s; "
        "bring your hand back in front of the sensor while they turn. Press Enter: "
    )
    result = link.motion("MOVE", args.speed, 0, 0, 5000, ms=5000)
    if result.reason == "OBST" and result.ran_ms > 0:
        ui.out(f"ok: the move stopped after {result.ran_ms} ms when the hand came close.")
    elif result.reason == "OBST":
        ui.out("FAIL: the move never started; the hand (or something else) was still too close.")
        ok = False
    else:
        ui.out(f"FAIL: expected DONE OBST, got {result.reason or describe_reply(result.reply)}.")
        ok = False

    ui.out("\nObstacle stop works." if ok else "\nThe obstacle stop did not behave as expected.")
    return 0 if ok else 1


COMMANDS: dict[str, Callable[[Link, argparse.Namespace, Ui], int]] = {
    "monitor": cmd_monitor,
    "ping": cmd_ping,
    "move": cmd_move,
    "drive": cmd_drive,
    "stop": cmd_stop,
    "tilt": cmd_tilt,
    "conf": cmd_conf,
    "raw": cmd_raw,
    "wheels": guided_wheels,
    "pwmmap": guided_pwmmap,
    "obstacle": guided_obstacle,
}


# ---------------------------------------------------------------- command line


def parse_conf(text: str) -> tuple[str, int]:
    key, sep, value = text.partition("=")
    try:
        if not sep:
            raise ValueError
        return key.strip().upper(), int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE with an integer value, got {text!r}") from None


def percent(text: str) -> int:
    value = int(text)
    if not 0 <= value <= 100:
        raise argparse.ArgumentTypeError("speed is a percentage, 0..100")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Bench tool for the Scout UNO firmware (protocol/uart.md).")
    p.add_argument("--port", required=True, help="serial port of the UNO, e.g. COM8 or /dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=BAUD)
    p.add_argument(
        "--conf",
        type=parse_conf,
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="CONF to send after BOOT, e.g. --conf MINPWM=110 (OBST, MINPWM, LINKMS); repeatable",
    )
    p.add_argument("--boot-timeout", type=float, default=BOOT_TIMEOUT, help="seconds to wait for BOOT")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    m = sub.add_parser("monitor", help="print telemetry and events until Ctrl+C")
    m.add_argument("--seconds", type=float, default=None, help="stop after this many seconds")
    sub.add_parser("ping", help="check the UNO answers; prints firmware and uptime")
    m = sub.add_parser("move", help="named move: move <direction> <speed%%> <ms>")
    m.add_argument("direction", choices=list(DIRECTIONS))
    m.add_argument("speed", type=percent, help="wheel speed in percent")
    m.add_argument("ms", type=int)
    m = sub.add_parser("drive", help="raw mecanum move: drive <vx> <vy> <wz> <ms>")
    for name in ("vx", "vy", "wz", "ms"):
        m.add_argument(name, type=int)
    sub.add_parser("stop", help="stop the motors")
    m = sub.add_parser("tilt", help="camera tilt in degrees from level, positive = up")
    m.add_argument("deg", type=int)
    m = sub.add_parser("conf", help="send one CONF and show the reply")
    m.add_argument("key", type=str.upper, help="OBST, MINPWM or LINKMS")
    m.add_argument("value", type=int)
    m = sub.add_parser("raw", help="drive the 74HC595 and the PWM pins directly (TEST)")
    for name in ("bits", "pwm5", "pwm6", "ms"):
        m.add_argument(name, type=int)
    for name, text in (
        ("wheels", "guided: spin each wheel alone and check the bit map"),
        ("pwmmap", "guided: find which wheels D5 and D6 drive"),
    ):
        m = sub.add_parser(name, help=text)
        m.add_argument("--pwm", type=int, default=GUIDE_PWM, help=f"PWM duty 0..255 (default {GUIDE_PWM})")
        m.add_argument(
            "--ms", type=int, default=GUIDE_MS if name == "wheels" else 2000, help="how long each step runs"
        )
    m = sub.add_parser("obstacle", help="guided: check that a forward move stops at a hand")
    m.add_argument("--speed", type=percent, default=30)
    return p


def open_serial(name: str, baud: int) -> SerialPort:
    import serial  # pyserial; imported here so the rest of this file works without it

    return serial.Serial(name, baud, timeout=READ_TIMEOUT, write_timeout=1.0)


def start_session(link: Link, args: argparse.Namespace, ui: Ui) -> None:
    """Waits for BOOT (opening the port resets the UNO), then applies --conf."""
    ui.out(f"Waiting for the UNO on {args.port} to boot ...")
    if link.wait_boot(args.boot_timeout) is None:
        reply = link.request("PING")
        if reply.type != "PONG":
            raise LinkError(f"unexpected reply to PING: {describe_reply(reply)}")
        ui.out(
            f"No BOOT seen, but the UNO answers (firmware {reply.args[0] if reply.args else '?'}); "
            "its settings may not be the defaults."
        )
    link.keepalive = True
    for key, value in args.conf:
        reply = link.request("CONF", key, value)
        if reply.type != "ACK":
            raise LinkError(f"CONF {key}={value} rejected: {describe_reply(reply)}")
        ui.out(f"CONF  {key} = {value}")


def main(
    argv: list[str] | None = None,
    *,
    open_port: Callable[[str, int], SerialPort] = open_serial,
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    args = build_parser().parse_args(argv)
    args.obst_cm = dict(args.conf).get("OBST", DEFAULT_OBST_CM)
    ui = Ui(ask=ask, out=out)
    try:
        port = open_port(args.port, args.baud)
    except Exception as exc:
        out(f"Can't open {args.port}: {exc}")
        return 2
    link = Link(port, clock=clock, out=out)
    try:
        try:
            start_session(link, args, ui)
            return COMMANDS[args.command](link, args, ui)
        except KeyboardInterrupt:
            out("\nInterrupted: sending STOP.")
            link.stop_now()
            return 130
        except LinkError as exc:
            out(f"Error: {exc}. Sending STOP.")
            link.stop_now()
            return 1
        except BaseException:
            link.stop_now()
            raise
    finally:
        port.close()


if __name__ == "__main__":
    sys.exit(main())
