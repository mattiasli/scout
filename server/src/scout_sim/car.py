"""The simulated car: the ESP32-CAM side of protocol/car-link.md.

``SimCar`` dials the server, says ``hello``, applies the ``welcome`` settings and answers
requests the way the firmware does, with a ``SimUno`` for the motors and a ``World`` for
the room, the ultrasonic sensor and the camera. ``Faults`` injects trouble for tests:
dropped or delayed replies, a broken camera, a silent car, a missing UNO and so on.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
import time
from collections import deque
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus, InvalidURI

from scout_sim.render import RESOLUTIONS, snapshot
from scout_sim.uno import Done, SimUno
from scout_sim.world import World

log = logging.getLogger("scout_sim")

RES_ORDER = {name: i for i, name in enumerate(RESOLUTIONS)}
# Roughly how long the camera takes to capture and encode a frame.
SNAP_DELAY_S = {
    "qvga": 0.04,
    "vga": 0.08,
    "svga": 0.1,
    "xga": 0.15,
    "hd": 0.18,
    "sxga": 0.22,
    "uxga": 0.3,
    "qxga": 0.45,
}
REQUESTS = frozenset(
    {"move", "stop", "tilt", "light", "snap", "stream", "conf", "test", "ota", "reboot", "ping"}
)
UNO_DONE_TIMEOUT_S = 1.5
_SHA_RE = re.compile(r"[0-9a-fA-F]{64}")


@dataclass
class SimConfig:
    url: str
    key: str
    fw: str = "0.1.0"
    camera: str | None = "OV3660"
    psram: int = 4 * 1024 * 1024
    max_res: str = "qxga"
    mac: str = "02:00:00:5C:00:01"  # a locally administered address, not a real device
    ip: str = "192.168.1.50"
    ssid: str = "sim-wifi"
    rssi: int = -55
    seed: int | None = None
    # Timing (protocol/car-link.md); tests shrink the backoff and the OTA steps.
    tel_interval_s: float = 1.0
    tel_moving_interval_s: float = 0.2
    tick_s: float = 0.02
    uno_ping_s: float = 0.25
    server_silence_s: float = 15.0
    open_timeout_s: float = 10.0
    backoff_initial_s: float = 2.0
    backoff_max_s: float = 60.0
    backoff_reset_s: float = 60.0
    backoff_jitter: float = 0.2
    snap_delay_scale: float = 1.0
    ota_step_s: float = 0.5
    reboot_s: float = 1.5
    next_fw: str | None = None  # firmware after a simulated OTA; default bumps the patch


@dataclass
class Faults:
    """Trouble to inject. Change the fields at any time; they take effect at once."""

    drop: set[str] = field(default_factory=set)  # request types whose replies never arrive
    delay_s: dict[str, float] = field(default_factory=dict)  # extra reply delay per type, "*" = all
    camera_error: bool = False  # snap and stream fail with "camera"
    uno_offline: bool = False  # the UNO doesn't answer: move/test/stop/tilt fail with "uno"
    uno_no_done: bool = False  # the UNO never reports DONE, so the car reports "error"
    uno_link_lost: bool = False  # the ESP stops pinging the UNO: running moves end with "link"
    silent: bool = False  # the car sends nothing at all
    ota_fail: bool = False  # the update fails verification


class _ArgsError(Exception):
    pass


def _int(msg: dict[str, Any], key: str, low: int, high: int, default: int | None = None) -> int:
    value = msg.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _ArgsError(f"{key} must be {low}..{high}")
    return value


def _bump(fw: str) -> str:
    parts = fw.split(".")
    if parts and parts[-1].isdigit():
        parts[-1] = str(int(parts[-1]) + 1)
        return ".".join(parts)
    return fw + ".1"


class SimCar:
    def __init__(
        self, config: SimConfig, *, world: World | None = None, faults: Faults | None = None
    ) -> None:
        self.config = config
        self.world = world or World.default(seed=config.seed)
        self.faults = faults or Faults()
        now = time.monotonic()
        self.uno = SimUno(self.world, now)
        self.fw = config.fw
        self.boot = now
        self.reset_reason = "poweron"
        self.vflip = False  # until the server's conf arrives the sensor's raw image is upside down
        self.hmirror = False
        self.light = 0
        self.welcome: dict[str, Any] | None = None
        self.connected = asyncio.Event()
        self.welcomed = asyncio.Event()
        self.connections = 0
        self.close_codes: list[int | None] = []
        self.refusals: list[int] = []
        self.received: deque[dict[str, Any]] = deque(maxlen=5000)
        self.sent: deque[dict[str, Any]] = deque(maxlen=5000)
        self._rng = random.Random(config.seed)
        self._ws: ClientConnection | None = None
        self._stopping = False
        self._wake = asyncio.Event()
        self._next_delay: float | None = None
        self._up_since: float | None = None
        self._last_rx = now
        self._stream: asyncio.Task[None] | None = None
        self._awaiting_done: dict[int, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._run_task: asyncio.Task[None] | None = None
        self._uno_was_offline = False
        self._ota_running = False

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> asyncio.Task[None]:
        """Run in the background (for tests); ``close`` stops it."""
        self._run_task = asyncio.create_task(self.run())
        return self._run_task

    async def close(self) -> None:
        self._stopping = True
        self._wake.set()
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        if self._run_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                async with asyncio.timeout(5):
                    await self._run_task
            if not self._run_task.done():
                self._run_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._run_task

    async def run(self) -> None:
        """Connect, serve the server, reconnect with backoff; until ``close``."""
        cfg = self.config
        world = asyncio.create_task(self._world_loop())
        delay = cfg.backoff_initial_s
        try:
            while not self._stopping:
                self._up_since = None
                try:
                    await self._session()
                except InvalidStatus as exc:
                    self.refusals.append(exc.response.status_code)
                    log.warning("the server refused the car (HTTP %d)", exc.response.status_code)
                except (OSError, InvalidHandshake, InvalidURI, ConnectionClosed, TimeoutError) as exc:
                    log.info("connection failed: %s", exc)
                if self._stopping:
                    break
                if self._up_since is not None and time.monotonic() - self._up_since >= cfg.backoff_reset_s:
                    delay = cfg.backoff_initial_s
                if self._next_delay is not None:
                    wait, self._next_delay = self._next_delay, None
                else:
                    jitter = cfg.backoff_jitter
                    wait = delay * self._rng.uniform(1 - jitter, 1 + jitter)
                    delay = min(delay * 2, cfg.backoff_max_s)
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(wait):
                        await self._wake.wait()
        finally:
            world.cancel()
            tasks = [world, *self._tasks]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _session(self) -> None:
        cfg = self.config
        headers = {"Authorization": f"Bearer {cfg.key}", "X-Scout-Firmware": self.fw}
        host = urlsplit(cfg.url).hostname or ""
        proxy: Literal[True] | None = None if host in ("127.0.0.1", "localhost", "::1") else True
        async with connect(
            cfg.url,
            additional_headers=headers,
            user_agent_header=f"scout-sim/{self.fw}",
            open_timeout=cfg.open_timeout_s,
            ping_interval=None,
            close_timeout=2,
            max_size=2**20,
            compression=None,
            proxy=proxy,
        ) as ws:
            if self._stopping:  # close() ran while the handshake was in flight
                await ws.close(1001, "shutting down")
                return
            self._ws = ws
            self.connections += 1
            self._up_since = time.monotonic()
            self._last_rx = time.monotonic()
            self.connected.set()
            log.info("connected to %s", cfg.url)
            await self._send(self._hello())  # always the first message
            helpers = [asyncio.create_task(self._tel_loop()), asyncio.create_task(self._silence_watch(ws))]
            try:
                async for message in ws:
                    self._last_rx = time.monotonic()
                    if isinstance(message, str):
                        self._on_text(message)
            except ConnectionClosed:
                pass
            finally:
                self.close_codes.append(ws.close_code)
                log.info("disconnected (code %s)", ws.close_code)
                for task in helpers:
                    task.cancel()
                await asyncio.gather(*helpers, return_exceptions=True)
                self._ws = None
                self.connected.clear()
                self.welcomed.clear()
                self._on_disconnect()

    def _on_disconnect(self) -> None:
        # The firmware stops the motors and switches the light off before reconnecting.
        self.uno.halt()
        self.light = 0
        if self._stream is not None:
            self._stream.cancel()
            self._stream = None
        for task in self._awaiting_done.values():
            task.cancel()
        self._awaiting_done.clear()

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- test helpers --------------------------------------------------------------

    async def send_raw(self, data: str | bytes) -> None:
        """Send exactly ``data`` (to test how the server copes with junk)."""
        if self._ws is not None:
            await self._ws.send(data)

    def drop_connection(self) -> None:
        """Cut the connection without a close frame, like a WiFi dropout."""
        if self._ws is not None:
            self._ws.transport.abort()

    async def restart(self, reason: str = "sw") -> None:
        """Reboot the ESP32: close, wait ``reboot_s`` and connect again."""
        self.reset_reason = reason
        self.boot = time.monotonic()
        self._next_delay = self.config.reboot_s
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close(1001, "rebooting")

    # -- sending -------------------------------------------------------------------

    async def _send(self, msg: dict[str, Any] | bytes) -> None:
        ws = self._ws
        if ws is None or self.faults.silent:
            return
        try:
            if isinstance(msg, bytes):
                await ws.send(msg)
            else:
                self.sent.append(msg)
                await ws.send(json.dumps(msg, separators=(",", ":")))
        except ConnectionClosed:
            pass

    async def _reply(self, kind: str, msg: dict[str, Any] | bytes) -> None:
        if kind in self.faults.drop:
            return
        delay = self.faults.delay_s.get(kind, self.faults.delay_s.get("*", 0.0))
        if delay > 0:
            await asyncio.sleep(delay)
        await self._send(msg)

    def _hello(self) -> dict[str, Any]:
        cfg = self.config
        return {
            "t": "hello",
            "fw": self.fw,
            "uno_fw": None if self.faults.uno_offline else self.uno.fw,
            "camera": cfg.camera,
            "psram": cfg.psram,
            "max_res": cfg.max_res,
            "mac": cfg.mac,
            "ip": cfg.ip,
            "ssid": cfg.ssid,
            "rssi": self._rssi(),
            "reset": self.reset_reason,
        }

    def _rssi(self) -> int:
        return self.config.rssi + self._rng.randint(-3, 3)

    def _tel(self) -> dict[str, Any]:
        uno_ok = not self.faults.uno_offline
        tel: dict[str, Any] = {
            "t": "tel",
            "cm": self.world.ultrasonic() if uno_ok else None,
            "line": self.world.line_sensors() if uno_ok else None,
            "tilt": self.uno.tilt,
            "moving": self.uno.moving,
            "light": self.light,
            "rssi": self._rssi(),
            "heap": 150_000 + self._rng.randint(-4000, 4000),
            "uno": uno_ok,
            "up": int(time.monotonic() - self.boot),
        }
        if uno_ok:
            # The laser on the tilt panel, simplified: what's straight ahead, without the
            # ultrasonic's noise (and without using up random numbers).
            laser = self.world.ultrasonic(noise=False)
            tel["laser"] = -1 if laser is None else laser
        return tel

    # -- background loops ----------------------------------------------------------

    async def _world_loop(self) -> None:
        cfg = self.config
        last = time.monotonic()
        last_ping = last
        while True:
            await asyncio.sleep(cfg.tick_s)
            now = time.monotonic()
            if not self.faults.uno_link_lost and now - last_ping >= cfg.uno_ping_s:
                self.uno.host_frame(now)
                last_ping = now
            for done in self.uno.tick(now):
                self._spawn(self._report(done))
            self.world.step_cat(now - last)
            last = now
            if self.faults.uno_offline != self._uno_was_offline:
                self._uno_was_offline = self.faults.uno_offline
                if self.faults.uno_offline:
                    self.uno.halt()
                    self._spawn(
                        self._send({"t": "event", "kind": "uno_lost", "msg": "UNO stopped answering"})
                    )
                else:
                    self._spawn(self._send({"t": "event", "kind": "uno_back", "msg": "UNO answering again"}))

    async def _tel_loop(self) -> None:
        cfg = self.config
        while True:
            await self._send(self._tel())
            sent = time.monotonic()
            while True:
                await asyncio.sleep(0.05)
                interval = cfg.tel_moving_interval_s if self.uno.moving else cfg.tel_interval_s
                if time.monotonic() - sent >= interval:
                    break

    async def _silence_watch(self, ws: ClientConnection) -> None:
        limit = self.config.server_silence_s
        while True:
            await asyncio.sleep(min(1.0, limit / 4))
            if time.monotonic() - self._last_rx > limit:
                log.warning("nothing from the server for %.0f s, reconnecting", limit)
                await ws.close(1000, "server silent")
                return

    # -- requests ------------------------------------------------------------------

    def _on_text(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        self.received.append(msg)
        t = msg.get("t")
        if t == "welcome":
            self.welcome = msg
            conf = msg.get("conf")
            if isinstance(conf, dict):
                self._apply_conf(conf, strict=False)
            self.welcomed.set()
            return
        request_id = msg.get("id")
        if isinstance(request_id, bool) or not isinstance(request_id, int) or not 0 < request_id < 2**32:
            return  # requests always carry an id; without one there is nobody to answer
        self._spawn(self._handle(t if isinstance(t, str) else "?", request_id, msg))

    async def _handle(self, t: str, request_id: int, msg: dict[str, Any]) -> None:
        try:
            if t not in REQUESTS:
                await self._reply(t, {"t": "err", "id": request_id, "code": "unknown_type", "msg": t[:40]})
                return
            await getattr(self, f"_req_{t}")(request_id, msg)
        except _ArgsError as exc:
            await self._reply(t, {"t": "err", "id": request_id, "code": "args", "msg": str(exc)})
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - a simulator bug, not a protocol event
            log.exception("handling %s failed", t)

    async def _uno_error(self, kind: str, request_id: int) -> None:
        await asyncio.sleep(0.05)  # the ESP waits for the UNO's ACK before giving up
        await self._reply(kind, {"t": "err", "id": request_id, "code": "uno", "msg": "UNO not answering"})

    async def _report(self, done: Done) -> None:
        if self.faults.uno_no_done:
            return  # the ESP's own timeout reports it
        watch = self._awaiting_done.pop(done.request_id, None)
        if watch is not None:
            watch.cancel()
        msg = {
            "t": "done",
            "id": done.request_id,
            "reason": done.reason,
            "cm_start": done.cm_start,
            "cm_end": done.cm_end,
            "ms": done.ms,
        }
        await self._reply(done.kind, msg)

    def _watch_done(self, request_id: int, kind: str, ms: int) -> None:
        async def watch() -> None:
            await asyncio.sleep(ms / 1000 + UNO_DONE_TIMEOUT_S)
            self._awaiting_done.pop(request_id, None)
            await self._reply(
                kind,
                {"t": "done", "id": request_id, "reason": "error", "cm_start": None, "cm_end": None, "ms": 0},
            )
            await self._reply(
                kind, {"t": "err", "id": request_id, "code": "timeout", "msg": "no DONE from UNO"}
            )

        self._awaiting_done[request_id] = self._spawn(watch())

    async def _start_run(self, kind: str, request_id: int, dones: list[Done], ms: int) -> None:
        started = self.uno.run is not None and self.uno.run.request_id == request_id
        if started:
            self._watch_done(request_id, kind, ms)
        await self._reply(kind, {"t": "ack", "id": request_id})
        for done in dones:
            await self._report(done)

    async def _req_move(self, request_id: int, msg: dict[str, Any]) -> None:
        vx = _int(msg, "vx", -100, 100)
        vy = _int(msg, "vy", -100, 100)
        wz = _int(msg, "wz", -100, 100)
        ms = _int(msg, "ms", 1, 5000)
        if self.faults.uno_offline:
            await self._uno_error("move", request_id)
            return
        dones = self.uno.move(request_id, vx, vy, wz, ms, time.monotonic())
        await self._start_run("move", request_id, dones, ms)

    async def _req_test(self, request_id: int, msg: dict[str, Any]) -> None:
        bits = _int(msg, "bits", 0, 255)
        pwm5 = _int(msg, "pwm5", 0, 255)
        pwm6 = _int(msg, "pwm6", 0, 255)
        ms = _int(msg, "ms", 1, 2000)
        if self.faults.uno_offline:
            await self._uno_error("test", request_id)
            return
        dones = self.uno.test(request_id, bits, pwm5, pwm6, ms, time.monotonic())
        await self._start_run("test", request_id, dones, ms)

    async def _req_stop(self, request_id: int, msg: dict[str, Any]) -> None:
        if self.faults.uno_offline:
            await self._uno_error("stop", request_id)
            return
        done = self.uno.stop(time.monotonic())
        await self._reply("stop", {"t": "ack", "id": request_id})
        if done is not None:
            await self._report(done)

    async def _req_tilt(self, request_id: int, msg: dict[str, Any]) -> None:
        deg = _int(msg, "deg", -90, 90)
        if self.faults.uno_offline:
            await self._uno_error("tilt", request_id)
            return
        applied = self.uno.set_tilt(deg)
        self.uno.host_frame(time.monotonic())
        await self._reply("tilt", {"t": "ack", "id": request_id, "deg": applied})

    async def _req_light(self, request_id: int, msg: dict[str, Any]) -> None:
        self.light = _int(msg, "level", 0, 100)
        await self._reply("light", {"t": "ack", "id": request_id, "level": self.light})

    def _check_res(self, msg: dict[str, Any], default: str | None = None) -> str:
        res = msg.get("res", default)
        if not isinstance(res, str) or res not in RESOLUTIONS:
            raise _ArgsError("unknown res")
        if RES_ORDER[res] > RES_ORDER.get(self.config.max_res, 0):
            raise _ArgsError(f"res above {self.config.max_res}")
        return res

    async def _camera_failed(self, kind: str, request_id: int) -> None:
        await self._send({"t": "event", "kind": "camera_error", "msg": "frame capture failed"})
        await self._reply(kind, {"t": "err", "id": request_id, "code": "camera", "msg": "capture failed"})

    async def _frame(self, res: str, q: int) -> bytes:
        scene = self.world.scene()
        return await asyncio.to_thread(
            snapshot,
            scene,
            res,
            q,
            tilt_deg=self.uno.tilt,
            light=self.light,
            vflip=self.vflip,
            hmirror=self.hmirror,
            seed=self._rng.randrange(2**31),
        )

    async def _req_snap(self, request_id: int, msg: dict[str, Any]) -> None:
        res = self._check_res(msg)
        q = _int(msg, "q", 4, 63, default=12)
        delay = SNAP_DELAY_S[res] * self.config.snap_delay_scale
        started = time.monotonic()
        if self.faults.camera_error:
            await asyncio.sleep(delay)
            await self._camera_failed("snap", request_id)
            return
        jpeg = await self._frame(res, q)
        await asyncio.sleep(max(0.0, delay - (time.monotonic() - started)))
        await self._reply("snap", b"SCIM" + request_id.to_bytes(4, "little") + jpeg)

    async def _req_stream(self, request_id: int, msg: dict[str, Any]) -> None:
        on = msg.get("on")
        if not isinstance(on, bool):
            raise _ArgsError("on must be true or false")
        if not on:
            if self._stream is not None:
                self._stream.cancel()
                self._stream = None
            await self._reply("stream", {"t": "ack", "id": request_id})
            return
        res = self._check_res(msg, "qvga")
        q = _int(msg, "q", 4, 63, default=12)
        fps = _int(msg, "fps", 1, 10)
        secs = _int(msg, "secs", 1, 120)
        if self.faults.camera_error:
            await self._camera_failed("stream", request_id)
            return
        if self._stream is not None:
            self._stream.cancel()
        self._stream = self._spawn(self._stream_loop(request_id, res, q, fps, secs))
        await self._reply("stream", {"t": "ack", "id": request_id})

    async def _stream_loop(self, request_id: int, res: str, q: int, fps: int, secs: int) -> None:
        end = time.monotonic() + secs
        period = 1.0 / fps
        next_frame = time.monotonic()
        header = b"SCST" + request_id.to_bytes(4, "little")
        while time.monotonic() < end:
            if self.faults.camera_error:
                await self._send({"t": "event", "kind": "camera_error", "msg": "stream capture failed"})
                return
            jpeg = await self._frame(res, q)
            await self._reply("stream", header + jpeg)
            next_frame += period
            await asyncio.sleep(max(0.0, next_frame - time.monotonic()))

    def _apply_conf(self, conf: dict[str, Any], *, strict: bool) -> None:
        ranges = {"obst_cm": (5, 100), "min_pwm": (0, 255), "link_ms": (300, 5000)}
        updates: dict[str, Any] = {}
        for key, (low, high) in ranges.items():
            if key in conf:
                try:
                    updates[key] = _int(conf, key, low, high)
                except _ArgsError:
                    if strict:
                        raise
        for key in ("vflip", "hmirror"):
            if key in conf:
                if isinstance(conf[key], bool):
                    updates[key] = conf[key]
                elif strict:
                    raise _ArgsError(f"{key} must be true or false")
        if "obst_cm" in updates:
            self.uno.obst_cm = updates["obst_cm"]
        if "min_pwm" in updates:
            self.uno.min_pwm = updates["min_pwm"]
        if "link_ms" in updates:
            self.uno.link_ms = updates["link_ms"]
        if "vflip" in updates:
            self.vflip = updates["vflip"]
        if "hmirror" in updates:
            self.hmirror = updates["hmirror"]

    async def _req_conf(self, request_id: int, msg: dict[str, Any]) -> None:
        self._apply_conf(msg, strict=True)
        await self._reply("conf", {"t": "ack", "id": request_id})

    async def _req_ota(self, request_id: int, msg: dict[str, Any]) -> None:
        url = msg.get("url")
        sha = msg.get("sha256")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise _ArgsError("url must be https")
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
            raise _ArgsError("bad sha256")
        _int(msg, "size", 1, 16 * 1024 * 1024)
        if self.uno.moving or self._ota_running:
            await self._reply(
                "ota",
                {
                    "t": "err",
                    "id": request_id,
                    "code": "busy",
                    "msg": "moving" if self.uno.moving else "update running",
                },
            )
            return
        self._ota_running = True
        try:
            step = self.config.ota_step_s
            for pct in (0, 25, 50, 75, 100):
                await self._reply(
                    "ota", {"t": "ota", "id": request_id, "state": "downloading", "pct": pct, "msg": ""}
                )
                await asyncio.sleep(step)
            await self._reply(
                "ota", {"t": "ota", "id": request_id, "state": "verifying", "pct": 100, "msg": ""}
            )
            await asyncio.sleep(step)
            if self.faults.ota_fail:
                await self._reply(
                    "ota",
                    {"t": "ota", "id": request_id, "state": "failed", "pct": 100, "msg": "sha256 mismatch"},
                )
                return
            await self._reply(
                "ota", {"t": "ota", "id": request_id, "state": "rebooting", "pct": 100, "msg": ""}
            )
            await asyncio.sleep(0.05)
            self.fw = self.config.next_fw or _bump(self.fw)
        finally:
            self._ota_running = False
        await self.restart("ota")

    async def _req_reboot(self, request_id: int, msg: dict[str, Any]) -> None:
        await self._reply("reboot", {"t": "ack", "id": request_id})
        await asyncio.sleep(0.05)
        await self.restart("sw")

    async def _req_ping(self, request_id: int, msg: dict[str, Any]) -> None:
        await self._reply("ping", {"t": "pong", "id": request_id})
