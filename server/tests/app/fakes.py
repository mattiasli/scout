"""Test doubles: a fake robot (implements ``RobotAPI``) and a fake GitHub (an httpx handler).

All secrets here are obviously fake.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import anyio
import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.websockets import WebSocket

from scout.config import Config
from scout.contracts import ActionResult, CarOffline, CarSummary
from scout.db import Store, User

FAKE_SECRET_KEY = "fake-secret-key-for-tests-only-0123456789"
FAKE_CLIENT_ID = "fake-client-id"
FAKE_CLIENT_SECRET = "fake-client-secret"
OWNER_GITHUB_ID = 1001
JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"


def make_config(data_dir: Path, **overrides: Any) -> Config:
    values: dict[str, Any] = {
        "public_url": "http://testserver",
        "secret_key": FAKE_SECRET_KEY,
        "data_dir": data_dir,
        "dev": True,
        "github_client_id": FAKE_CLIENT_ID,
        "github_client_secret": FAKE_CLIENT_SECRET,
        "owner_github_id": OWNER_GITHUB_ID,
    }
    values.update(overrides)
    return Config(**values)


def write_capture(data_dir: Path, capture_id: str, user_id: int, *, photos: int = 2) -> Path:
    folder = data_dir / "captures" / capture_id
    folder.mkdir(parents=True, exist_ok=True)
    names = [f"{i:03d}.jpg" for i in range(1, photos + 1)]
    for name in names:
        (folder / name).write_bytes(JPEG + name.encode())
    meta = {
        "capture_id": capture_id,
        "car_id": "scout-1",
        "user_id": user_id,
        "created": "2026-11-20T10:00:00+00:00",
        "photos": [{"file": n} for n in names],
    }
    (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return folder


class FakeRobot:
    """Records every call. Set ``fail`` to make the next action raise, ``gate`` to hold actions."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.online = True
        self.fail: BaseException | None = None
        self.gate: anyio.Event | None = None
        self.started = False
        self.closed = False
        self._captures = 0

    # lifecycle and car link
    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def handle_car_socket(self, websocket: WebSocket) -> None:
        await websocket.close(code=1008)

    # inspection and admin
    def cars(self) -> list[CarSummary]:
        return [self.car("scout-1")]

    def car(self, car_id: str) -> CarSummary:
        return CarSummary(
            car_id=car_id,
            online=self.online,
            connected_since=1_795_300_000.0 if self.online else None,
            firmware="0.1.0",
            uno_firmware="0.1.0",
            uno_ok=True,
            camera="OV3660",
            psram=4_194_304,
            max_res="qxga",
            ssid="fake-wifi",
            rssi=-58,
            telemetry={"cm": 82, "tilt": 0},
        )

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))
        if self.fail is not None:
            exc, self.fail = self.fail, None
            raise exc

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    async def push_settings(self, car_id: str) -> None:
        self._record("push_settings", car_id=car_id)
        if not self.online:
            raise CarOffline("Scout is offline.")

    async def stop_everything(self, car_id: str) -> None:
        self._record("stop_everything", car_id=car_id)

    async def disconnect(self, car_id: str) -> None:
        self._record("disconnect", car_id=car_id)

    async def update_firmware(self, car_id: str, *, url: str, sha256: str, size: int) -> None:
        self._record("update_firmware", car_id=car_id, url=url, sha256=sha256, size=size)

    async def wheel_test(self, car_id: str, *, bits: int, pwm5: int, pwm6: int, ms: int) -> ActionResult:
        self._record("wheel_test", car_id=car_id, bits=bits, pwm5=pwm5, pwm6=pwm6, ms=ms)
        return ActionResult(data={"reason": "time", "ms": ms})

    def capture_dir(self, capture_id: str) -> Path | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", capture_id):
            return None
        folder = self.data_dir / "captures" / capture_id
        return folder if folder.is_dir() else None

    # tool actions
    async def _act(self, name: str, images: int = 0, **kwargs: Any) -> ActionResult:
        self._record(name, **kwargs)
        if self.gate is not None:
            await self.gate.wait()
        if not self.online:
            raise CarOffline("Scout is offline right now.")
        return ActionResult(data={"action": name, **kwargs}, images=[JPEG] * images)

    async def status(self, car_id: str) -> ActionResult:
        return await self._act("status", car_id=car_id)

    async def look(
        self, car_id: str, *, tilt: int | None = None, light: int | None = None, size: str = "medium"
    ) -> ActionResult:
        return await self._act("look", images=1, car_id=car_id, tilt=tilt, light=light, size=size)

    async def move(
        self, car_id: str, *, direction: str, ms: int, speed: int = 60, look: bool = False
    ) -> ActionResult:
        return await self._act(
            "move", images=1 if look else 0, car_id=car_id, direction=direction, ms=ms, speed=speed, look=look
        )

    async def turn(
        self, car_id: str, *, direction: str, degrees: int, speed: int = 50, look: bool = False
    ) -> ActionResult:
        return await self._act(
            "turn",
            images=1 if look else 0,
            car_id=car_id,
            direction=direction,
            degrees=degrees,
            speed=speed,
            look=look,
        )

    async def drive(
        self, car_id: str, *, vx: int, vy: int, wz: int, ms: int, look: bool = False
    ) -> ActionResult:
        return await self._act(
            "drive", images=1 if look else 0, car_id=car_id, vx=vx, vy=vy, wz=wz, ms=ms, look=look
        )

    async def set_light(self, car_id: str, *, level: int) -> ActionResult:
        return await self._act("set_light", car_id=car_id, level=level)

    async def stop(self, car_id: str) -> ActionResult:
        return await self._act("stop", car_id=car_id)

    async def look_around(self, car_id: str, *, views: int = 6) -> ActionResult:
        return await self._act("look_around", images=views, car_id=car_id, views=views)

    async def capture_photo(
        self, car_id: str, *, user_id: int, capture_id: str | None = None, note: str | None = None
    ) -> ActionResult:
        result = await self._act(
            "capture_photo", car_id=car_id, user_id=user_id, capture_id=capture_id, note=note
        )
        if capture_id is None:
            self._captures += 1
            capture_id = f"cap{self._captures:04d}-fake"
            write_capture(self.data_dir, capture_id, user_id, photos=1)
        return ActionResult(data=result.data, images=[JPEG], capture_id=capture_id)

    async def capture_orbit(
        self, car_id: str, *, user_id: int, photos: int = 12, direction: str = "left"
    ) -> ActionResult:
        result = await self._act(
            "capture_orbit", car_id=car_id, user_id=user_id, photos=photos, direction=direction
        )
        self._captures += 1
        capture_id = f"orbit{self._captures:04d}"
        write_capture(self.data_dir, capture_id, user_id, photos=photos)
        return ActionResult(data=result.data, images=[JPEG, JPEG], capture_id=capture_id)

    async def find(self, car_id: str, *, target: str | None = None, size: str = "medium") -> ActionResult:
        return await self._act("find", images=1, car_id=car_id, target=target, size=size)

    async def follow(
        self,
        car_id: str,
        *,
        target: str = "cat",
        seconds: int = 60,
        pick: int | None = None,
        distance_cm: int | None = None,
    ) -> ActionResult:
        return await self._act(
            "follow",
            images=1,
            car_id=car_id,
            target=target,
            seconds=seconds,
            pick=pick,
            distance_cm=distance_cm,
        )

    async def follow_view(self, car_id: str, *, wait_s: int = 0) -> ActionResult:
        return await self._act("follow_view", images=2, car_id=car_id, wait_s=wait_s)


