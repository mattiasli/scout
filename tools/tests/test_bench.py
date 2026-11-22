"""Tests for tools/bench.py without a serial port: a fake clock, a fake port and a small
fake UNO that answers the way protocol/uart.md says."""

from __future__ import annotations

import itertools
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import bench  # noqa: E402
import scoutlink  # noqa: E402
from scoutlink import Frame  # noqa: E402

SPEC_CHECK_VALUES = {  # protocol/uart.md, "Mecanum mixing"
    "forward": 163,
    "back": 92,
    "left": 106,
    "right": 149,
    "forward_left": 34,
    "forward_right": 129,
    "back_left": 72,
    "back_right": 20,
    "cw": 172,
    "ccw": 83,
}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeUno:
    """Answers requests like the firmware does, on the fake clock, including its link watchdog."""

    def __init__(self, clock: FakeClock, *, boot: bool = True) -> None:
        self.clock = clock
        self.parser = scoutlink.Parser()
        self.outbox = bytearray()
        self.received: list[tuple[float, Frame]] = []
        self.motion: tuple[int, float, float, str] | None = None  # seq, start, end, reason
        self.outcomes: list[tuple[str, float]] = []  # scripted (reason, after seconds) per motion
        self.errors: dict[str, str] = {}  # request type -> ERR code to answer with
        self.boot_times = [clock() + 0.5] if boot else []
        self.link_s = 1.0
        self.started = clock()
        self.last_rx = clock()
        self.next_tel = clock() + 0.2

    def emit(self, type_: str, seq: int, *args: int | str) -> None:
        self.outbox += scoutlink.encode(type_, seq, *args)

    def write(self, data: bytes) -> int:
        for frame in self.parser.feed(data):
            self.received.append((self.clock(), frame))
            self.last_rx = self.clock()
            self.handle(frame)
        return len(data)

    def handle(self, f: Frame) -> None:
        if f.type in self.errors:
            self.emit("ERR", f.seq, self.errors[f.type])
        elif f.type == "PING":
            self.emit("PONG", f.seq, "0.1.0", int(self.clock() - self.started))
        elif f.type in ("MOVE", "TEST"):
            self.emit("ACK", f.seq)
            if self.motion:
                self.end("NEW")
            reason, after = self.outcomes.pop(0) if self.outcomes else ("TIME", int(f.args[3]) / 1000)
            if reason == "OBST" and after == 0:
                self.emit("DONE", f.seq, "OBST", 8, 8, 0)
            else:
                self.motion = (f.seq, self.clock(), self.clock() + after, reason)
        elif f.type == "STOP":
            self.emit("ACK", f.seq)
            if self.motion:
                self.end("STOP")
        elif f.type == "TILT":
            self.emit("ACK", f.seq, max(-40, min(40, int(f.args[0]))))
        elif f.type == "CONF":
            self.emit("ACK", f.seq)
        else:
            self.emit("ERR", f.seq, "TYPE")

    def end(self, reason: str) -> None:
        assert self.motion is not None
        seq, started, _, _ = self.motion
        self.motion = None
        self.emit("DONE", seq, reason, 50, 40, round((self.clock() - started) * 1000))

    def tick(self) -> None:
        now = self.clock()
        while self.boot_times and now >= self.boot_times[0]:
            self.boot_times.pop(0)
            self.emit("BOOT", 0, "0.1.0", 0)
        if self.motion:
            _, _, end, reason = self.motion
            if now - self.last_rx >= self.link_s:
                self.end("LINK")
            elif now >= end:
                self.end(reason)
        if now >= self.next_tel:
            self.emit("TEL", 0, 50, 512, 600, 480, 0, self.motion[0] if self.motion else 0, 0)
            self.next_tel += 0.2


class FakeSerial:
    """A serial port wired to a FakeUno. An empty read lets 10 ms of fake time pass.

    With interrupt_after_reads, one read raises KeyboardInterrupt, as if Ctrl+C was pressed.
    """

    def __init__(self, uno: FakeUno, clock: FakeClock, *, interrupt_after_reads: int | None = None) -> None:
        self.uno = uno
        self.clock = clock
        self.interrupt_after_reads = interrupt_after_reads
        self.reads = 0
        self.closed = False

    @property
    def in_waiting(self) -> int:
        self.uno.tick()
        return len(self.uno.outbox)

    def read(self, size: int = 1) -> bytes:
        self.reads += 1
        if self.reads == self.interrupt_after_reads:
            raise KeyboardInterrupt
        self.uno.tick()
        if not self.uno.outbox:
            self.clock.advance(0.01)
            self.uno.tick()
        data = bytes(self.uno.outbox[:size])
        del self.uno.outbox[:size]
        return data

    def write(self, data: bytes) -> int:
        return self.uno.write(data)

    def close(self) -> None:
        self.closed = True


