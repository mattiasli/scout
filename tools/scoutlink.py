"""Reference implementation of the UNO serial link framing (see protocol/uart.md).

Used by the bench tool and by the tests that keep the C++ firmware and this file in
agreement through protocol/uart_vectors.txt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_BODY = 64
MAX_ARGS = 8

_TYPE_RE = re.compile(r"[A-Z]{1,8}")
_SEQ_RE = re.compile(r"(0|[1-9][0-9]{0,4})")
_INT_RE = re.compile(r"-?(0|[1-9][0-9]{0,9})")


def crc8(data: bytes) -> int:
    """CRC-8/SMBUS: poly 0x07, init 0x00, no reflection, no final XOR."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def encode(type_: str, seq: int, *args: int | str) -> bytes:
    """Build one frame, e.g. encode("MOVE", 12, 60, 0, 0, 800) -> b"$MOVE,12,60,0,0,800*XX\\n"."""
    if not _TYPE_RE.fullmatch(type_):
        raise ValueError(f"bad type {type_!r}")
    if not 0 <= seq <= 65535:
        raise ValueError(f"bad seq {seq}")
    if len(args) > MAX_ARGS:
        raise ValueError("too many args")
    parts = [type_, str(seq)]
    for arg in args:
        text = str(arg)
        if not text or any(c in text for c in ",$*") or not text.isascii() or not text.isprintable():
            raise ValueError(f"bad arg {arg!r}")
        parts.append(text)
    body = ",".join(parts).encode("ascii")
    if len(body) > MAX_BODY:
        raise ValueError("body too long")
    return b"$" + body + b"*" + f"{crc8(body):02X}".encode("ascii") + b"\n"


@dataclass
class Frame:
    type: str
    seq: int
    args: list[str] = field(default_factory=list)

    def int_arg(self, index: int) -> int:
        text = self.args[index]
        if not _INT_RE.fullmatch(text):
            raise ValueError(f"arg {index} is not an integer: {text!r}")
        value = int(text)
        if not -(2**31) <= value < 2**31:
            raise ValueError(f"arg {index} does not fit in 32 bits")
        return value


def parse_body(body: bytes) -> Frame | None:
    """Validate a body (the bytes between '$' and '*'). Returns None if it is malformed."""
    if not 1 <= len(body) <= MAX_BODY or any(b < 0x20 or b > 0x7E or b in b"$*" for b in body):
        return None
    fields = body.decode("ascii").split(",")
    if len(fields) < 2 or len(fields) > 2 + MAX_ARGS:
        return None
    type_, seq_text, *args = fields
    if not _TYPE_RE.fullmatch(type_) or not _SEQ_RE.fullmatch(seq_text):
        return None
    seq = int(seq_text)
    if seq > 65535:
        return None
    return Frame(type_, seq, args)


class Parser:
    """Streaming parser. Feed it bytes; it yields every valid frame and skips the rest."""

    _HEX = b"0123456789abcdefABCDEF"

    def __init__(self) -> None:
        self._state = "idle"
        self._body = bytearray()
        self._crc_text = bytearray()
        self.rx_errors = 0

    def feed(self, data: bytes) -> list[Frame]:
        frames: list[Frame] = []
        for byte in data:
            frame = self._feed_byte(byte)
            if frame is not None:
                frames.append(frame)
        return frames

    def _feed_byte(self, byte: int) -> Frame | None:
        if byte == ord("$"):
            if self._state != "idle":
                self.rx_errors += 1
            self._state, self._body, self._crc_text = "body", bytearray(), bytearray()
            return None
        if self._state == "idle":
            return None
        if self._state == "body":
            if byte == ord("*"):
                self._state = "crc"
            elif 0x20 <= byte <= 0x7E and len(self._body) < MAX_BODY:
                self._body.append(byte)
            else:
                self.rx_errors += 1
                self._state = "idle"
            return None
        # state == "crc"
        if byte not in self._HEX:
            self.rx_errors += 1
            self._state = "idle"
            return None
        self._crc_text.append(byte)
        if len(self._crc_text) < 2:
            return None
        self._state = "idle"
        if int(self._crc_text, 16) != crc8(bytes(self._body)):
            self.rx_errors += 1
            return None
        frame = parse_body(bytes(self._body))
        if frame is None:
            self.rx_errors += 1
        return frame
