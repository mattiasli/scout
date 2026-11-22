"""Short-lived image links (``/i/<id>``).

Pictures from tool calls are kept in memory for ``ttl`` seconds under an unguessable id,
so an AI can hand a link to a person. The store is capped by count and by total bytes;
the oldest pictures go first.
"""

from __future__ import annotations

import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

IMAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


@dataclass(frozen=True)
class _Entry:
    data: bytes
    expires: float


class ImageStore:
    def __init__(
        self,
        *,
        ttl: float = 15 * 60,
        max_bytes: int = 64 * 1024 * 1024,
        max_items: int = 2000,
        max_image_bytes: int = 8 * 1024 * 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl = ttl
        self.max_bytes = max_bytes
        self.max_items = max_items
        self.max_image_bytes = max_image_bytes
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._bytes = 0

    @property
    def total_bytes(self) -> int:
        return self._bytes

    def __len__(self) -> int:
        return len(self._entries)

    def _drop_oldest(self) -> None:
        _, entry = self._entries.popitem(last=False)
        self._bytes -= len(entry.data)

    def _purge(self) -> None:
        now = self._clock()
        # Every entry has the same lifetime, so insertion order is expiry order.
        while self._entries and next(iter(self._entries.values())).expires <= now:
            self._drop_oldest()

    def put(self, data: bytes) -> str | None:
        """Keep a picture. Returns its id, or None if it's too big to keep."""
        if len(data) > self.max_image_bytes or len(data) > self.max_bytes:
            return None
        self._purge()
        while self._entries and (
            len(self._entries) >= self.max_items or self._bytes + len(data) > self.max_bytes
        ):
            self._drop_oldest()
        image_id = secrets.token_urlsafe(24)
        self._entries[image_id] = _Entry(data=data, expires=self._clock() + self.ttl)
        self._bytes += len(data)
        return image_id

    def get(self, image_id: str) -> bytes | None:
        if not IMAGE_ID_RE.match(image_id):
            return None
        self._purge()
        entry = self._entries.get(image_id)
        return entry.data if entry is not None else None
