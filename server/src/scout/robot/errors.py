"""Errors the robot side raises, and how car ``err`` codes map onto them."""

from __future__ import annotations

from scout.contracts import CarBusy, CarError


class InvalidRequest(CarError):
    """A tool argument is missing or out of range.

    It subclasses ``CarError`` so the app side, which already shows ``CarError``
    messages to the AI, needs no extra handling. The message says what is allowed.
    """


class LinkClosed(Exception):
    """The car's connection closed while we were waiting on it (internal)."""


class ReplyTimeout(Exception):
    """The car didn't answer a request in time (internal)."""


def clean_text(value: object, limit: int = 120) -> str:
    """Printable ASCII only, trimmed. Car-supplied text goes through this."""
    text = value if isinstance(value, str) else ""
    text = "".join(ch for ch in text if 32 <= ord(ch) < 127)
    return text[:limit].strip()


def error_for_code(code: object, msg: object, name: str) -> Exception:
    """Turn a car ``err`` reply into the exception the AI should see."""
    detail = clean_text(msg)
    suffix = f" ({detail})" if detail else ""
    if code == "uno":
        return CarError(f"{name}'s motor board isn't answering, so it can't move right now.")
    if code == "camera":
        return CarError(f"{name}'s camera failed to take the picture{suffix}.")
    if code == "args":
        return CarError(f"{name} rejected the request as invalid{suffix}.")
    if code == "busy":
        return CarBusy(f"{name} is busy and can't do that right now{suffix}.")
    if code == "timeout":
        return CarError(f"{name}'s motor board didn't report back in time{suffix}.")
    if code == "unknown_type":
        return CarError(f"{name}'s firmware doesn't support this command. It may need a firmware update.")
    if code == "ota":
        return CarError(f"The firmware update failed{suffix}.")
    return CarError(f"{name} reported an error ({clean_text(code, 24) or 'unknown'}){suffix}.")
