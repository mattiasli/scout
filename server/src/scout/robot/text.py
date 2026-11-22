"""Plain-language phrases for results shown to the AI."""

from __future__ import annotations


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def ago(seconds: float) -> str:
    """``5 minutes ago`` style text for a duration in seconds."""
    s = max(0, int(seconds))
    if s < 5:
        return "just now"
    if s < 60:
        return f"{s} seconds ago"
    m = s // 60
    if m < 60:
        return "a minute ago" if m == 1 else f"{m} minutes ago"
    h = m // 60
    if h < 48:
        return "an hour ago" if h == 1 else f"{h} hours ago"
    return f"{h // 24} days ago"


def cm_text(cm: int | None) -> str:
    return "no echo" if cm is None else f"{cm} cm"


def reason_phrase(reason: str | None, cm_start: int | None, cm_end: int | None, ms: int) -> str:
    """What happened to a move, in words."""
    if reason == "time":
        return "completed"
    if reason == "obstacle":
        cm = cm_end if cm_end is not None else cm_start
        where = f"something {cm} cm ahead" if cm is not None else "something too close ahead"
        return f"didn't move: {where}" if ms == 0 else f"stopped early: {where}"
    if reason == "stopped":
        return "stopped early: stop was requested"
    if reason == "link":
        return "stopped early: the motor board lost contact with the camera board"
    if reason == "replaced":
        return "stopped early: replaced by a newer command"
    return f"stopped early: {reason or 'unknown reason'}"


def heading_label(deg: float) -> str:
    """Heading relative to where a look-around started; clockwise is to the right."""
    d = round(deg) % 360
    if d == 0:
        return "0° ahead"
    if d == 180:
        return "180° behind"
    if d < 180:
        return f"{d}° right"
    return f"{360 - d}° left"
