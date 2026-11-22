"""Photo captures on disk: ``data_dir/captures/<capture_id>/NNN.jpg`` plus ``meta.json``.

``meta.json``::

    {"capture_id": str, "car_id": str, "user_id": int, "created": iso8601,
     "photos": [{"file": "001.jpg", "time": iso8601, "note": str|null, "cm": int|null,
                 "heading_deg": float|null, "width": int, "height": int}]}

Capture ids are URL-safe random strings (16 characters), and every lookup validates the
id first, so an id can never name a path outside the captures folder.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scout.robot.errors import InvalidRequest

_ID_RE = re.compile(r"[A-Za-z0-9_-]{12,64}")
MAX_PHOTOS = 999
META = "meta.json"


def new_capture_id() -> str:
    return secrets.token_urlsafe(12)


def valid_capture_id(capture_id: object) -> bool:
    return isinstance(capture_id, str) and _ID_RE.fullmatch(capture_id) is not None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class CaptureStore:
    """Synchronous and thread-safe; the robot calls it through ``asyncio.to_thread``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()

    def directory(self, capture_id: str) -> Path | None:
        if not valid_capture_id(capture_id):
            return None
        path = self.root / capture_id
        return path if (path / META).is_file() else None

    def read_meta(self, capture_id: str) -> dict[str, Any] | None:
        path = self.directory(capture_id)
        if path is None:
            return None
        try:
            meta = json.loads((path / META).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return meta if isinstance(meta, dict) else None

    def create(self, car_id: str, user_id: int) -> str:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            while True:
                capture_id = new_capture_id()
                path = self.root / capture_id
                try:
                    path.mkdir()
                    break
                except FileExistsError:  # pragma: no cover - 96 random bits
                    continue
            meta = {
                "capture_id": capture_id,
                "car_id": car_id,
                "user_id": user_id,
                "created": _now(),
                "photos": [],
            }
            _write_atomic(path / META, json.dumps(meta, indent=2).encode())
            return capture_id

    def check_owner(self, capture_id: str, car_id: str, user_id: int) -> None:
        meta = self.read_meta(capture_id)
        if meta is None:
            raise InvalidRequest(
                f"There is no capture called {capture_id!r}. Leave capture_id empty to start a new one."
            )
        if meta.get("car_id") != car_id:
            raise InvalidRequest(
                "That capture was made with another car. Leave capture_id empty to start a new one."
            )
        if meta.get("user_id") != user_id:
            raise InvalidRequest(
                "That capture belongs to someone else. Leave capture_id empty to start a new one."
            )

    def add_photo(
        self,
        capture_id: str,
        jpeg: bytes,
        *,
        width: int,
        height: int,
        note: str | None = None,
        cm: int | None = None,
        heading_deg: float | None = None,
    ) -> tuple[dict[str, Any], int]:
        """Save the next photo; returns its meta entry and the new photo count."""
        with self._lock:
            path = self.directory(capture_id)
            meta = self.read_meta(capture_id)
            if path is None or meta is None:
                raise InvalidRequest(f"There is no capture called {capture_id!r}.")
            photos = meta.setdefault("photos", [])
            if len(photos) >= MAX_PHOTOS:
                raise InvalidRequest(f"A capture holds at most {MAX_PHOTOS} photos. Start a new one.")
            name = f"{len(photos) + 1:03d}.jpg"
            _write_atomic(path / name, jpeg)
            entry = {
                "file": name,
                "time": _now(),
                "note": note,
                "cm": cm,
                "heading_deg": None if heading_deg is None else round(float(heading_deg), 1),
                "width": width,
                "height": height,
            }
            photos.append(entry)
            _write_atomic(path / META, json.dumps(meta, indent=2).encode())
            return entry, len(photos)
