"""One car connection, server side (protocol/car-link.md).

``CarLink`` owns a WebSocket after it was accepted: it numbers requests, routes replies
and pictures back to whoever asked, sends ``ping`` and closes a car that has gone quiet.
It knows nothing about tools or behaviours; ``scout.robot.robot`` builds on it.

Everything the car sends is treated as untrusted: frames over the size caps, bad JSON,
unknown types and replies to unknown ids are counted in ``stats`` and dropped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from typing import Any, Protocol

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketDisconnected

from scout.robot.config import RobotConfig
from scout.robot.errors import LinkClosed

log = logging.getLogger("scout.robot.link")

# Close codes. 4409 is from car-link.md; the others are this server's choice.
CLOSE_REPLACED = 4409
CLOSE_SILENT = 4408
CLOSE_NO_HELLO = 4400
CLOSE_GOING_AWAY = 1001
CLOSE_ADMIN = 1000

REPLY_TYPES = frozenset({"ack", "done", "err", "ota"})
TERMINAL = frozenset({"done", "err"})

Reply = dict[str, Any] | bytes
StreamSink = Callable[[int, bytes], None]


class ImageTooLarge(Exception):
    """A snapshot was bigger than the server accepts."""


class LinkHandler(Protocol):
    def on_tel(self, link: CarLink, msg: dict[str, Any]) -> None: ...
    def on_event(self, link: CarLink, msg: dict[str, Any]) -> None: ...
    def on_hello(self, link: CarLink, msg: dict[str, Any]) -> None: ...
    def on_activity(self, link: CarLink) -> None: ...


class Pending:
    """Replies to one request, in arrival order."""

    def __init__(self, link: CarLink, request_id: int, kind: str) -> None:
        self.link = link
        self.id = request_id
        self.kind = kind
        self.created = time.monotonic()
        self._queue: asyncio.Queue[Reply | BaseException] = asyncio.Queue()
        self._callback: Callable[[Reply], None] | None = None
        self._detached = False

    def deliver(self, item: Reply | BaseException) -> None:
        if self._detached:
            if self._callback is not None and not isinstance(item, BaseException):
                try:
                    self._callback(item)
                except Exception:  # pragma: no cover - defensive
                    log.exception("reply callback failed")
            if isinstance(item, BaseException) or (isinstance(item, dict) and item.get("t") in TERMINAL):
                self.close()
            return
        self._queue.put_nowait(item)

    async def next(self, wait: float) -> Reply:
        """The next reply. Raises ``TimeoutError``, ``LinkClosed`` or ``ImageTooLarge``."""
        if not self._queue.empty():
            item = self._queue.get_nowait()
        else:
            item = await asyncio.wait_for(self._queue.get(), wait)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def detached(self) -> bool:
        return self._detached

    def detach(self, callback: Callable[[Reply], None] | None = None) -> None:
        """Stop waiting; later replies go to ``callback`` until a ``done`` or ``err``."""
        self._detached = True
        self._callback = callback
        while not self._queue.empty():
            self.deliver(self._queue.get_nowait())

    def close(self) -> None:
        self.link.forget(self.id)


class CarLink:
    def __init__(self, websocket: WebSocket, car_id: str, config: RobotConfig) -> None:
        self.ws = websocket
        self.car_id = car_id
        self.config = config
        self.opened = time.monotonic()
        self.last_rx = self.opened
        self.hello: dict[str, Any] | None = None
        self.last_tel: dict[str, Any] | None = None
        self.hello_event = asyncio.Event()
        self.closed_event = asyncio.Event()
        self.close_code: int | None = None
        self.close_reason: str = ""
        self.stats: Counter[str] = Counter()
        self.stream_sink: StreamSink | None = None
        self.replaced = False
        self._next_id = 1
        self._pending: dict[int, Pending] = {}
        self._send_lock = asyncio.Lock()
        self._closing = False

    # -- state ---------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self.closed_event.is_set() or self._closing

    def pending_count(self) -> int:
        return len(self._pending)

    def forget(self, request_id: int) -> None:
        self._pending.pop(request_id, None)

    # -- sending -------------------------------------------------------------------

    async def send(self, msg: dict[str, Any]) -> None:
        if self.closed:
            raise LinkClosed()
        text = json.dumps(msg, separators=(",", ":"))
        async with self._send_lock:
            if self.closed:
                raise LinkClosed()
            try:
                await self.ws.send_text(text)
            except (WebSocketDisconnect, WebSocketDisconnected, RuntimeError, OSError) as exc:
                raise LinkClosed() from exc

    def _alloc_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id

    async def request(self, t: str, **fields: Any) -> Pending:
        request_id = self._alloc_id()
        pending = Pending(self, request_id, t)
        self._pending[request_id] = pending
        try:
            await self.send({"t": t, "id": request_id, **fields})
        except LinkClosed:
            self.forget(request_id)
            raise
        return pending

    async def fire(self, t: str, callback: Callable[[Reply], None] | None = None, **fields: Any) -> None:
        """Send a request without waiting; replies go to ``callback`` (best effort)."""
        pending = await self.request(t, **fields)
        pending.detach(callback)

    async def close(self, code: int = CLOSE_ADMIN, reason: str = "") -> None:
        if self.closed:
            return
        self._closing = True
        self.close_code = code
        self.close_reason = reason
        try:
            async with asyncio.timeout(2.0):
                await self.ws.close(code, reason)
        except (TimeoutError, WebSocketDisconnect, WebSocketDisconnected, RuntimeError, OSError):
            pass

    # -- receiving -----------------------------------------------------------------

    async def receive_loop(self, handler: LinkHandler) -> None:
        try:
            while True:
                try:
                    message = await self.ws.receive()
                except (WebSocketDisconnect, WebSocketDisconnected, RuntimeError, OSError):
                    break
                if message["type"] == "websocket.disconnect":
                    if self.close_code is None:
                        self.close_code = message.get("code")
                    break
                self.last_rx = time.monotonic()
                handler.on_activity(self)
                text = message.get("text")
                data = message.get("bytes")
                if text is not None:
                    self._on_text(text, handler)
                elif data is not None:
                    self._on_binary(data)
        finally:
            self._mark_closed()

    def _mark_closed(self) -> None:
        self.closed_event.set()
        self.stream_sink = None
        pending, self._pending = self._pending, {}
        for p in pending.values():
            p.deliver(LinkClosed())

    def _on_text(self, text: str, handler: LinkHandler) -> None:
        if len(text) > self.config.max_text_bytes:
            self.stats["oversized_text"] += 1
            return
        try:
            msg = json.loads(text)
        except ValueError:
            self.stats["bad_json"] += 1
            return
        if not isinstance(msg, dict) or not isinstance(msg.get("t"), str):
            self.stats["bad_message"] += 1
            return
        t = msg["t"]
        if t in REPLY_TYPES:
            request_id = msg.get("id")
            if not isinstance(request_id, int) or isinstance(request_id, bool):
                self.stats["bad_message"] += 1
                return
            pending = self._pending.get(request_id)
            if pending is None:
                self.stats["unknown_id"] += 1
                return
            pending.deliver(msg)
        elif t == "pong":
            return  # any message counts as a sign of life; nothing else to do
        elif t == "tel":
            self.last_tel = msg
            handler.on_tel(self, msg)
        elif t == "event":
            handler.on_event(self, msg)
        elif t == "hello":
            first = self.hello is None
            self.hello = msg
            handler.on_hello(self, msg)
            if first:
                self.hello_event.set()
        else:
            self.stats["unknown_type"] += 1

    def _on_binary(self, data: bytes) -> None:
        if len(data) < 8:
            self.stats["bad_frame"] += 1
            return
        magic = bytes(data[:4])
        request_id = int.from_bytes(data[4:8], "little")
        if magic not in (b"SCIM", b"SCST"):
            self.stats["bad_frame"] += 1
            return
        too_big = len(data) > self.config.max_image_bytes
        if magic == b"SCST":
            if too_big:
                self.stats["oversized_frame"] += 1
            elif self.stream_sink is not None:
                self.stream_sink(request_id, bytes(data[8:]))
            else:
                self.stats["unexpected_stream"] += 1
            return
        pending = self._pending.get(request_id)
        if pending is None:
            self.stats["unknown_id"] += 1
            return
        if too_big:
            self.stats["oversized_frame"] += 1
            pending.deliver(ImageTooLarge())
            return
        pending.deliver(bytes(data[8:]))

    # -- keepalive -----------------------------------------------------------------

    async def wait_hello(self) -> dict[str, Any] | None:
        """The ``hello`` message, or ``None`` if the link closed or it never came."""
        hello = asyncio.ensure_future(self.hello_event.wait())
        closed = asyncio.ensure_future(self.closed_event.wait())
        try:
            await asyncio.wait(
                {hello, closed}, timeout=self.config.hello_timeout_s, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            hello.cancel()
            closed.cancel()
        if self.hello is None and not self.closed:
            await self.close(CLOSE_NO_HELLO, "no hello")
        return None if self.closed else self.hello

    async def keepalive(self) -> None:
        """Ping every ``ping_interval_s``; close after ``silence_timeout_s`` of silence."""
        cfg = self.config
        tick = max(0.05, min(cfg.ping_interval_s, cfg.silence_timeout_s / 4, 1.0))
        last_ping = time.monotonic()
        while not self.closed:
            await asyncio.sleep(tick)
            now = time.monotonic()
            if now - self.last_rx > cfg.silence_timeout_s:
                log.info("car %s silent for %.0f s, dropping it", self.car_id, now - self.last_rx)
                await self.close(CLOSE_SILENT, "silent")
                return
            if self.hello is not None and now - last_ping >= cfg.ping_interval_s:
                last_ping = now
                try:
                    await self.send({"t": "ping", "id": self._alloc_id()})
                except LinkClosed:
                    return
            for p in list(self._pending.values()):
                if p.detached and now - p.created > 60:
                    p.close()
