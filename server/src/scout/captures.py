"""Capture downloads: listing a user's captures, signed links and zips built on the fly.

The robot side writes each capture to ``$SCOUT_DATA_DIR/captures/<id>/`` with a
``meta.json`` holding ``capture_id``, ``car_id``, ``user_id``, ``created`` and ``photos``.
"""

from __future__ import annotations

import io
import json
import os
import re
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from scout.security import LinkSigner

CAPTURE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
LINK_TTL_S = 24 * 3600
MAX_LISTED = 100
_CHUNK = 256 * 1024


def valid_capture_id(capture_id: str) -> bool:
    return bool(CAPTURE_ID_RE.match(capture_id))


@dataclass(frozen=True)
class CaptureInfo:
    capture_id: str
    car_id: str | None
    user_id: int | None
    created: float | None
    created_text: str | None
    photos: int


def _parse_created(value: Any) -> tuple[float | None, str | None]:
    if isinstance(value, bool):
        return None, None
    if isinstance(value, int | float):
        return float(value), None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None, value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp(), value
    return None, None


def read_meta(folder: Path) -> CaptureInfo | None:
    try:
        raw = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    capture_id = str(raw.get("capture_id") or folder.name)
    user_id = raw.get("user_id")
    photos = raw.get("photos")
    if isinstance(photos, list):
        count = len(photos)
    elif isinstance(photos, int) and not isinstance(photos, bool):
        count = photos
    else:
        count = 0
    created, created_text = _parse_created(raw.get("created"))
    return CaptureInfo(
        capture_id=capture_id,
        car_id=str(raw["car_id"]) if raw.get("car_id") is not None else None,
        user_id=user_id if isinstance(user_id, int) and not isinstance(user_id, bool) else None,
        created=created,
        created_text=created_text,
        photos=count,
    )


def list_user_captures(captures_dir: Path, user_id: int, *, limit: int = MAX_LISTED) -> list[CaptureInfo]:
    """The user's captures, newest first. Blocking: run it in a worker thread."""
    found: list[tuple[float, CaptureInfo]] = []
    try:
        entries = list(os.scandir(captures_dir))
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False) or not valid_capture_id(entry.name):
            continue
        info = read_meta(Path(entry.path))
        if info is None or info.user_id != user_id:
            continue
        try:
            mtime = entry.stat(follow_symlinks=False).st_mtime
        except OSError:
            mtime = 0.0
        found.append((info.created if info.created is not None else mtime, info))
    found.sort(key=lambda item: item[0], reverse=True)
    return [info for _, info in found[:limit]]


class CaptureLinks:
    """Signed, expiring download links for captures (handed to AIs in tool results)."""

    def __init__(self, public_url: str, secret_key: str, *, ttl: int = LINK_TTL_S) -> None:
        self.public_url = public_url
        self.ttl = ttl
        self._signer = LinkSigner(secret_key, "capture download")

    def link(self, capture_id: str, *, now: float | None = None) -> str:
        expires = int((time.time() if now is None else now) + self.ttl)
        query = urlencode({"exp": expires, "sig": self._signer.signature(capture_id, expires)})
        return f"{self.public_url}/captures/{capture_id}.zip?{query}"

    def verify(self, capture_id: str, exp: str | None, sig: str | None, *, now: float | None = None) -> bool:
        if not exp or not sig or not exp.isdigit() or len(sig) > 100:
            return False
        return self._signer.verify(capture_id, int(exp), sig, time.time() if now is None else now)


class _Sink(io.RawIOBase):
    """A write-only, non-seekable buffer that zipfile writes into while we stream it out."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        chunk = bytes(data)
        self._chunks.append(chunk)
        return len(chunk)

    def take(self) -> bytes:
        out = b"".join(self._chunks)
        self._chunks.clear()
        return out


def _capture_files(folder: Path) -> list[tuple[Path, str]]:
    """Regular files under the folder (no symlinks), with their names inside the zip."""
    files: list[tuple[Path, str]] = []
    for root, dirs, names in os.walk(folder, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(root, d)))
        for name in sorted(names):
            path = Path(root) / name
            if path.is_symlink() or not path.is_file():
                continue
            files.append((path, path.relative_to(folder).as_posix()))
    return files


def iter_zip(folder: Path, prefix: str) -> Iterator[bytes]:
    """Yield a zip of the capture folder piece by piece, holding at most one chunk in memory.

    Blocking: Starlette runs a sync iterator in a worker thread.
    """
    sink = _Sink()
    with zipfile.ZipFile(sink, mode="w", compression=zipfile.ZIP_STORED) as archive:
        for path, name in _capture_files(folder):
            info = zipfile.ZipInfo.from_file(path, arcname=f"{prefix}/{name}")
            info.compress_type = zipfile.ZIP_STORED
            with path.open("rb") as src, archive.open(info, mode="w") as dest:
                while chunk := src.read(_CHUNK):
                    dest.write(chunk)
                    if data := sink.take():
                        yield data
            if data := sink.take():
                yield data
    if data := sink.take():
        yield data
