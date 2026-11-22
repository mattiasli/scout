"""Checks tools/scoutlink.py against the shared vectors in protocol/uart_vectors.txt."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from scoutlink import Frame, Parser, crc8, encode  # noqa: E402


def rows(kind: str) -> list[list[str]]:
    lines = (ROOT / "protocol" / "uart_vectors.txt").read_text(encoding="ascii").splitlines()
    found = [line.split("\t") for line in lines if line and not line.startswith("#")]
    found = [row for row in found if row[0] == kind]
    assert found, kind
    return found


@pytest.mark.parametrize("row", rows("crc"))
def test_crc(row: list[str]) -> None:
    assert crc8(row[1].encode("ascii")) == int(row[2], 16)


@pytest.mark.parametrize("row", rows("ok"))
def test_valid_frames_parse(row: list[str]) -> None:
    parser = Parser()
    frames = parser.feed(row[1].encode("ascii") + b"\n")
    assert frames == [Frame(row[2], int(row[3]), row[4].split(",") if row[4] else [])]
    assert parser.rx_errors == 0


@pytest.mark.parametrize("row", [r for r in rows("ok") if r[1][-2:] == r[1][-2:].upper()])
def test_encode_reproduces_frames(row: list[str]) -> None:
    args: list[int | str] = []
    for text in row[4].split(",") if row[4] else []:
        frame = Frame("X", 0, [text])
        try:
            args.append(frame.int_arg(0))
        except ValueError:
            args.append(text)
    assert encode(row[2], int(row[3]), *args) == row[1].encode("ascii") + b"\n"


@pytest.mark.parametrize("row", rows("bad"))
def test_invalid_frames_dropped(row: list[str]) -> None:
    parser = Parser()
    assert parser.feed(bytes.fromhex(row[1]) + b"\n") == []
    assert parser.rx_errors == 1, row[2]


@pytest.mark.parametrize("row", rows("int"))
def test_int_args(row: list[str]) -> None:
    frame = Frame("X", 0, [row[1]])
    if row[2] == "bad":
        with pytest.raises(ValueError):
            frame.int_arg(0)
    else:
        assert frame.int_arg(0) == int(row[2])


def test_noise_between_frames() -> None:
    parser = Parser()
    noise = b"ets Jun  8 2016 00:22:57\r\nrst:0x1 (POWERON_RESET)\x01\xff"
    frames = parser.feed(noise + b"$PING,1*51\n" + noise + b"$STOP,7*5C\r\n")
    assert [f.type for f in frames] == ["PING", "STOP"]
    assert parser.rx_errors == 0