class Bench:
    """Runs bench.main() against the fake UNO with scripted operator answers."""

    def __init__(
        self,
        answers: list[object] | None = None,
        *,
        boot: bool = True,
        interrupt_after_reads: int | None = None,
    ) -> None:
        self.clock = FakeClock()
        self.uno = FakeUno(self.clock, boot=boot)
        self.port = FakeSerial(self.uno, self.clock, interrupt_after_reads=interrupt_after_reads)
        self.answers = list(answers or [])
        self.prompts: list[str] = []
        self.lines: list[str] = []

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {prompt}")
        answer = self.answers.pop(0)
        if isinstance(answer, type) and issubclass(answer, BaseException):
            raise answer
        assert isinstance(answer, str)
        return answer

    def run(self, *argv: str) -> int:
        return bench.main(
            ["--port", "FAKE", *argv],
            open_port=lambda name, baud: self.port,
            ask=self.ask,
            out=self.lines.append,
            clock=self.clock,
        )

    @property
    def output(self) -> str:
        return "\n".join(self.lines)

    def sent(self, type_: str | None = None) -> list[Frame]:
        return [f for _, f in self.uno.received if type_ is None or f.type == type_]

    def requests(self) -> list[Frame]:
        """What the tool sent, without the keep-alive PINGs."""
        return [f for f in self.sent() if f.type != "PING"]


# ---------------------------------------------------------------- tables and mixing


def test_direction_table_matches_the_spec_check_values() -> None:
    assert set(bench.DIRECTIONS) == set(SPEC_CHECK_VALUES)
    for name, bits in SPEC_CHECK_VALUES.items():
        for speed in (1, 10, 55, 100):
            assert bench.expected_bits(*bench.move_args(name, speed)) == bits, (name, speed)


def test_move_args_give_the_wheel_speed() -> None:
    assert bench.move_args("forward", 40) == (40, 0, 0)
    assert bench.move_args("back", 40) == (-40, 0, 0)
    assert bench.move_args("left", 40) == (0, -40, 0)
    assert bench.move_args("right", 40) == (0, 40, 0)
    assert bench.move_args("cw", 25) == (0, 0, 25)
    assert bench.move_args("ccw", 25) == (0, 0, -25)
    assert bench.move_args("forward_left", 60) == (30, -30, 0)
    assert bench.move_args("back_right", 100) == (-50, 50, 0)
    assert bench.move_args("forward", 0) == (0, 0, 0)
    # A diagonal drives two wheels at about the requested speed, the others not at all.
    speeds = bench.wheel_speeds(*bench.move_args("forward_right", 60))
    assert speeds == {"fl": 60, "rl": 0, "fr": 0, "rr": 60}


def test_wheel_bits_use_every_shift_register_line_once() -> None:
    bits = [w.forward_bit for w in bench.WHEELS] + [w.back_bit for w in bench.WHEELS]
    assert sorted(bits) == [1 << i for i in range(8)]
    assert bench.bit_line(0x80) == "M1+" and bench.bit_line(0x08) == "M4-" and bench.bit_line(3) == "?"


def test_expected_bits_follow_the_firmware_coast_rule() -> None:
    assert bench.expected_bits(40, 0, 30) == 0x80 | 0x20  # right wheels at 10 of 70: coast
    assert bench.expected_bits(100, 0, 60) == 163  # 25 % exactly still drives
    assert bench.expected_bits(100, 0, 61) == 0x80 | 0x20
    assert bench.expected_bits(0, 0, 0) == 0
    assert bench.wheel_speeds(100, 50, 0) == {"fl": 100, "rl": 33, "fr": 33, "rr": 100}


