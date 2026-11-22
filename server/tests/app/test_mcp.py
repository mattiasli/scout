"""The MCP endpoint, exercised with the real MCP client over Streamable HTTP (in-process ASGI)."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable

import httpx2
import mcp_types as types
import pytest
from starlette.applications import Starlette

from scout.context import Scout

from .fakes import JPEG, OWNER_GITHUB_ID, FakeRobot, add_user, mcp_client

pytestmark = pytest.mark.anyio

TOOL_NAMES = {
    "status", "look", "move", "turn", "drive", "set_light", "stop", "look_around",
    "capture_photo", "capture_orbit", "find", "follow", "follow_view", "release_car",
}  # fmt: skip
SERVER_LOGGERS = ("scout", "mcp.server", "mcp.shared", "uvicorn", "starlette")


def _setup(app: Starlette, *, availability: str = "everyone") -> tuple[Scout, str]:
    """An approved user with a token, and a car open to everyone."""
    scout: Scout = app.state.scout
    user = add_user(scout.store, 500, "driver")
    scout.store.add_car("scout-1", "Scout")
    scout.store.set_availability("scout-1", availability)  # type: ignore[arg-type]
    _, token = scout.store.create_token(user.id, "tests")
    return scout, token


def _json(result: types.CallToolResult) -> dict:
    first = result.content[0]
    assert isinstance(first, types.TextContent)
    data = json.loads(first.text)
    assert isinstance(data, dict)
    return data


def _text(result: types.CallToolResult) -> str:
    first = result.content[0]
    assert isinstance(first, types.TextContent)
    return first.text


async def test_lists_exactly_the_tools_with_schemas(running_app: Starlette) -> None:
    _, token = _setup(running_app)
    async with mcp_client(running_app, token=token) as client:
        listed = await client.list_tools()
    tools = {t.name: t for t in listed.tools}
    assert set(tools) == TOOL_NAMES
    move = tools["move"].input_schema
    assert move["required"] == ["direction", "ms"]
    assert move["properties"]["ms"]["minimum"] == 100 and move["properties"]["ms"]["maximum"] == 3000
    assert "forward_left" in move["properties"]["direction"]["enum"]
    assert move["properties"]["speed"]["default"] == 60
    assert tools["look"].input_schema["properties"]["size"]["enum"] == ["small", "medium", "large"]
    for tool in tools.values():
        assert (
            "car" not in tool.input_schema["properties"] and "car_id" not in tool.input_schema["properties"]
        )
        assert tool.description and len(tool.description) > 40
    assert "sideways" in (tools["move"].description or "")
    assert "gyroscope" in (tools["turn"].description or "")


async def test_status_with_bearer_header(running_app: Starlette, robot: FakeRobot) -> None:
    _, token = _setup(running_app)
    async with mcp_client(running_app, token=token, mode="legacy") as client:
        result = await client.call_tool("status", {})
    assert not result.is_error
    data = _json(result)
    assert data["action"] == "status" and data["driver"] is None
    assert robot.names() == ["status"]


async def test_token_in_the_path_works_and_never_reaches_the_logs(
    running_app: Starlette, robot: FakeRobot, caplog: pytest.LogCaptureFixture
) -> None:
    _, token = _setup(running_app)
    caplog.set_level(logging.DEBUG)
    async with mcp_client(running_app, path_token=token, mode="legacy") as client:
        result = await client.call_tool("move", {"direction": "forward", "ms": 800})
    async with mcp_client(running_app, path_token=token, mode="auto") as client:
        assert not (await client.call_tool("status", {})).is_error
    assert not result.is_error, _text(result)
    assert robot.calls[0] == (
        "move",
        {"car_id": "scout-1", "direction": "forward", "ms": 800, "speed": 60, "look": False},
    )
    # The test's own HTTP client logs its URL; nothing on the server side may.
    server_side = [r for r in caplog.records if r.name.startswith(SERVER_LOGGERS)]
    assert server_side, "expected some server-side log records"
    assert all(token not in record.getMessage() for record in server_side)


async def test_modern_protocol_version_works(running_app: Starlette) -> None:
    _, token = _setup(running_app)
    async with mcp_client(running_app, token=token, mode="2026-07-28") as client:
        result = await client.call_tool("status", {})
    assert not result.is_error


async def test_look_returns_an_image_and_a_link(running_app: Starlette) -> None:
    _, token = _setup(running_app)
    async with mcp_client(running_app, token=token) as client:
        result = await client.call_tool("look", {"tilt": 10, "size": "small"})
    assert not result.is_error
    images = [c for c in result.content if isinstance(c, types.ImageContent)]
    assert len(images) == 1
    assert images[0].mime_type == "image/jpeg"
    assert base64.b64decode(images[0].data) == JPEG
    data = _json(result)
    assert data["tilt"] == 10 and data["size"] == "small" and data["light"] is None
    link = data["image_links"][0]
    assert link.startswith("http://testserver/i/")
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=running_app), base_url="http://testserver"
    ) as h:
        response = await h.get(link.removeprefix("http://testserver"))
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == JPEG


async def test_look_around_returns_one_image_per_view(running_app: Starlette) -> None:
    _, token = _setup(running_app)
    async with mcp_client(running_app, token=token) as client:
        result = await client.call_tool("look_around", {"views": 4})
    assert len([c for c in result.content if isinstance(c, types.ImageContent)]) == 4
    assert len(_json(result)["image_links"]) == 4


async def test_capture_photo_passes_the_user_and_links_a_download(
    running_app: Starlette, robot: FakeRobot
) -> None:
    scout, token = _setup(running_app)
    user = scout.store.get_user_by_github_id(500)
    assert user is not None
    async with mcp_client(running_app, token=token) as client:
        result = await client.call_tool("capture_photo", {"note": "the red mug"})
    assert not result.is_error
    assert robot.calls[-1][1]["user_id"] == user.id
    data = _json(result)
    assert data["capture_id"] == "cap0001-fake"
    download = data["capture_download"]
    assert download.startswith("http://testserver/captures/cap0001-fake.zip?exp=")
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=running_app), base_url="http://testserver"
    ) as h:
        response = await h.get(download.removeprefix("http://testserver"))
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.content[:2] == b"PK"


async def test_invalid_arguments_are_tool_errors(running_app: Starlette, robot: FakeRobot) -> None:
    _, token = _setup(running_app)
    async with mcp_client(running_app, token=token) as client:
        too_long = await client.call_tool("move", {"direction": "forward", "ms": 9000})
        bad_enum = await client.call_tool("move", {"direction": "up", "ms": 500})
        extra = await client.call_tool("status", {"car": "scout-2"})
    assert too_long.is_error and "between 100 and 3000" in _text(too_long)
    assert bad_enum.is_error and "one of" in _text(bad_enum)
    assert extra.is_error and "doesn't take car" in _text(extra)
    assert robot.calls == []


async def test_bootstrap_token_acts_as_the_owner(make_app: Callable[..., Starlette]) -> None:
    bootstrap = "fake-bootstrap-token-for-tests-0123456789"
    app = make_app(bootstrap_token=bootstrap)
    scout: Scout = app.state.scout
    scout.store.add_car("scout-1", "Scout")  # admins only by default; the owner is an admin
    owner = scout.store.get_user_by_github_id(OWNER_GITHUB_ID)
    assert owner is not None and owner.admin
    async with app.router.lifespan_context(app):
        async with mcp_client(app, token=bootstrap) as client:
            result = await client.call_tool("stop", {})
        assert not result.is_error, _text(result)
        assert scout.store.recent_activity(limit=1)[0].user_id == owner.id


async def _raw_post(app: Starlette, path: str, headers: dict[str, str]) -> httpx2.Response:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://testserver") as h:
        return await h.post(
            path,
            json=body,
            headers={"Accept": "application/json, text/event-stream", **headers},
        )


async def test_missing_or_unknown_tokens_get_401(running_app: Starlette) -> None:
    scout, token = _setup(running_app)
    missing = await _raw_post(running_app, "/mcp", {})
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"].startswith("Bearer")
    assert "account" in missing.json()["error_description"]

    unknown = await _raw_post(running_app, "/mcp", {"Authorization": "Bearer scout_" + "x" * 43})
    assert unknown.status_code == 401 and unknown.json()["error"] == "invalid_token"

    ok = await _raw_post(running_app, "/mcp", {"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200

    user = scout.store.get_user_by_github_id(500)
    assert user is not None
    token_id = scout.store.list_tokens(user.id)[0].id
    scout.store.revoke_token(user.id, token_id)
    revoked = await _raw_post(running_app, f"/mcp/{token}", {})
    assert revoked.status_code == 401
    assert token not in revoked.text


async def test_wrong_host_is_refused(running_app: Starlette) -> None:
    _, token = _setup(running_app)
    response = await _raw_post(
        running_app, "/mcp", {"Authorization": f"Bearer {token}", "Host": "evil.example"}
    )
    assert response.status_code == 421


async def test_foreign_origin_is_refused(running_app: Starlette) -> None:
    _, token = _setup(running_app)
    response = await _raw_post(
        running_app, "/mcp", {"Authorization": f"Bearer {token}", "Origin": "https://evil.example"}
    )
    assert response.status_code == 403


def test_access_log_lines_are_redacted() -> None:
    from scout.security import RedactingFilter

    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "POST", "/mcp/scout_FAKEFAKEFAKE", "1.1", 200), None,
    )  # fmt: skip
    RedactingFilter().filter(record)
    assert "scout_FAKEFAKEFAKE" not in record.getMessage()
    assert "/mcp/<redacted>" in record.getMessage()
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/captures/x.zip?exp=1&sig=SIGNATURE", "1.1", 200), None,
    )  # fmt: skip
    RedactingFilter().filter(record)
    assert "SIGNATURE" not in record.getMessage()