class FakeGitHub:
    """Answers GitHub's token and user endpoints, checking the client secret and PKCE."""

    def __init__(self) -> None:
        self.profiles: dict[str, dict[str, Any]] = {}  # code -> profile
        self.challenges: dict[str, str] = {}  # code -> code_challenge the browser was sent with
        self.token_requests: list[dict[str, str]] = []
        self.fail_token = False

    def add_user(self, code: str, github_id: int, login: str, name: str | None = None) -> None:
        self.profiles[code] = {
            "id": github_id,
            "login": login,
            "name": name,
            "avatar_url": f"https://avatars.githubusercontent.com/u/{github_id}?v=4",
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "github.com" and url.path == "/login/oauth/access_token":
            form = dict(parse_qsl(request.content.decode()))
            self.token_requests.append(form)
            code = form.get("code", "")
            verifier = form.get("code_verifier", "")
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            )
            if (
                self.fail_token
                or code not in self.profiles
                or form.get("client_id") != FAKE_CLIENT_ID
                or form.get("client_secret") != FAKE_CLIENT_SECRET
                or (code in self.challenges and self.challenges[code] != challenge)
            ):
                return httpx.Response(200, json={"error": "bad_verification_code"})
            return httpx.Response(
                200, json={"access_token": f"gho_fake_{code}", "token_type": "bearer", "scope": ""}
            )
        if url.host == "api.github.com" and url.path == "/user":
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            code = token.removeprefix("gho_fake_")
            if code not in self.profiles:
                return httpx.Response(401, json={"message": "Bad credentials"})
            return httpx.Response(200, json=self.profiles[code])
        return httpx.Response(404)


def authorize_params(location: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(location).query))


CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')


def csrf_from(html: str) -> str:
    match = CSRF_RE.search(html)
    assert match, "no CSRF token on the page"
    return match.group(1)


def add_user(
    store: Store, github_id: int, login: str, *, status: str = "active", admin: bool = False
) -> User:
    """Create an account the way sign-in does, then approve it (and make it admin) as the owner."""
    user, _ = store.sign_in_github_user(github_id=github_id, login=login, name=None, avatar_url=None)
    owner = store.ensure_owner()
    assert owner is not None
    if status != "waiting":
        user = store.set_status(user.id, "active", by=owner)
    if admin:
        user = store.set_admin(user.id, True, by=owner)
    if status == "suspended":
        user = store.set_status(user.id, "suspended", by=owner)
    return user


@asynccontextmanager
async def mcp_client(
    app: Any, *, token: str | None = None, path_token: str | None = None, mode: str = "legacy"
) -> AsyncIterator[Client]:
    """A real MCP client talking Streamable HTTP to the ASGI app (no network)."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver", headers=headers, timeout=30
    )
    url = "http://testserver/mcp" + (f"/{path_token}" if path_token else "")
    async with http, Client(streamable_http_client(url, http_client=http), mode=mode) as client:
        yield client