def test_pwm_report_suggests_the_config() -> None:
    left, right, every = bench.LEFT_WHEELS, bench.RIGHT_WHEELS, bench.ALL_WHEELS
    text = "\n".join(bench.pwm_report(left, right))
    assert "SCOUT_PWM_SPLIT 1" in text and "SCOUT_PWM5_LEFT 1" in text
    text = "\n".join(bench.pwm_report(right, left))
    assert "SCOUT_PWM_SPLIT 1" in text and "SCOUT_PWM5_LEFT 0" in text
    assert "shared mode (SCOUT_PWM_SPLIT 0)" in "\n".join(bench.pwm_report(every, every))
    assert "drove no wheel" in "\n".join(bench.pwm_report(frozenset(), every))
    assert "not one side each" in "\n".join(
        bench.pwm_report(frozenset({"fl", "fr"}), frozenset({"rl", "rr"}))
    )
    assert "Unexpected" in "\n".join(bench.pwm_report(frozenset({"fl", "rl"}), frozenset({"rl", "rr"})))
    assert bench.parse_wheel_set("fl, RR") == frozenset({"fl", "rr"})
    assert bench.parse_wheel_set("all") == every and bench.parse_wheel_set("none") == frozenset()
    assert bench.parse_wheel_set("left") is None


def test_formatting() -> None:
    tel = Frame("TEL", 0, ["-1", "512", "600", "480", "-20", "7", "3"])
    assert bench.format_tel(tel) == (
        "TEL   front    none   line A0/A1/A2  512  600  480   tilt -20   moving seq 7   rx errors 3"
    )
    assert bench.format_done(Frame("DONE", 5, ["OBST", "40", "14", "350"])) == (
        "DONE  seq 5 OBST  front 40 -> 14 cm  ran 350 ms"
    )
    assert "watchdog" in bench.format_boot(Frame("BOOT", 0, ["0.1.0", "8"]))
    assert "bootloader" in bench.format_boot(Frame("BOOT", 0, ["0.1.0", "0"]))


# ---------------------------------------------------------------- the link


class ScriptedPort:
    def __init__(self, clock: FakeClock, data: bytes) -> None:
        self.clock = clock
        self.data = bytearray(data)
        self.written = bytearray()

    @property
    def in_waiting(self) -> int:
        return len(self.data)

    def read(self, size: int = 1) -> bytes:
        if not self.data:
            self.clock.advance(0.01)
            return b""
        out = bytes(self.data[:size])
        del self.data[:size]
        return out

    def write(self, data: bytes) -> int:
        self.written += data
        return len(data)

    def close(self) -> None:
        pass


def test_link_skips_noise_and_matches_replies_by_seq() -> None:
    clock = FakeClock()
    noise = b"ets Jun  8 2016 00:22:57\r\nrst:0x1 (POWERON_RESET)\x01\xff$BROKEN*00\n"
    data = (
        noise
        + scoutlink.encode("TEL", 0, 42, 1, 2, 3, 0, 0, 0)
        + scoutlink.encode("ACK", 9)  # stale reply to some other request
        + scoutlink.encode("ERR", 1, "RANGE")
        + noise
    )
    port = ScriptedPort(clock, data)
    lines: list[str] = []
    link = bench.Link(port, clock=clock, out=lines.append)
    reply = link.request("MOVE", 500, 0, 0, 100)
    assert (reply.type, reply.seq, reply.args) == ("ERR", 1, ["RANGE"])
    assert bytes(port.written) == scoutlink.encode("MOVE", 1, 500, 0, 0, 100)
    assert link.telemetry is not None and link.telemetry.args[0] == "42"
    link.pump(0.1)
    assert link.parser.rx_errors == 2  # "$BROKEN*00" twice
    assert lines == []  # replies and telemetry aren't printed by the link itself


def test_link_times_out_without_a_reply() -> None:
    clock = FakeClock()
    link = bench.Link(ScriptedPort(clock, b""), clock=clock, out=lambda _: None)
    with pytest.raises(bench.LinkError):
        link.request("PING", timeout=0.3)


def test_seq_counts_from_1_and_wraps_past_65535() -> None:
    clock = FakeClock()
    link = bench.Link(ScriptedPort(clock, b""), clock=clock, out=lambda _: None)
    assert link.next_seq() == 1
    link.seq = 65535
    assert link.next_seq() == 1


def test_keepalive_pings_are_answered_silently() -> None:
    b = Bench()
    assert b.run("monitor", "--seconds", "2") == 0
    pings = [t for t, f in b.uno.received if f.type == "PING"]
    assert len(pings) >= 7
    assert max(later - earlier for earlier, later in itertools.pairwise(pings)) <= 0.3
    assert "PONG" not in b.output
    assert b.output.count("TEL ") >= 8


# ---------------------------------------------------------------- commands


def test_waits_for_boot_before_sending_anything() -> None:
    b = Bench()
    assert b.run("ping") == 0
    first_request_time = b.uno.received[0][0]
    assert first_request_time >= 1000.5  # BOOT came 0.5 s after the port opened
    assert "BOOT  firmware 0.1.0" in b.output
    assert "PONG  firmware 0.1.0" in b.output
    assert b.port.closed


def test_without_boot_it_checks_with_a_ping() -> None:
    b = Bench(boot=False)
    assert b.run("--boot-timeout", "0.5", "tilt", "90") == 0
    assert "No BOOT seen, but the UNO answers" in b.output
    assert "Tilt set to 40 degrees (asked for 90)" in b.output


def test_move_sends_the_mixed_move_and_keeps_the_link_alive() -> None:
    b = Bench()
    assert b.run("move", "forward_left", "60", "1800") == 0
    moves = b.sent("MOVE")
    assert [m.args for m in moves] == [["30", "-30", "0", "1800"]]
    assert "expect 74HC595 byte 34" in b.output
    assert "DONE  seq" in b.output and "TIME" in b.output and "LINK" not in b.output
    times = [t for t, _ in b.uno.received]
    move_time = next(t for t, f in b.uno.received if f.type == "MOVE")
    during = [t for t in times if move_time <= t <= move_time + 1.8]
    assert max(later - earlier for earlier, later in itertools.pairwise(during)) <= 0.3


def test_drive_raw_stop_and_conf_commands() -> None:
    b = Bench()
    assert b.run("drive", "0", "0", "-50", "300") == 0
    assert b.sent("MOVE")[0].args == ["0", "0", "-50", "300"]
    b = Bench()
    assert b.run("raw", "128", "200", "0", "400") == 0
    assert b.sent("TEST")[0].args == ["128", "200", "0", "400"]
    b = Bench()
    assert b.run("stop") == 0
    assert "ACK" in b.output
    b = Bench()
    assert b.run("conf", "obst", "30") == 0
    assert b.sent("CONF")[0].args == ["OBST", "30"]


def test_a_rejected_request_fails_the_command() -> None:
    b = Bench()
    b.uno.errors["MOVE"] = "RANGE"
    assert b.run("drive", "500", "0", "0", "100") == 1
    assert "Rejected: ERR" in b.output and "RANGE" in b.output


def test_conf_option_is_sent_after_boot_and_before_the_command() -> None:
    b = Bench()
    assert b.run("--conf", "obst=30", "--conf", "MINPWM=110", "move", "forward", "20", "200") == 0
    assert [(f.type, f.args) for f in b.requests()] == [
        ("CONF", ["OBST", "30"]),
        ("CONF", ["MINPWM", "110"]),
        ("MOVE", ["20", "0", "0", "200"]),
    ]


def test_a_rejected_conf_option_stops_the_run() -> None:
    b = Bench()
    b.uno.errors["CONF"] = "RANGE"
    assert b.run("--conf", "OBST=500", "move", "forward", "20", "200") == 1
    assert not b.sent("MOVE")
    assert b.sent("STOP")


def test_bad_arguments_are_refused_before_opening_the_port() -> None:
    b = Bench()
    with pytest.raises(SystemExit):
        b.run("--conf", "OBST", "ping")
    with pytest.raises(SystemExit):
        b.run("move", "up", "20", "200")
    with pytest.raises(SystemExit):
        b.run("move", "forward", "120", "200")
    assert not b.uno.received


def test_an_unexpected_boot_is_reported() -> None:
    b = Bench()
    b.uno.boot_times.append(b.clock() + 1.2)  # resets during the move
    b.run("move", "forward", "50", "1500")
    assert "WARNING: the UNO reset during the session" in b.output


# ---------------------------------------------------------------- Ctrl+C


def test_ctrl_c_during_monitor_sends_stop() -> None:
    b = Bench(interrupt_after_reads=150)
    assert b.run("monitor") == 130
    assert b.sent("STOP")
    assert "Interrupted: sending STOP" in b.output


def test_ctrl_c_during_a_move_sends_stop_at_once() -> None:
    b = Bench(interrupt_after_reads=120)
    assert b.run("move", "forward", "50", "5000") == 130
    move_time = next(t for t, f in b.uno.received if f.type == "MOVE")
    stop_time = next(t for t, f in b.uno.received if f.type == "STOP")
    assert stop_time < move_time + 5.0
    assert "DONE" in b.output and "STOP" in b.output


def test_ctrl_c_at_a_prompt_sends_stop() -> None:
    b = Bench(["yes", KeyboardInterrupt])
    assert b.run("wheels") == 130
    types = [f.type for f in b.sent()]
    assert "STOP" in types[types.index("TEST") :]


def test_ctrl_c_while_waiting_for_boot_still_sends_stop() -> None:
    b = Bench(interrupt_after_reads=5)
    assert b.run("ping") == 130
    assert b.sent("STOP")


# ---------------------------------------------------------------- guided tests

CORRECT_WHEEL_ANSWERS = [
    "fl",
    "f",
    "fl",
    "b",
    "rl",
    "f",
    "rl",
    "b",
    "fr",
    "f",
    "fr",
    "b",
    "rr",
    "f",
    "rr",
    "b",
]


@pytest.mark.parametrize("command", ["wheels", "pwmmap", "obstacle"])
def test_guided_tests_warn_and_need_a_yes(command: str) -> None:
    b = Bench(["no"])
    assert b.run(command) == 1
    assert "WARNING: the wheels will turn" in b.output and "stand" in b.output
    assert "off the ground" in b.prompts[0]
    assert not b.sent("TEST") and not b.sent("MOVE")


def test_wheels_spins_each_line_alone_and_confirms_the_bit_map() -> None:
    b = Bench(["yes", *CORRECT_WHEEL_ANSWERS])
    assert b.run("wheels") == 0
    tests = b.sent("TEST")
    assert [int(t.args[0]) for t in tests] == [128, 64, 32, 16, 2, 4, 1, 8]
    assert all(t.args[1:] == [str(bench.GUIDE_PWM), str(bench.GUIDE_PWM), str(bench.GUIDE_MS)] for t in tests)
    assert b.output.count(" ok") == 8
    assert "matches protocol/uart.md" in b.output
    assert "LINK" not in b.output


def test_wheels_reports_a_mismatch_and_can_repeat_a_step() -> None:
    answers = list(CORRECT_WHEEL_ANSWERS)
    answers[8:12] = ["rr", "f", "rr", "b"]  # the M3 lines turned the rear-right wheel
    answers[0:0] = ["huh", "again"]  # a bad answer, then a repeat of step 1
    b = Bench(["yes", *answers])
    assert b.run("wheels") == 1
    assert len(b.sent("TEST")) == 9
    assert "Please answer one of" in b.output
    assert b.output.count("MISMATCH") == 3  # two table rows and the summary
    assert "front-right forward  rear-right forward" in b.output


def test_pwmmap_drives_one_pin_at_a_time() -> None:
    b = Bench(["yes", "again", "fl rl", "fr rr"])
    assert b.run("pwmmap") == 0
    assert [t.args for t in b.sent("TEST")] == [
        ["163", "150", "0", "2000"],
        ["163", "150", "0", "2000"],
        ["163", "0", "150", "2000"],
    ]
    assert "D5 drives: front-left, rear-left" in b.output
    assert "SCOUT_PWM5_LEFT 1" in b.output


def test_obstacle_passes_when_both_moves_stop_for_the_hand() -> None:
    b = Bench(["yes", "", ""])
    b.uno.outcomes = [("OBST", 0), ("OBST", 1.2)]
    assert b.run("obstacle") == 0
    moves = b.sent("MOVE")
    assert [m.args for m in moves] == [["30", "0", "0", "3000"], ["30", "0", "0", "5000"]]
    assert "never started" in b.output
    assert re.search(r"stopped after 12[01]\d ms", b.output)  # the fake UNO runs in 10 ms steps
    assert "Obstacle stop works." in b.output
    assert "7 cm" in b.prompts[1]  # half the default 15 cm OBST


def test_obstacle_fails_when_the_moves_run_out_their_time() -> None:
    b = Bench(["yes", "", ""])
    assert b.run("--conf", "OBST=30", "obstacle", "--speed", "20") == 1
    assert b.output.count("FAIL: expected DONE OBST, got TIME") == 2
    assert "15 cm" in b.prompts[1]
    assert b.sent("MOVE")[0].args[0] == "20"
