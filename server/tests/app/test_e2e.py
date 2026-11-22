"""End to end: the real Robot, the simulated car over a real WebSocket, and an MCP client.

Skipped until the robot side (``scout.robot.Robot``) and the simulator (``scout_sim``) exist.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

import anyio
import httpx2
import mcp_types as types
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

robot_module = pytest.importorskip("scout.robot")
pytest.importorskip("scout_sim")
if not hasattr(robot_module, "Robot") or importlib.util.find_spec("scout_sim.__main__") is None:
    pytest.skip("the robot side or the simulator isn't there yet", allow_module_level=True)

from scout.app import create_app  # noqa: E402
from scout.config import Config  # noqa: E402

pytestmark = pytest.mark.anyio

BOOTSTRAP_TOKEN = "fake-e2e-bootstrap-token-0123456789abcdef"
CAR_KEY = "fake-e2e-car-key-0123456789abcdef"


def _open_log(path: Path) -> IO[bytes]:
    return path.open("wb")


def _tail(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-3000:]
    except OSError:
        return ""


async def _wait_until(predicate: Callable[[], bool], seconds: float) -> None:
    """Poll something we can't await (another process, uvicorn's start-up flag)."""
    with anyio.fail_after(seconds):
        for _ in range(int(seconds * 20) + 1):
            if predicate():
                return
            await anyio.sleep(0.05)
    raise TimeoutError


def _text(result: types.CallToolResult) -> str:
    first = result.content[0]
    assert isinstance(first, types.TextContent)
    return first.text


async def test_simulated_car_end_to_end(tmp_path: Path) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = Config(
        public_url=f"http://127.0.0.1:{port}",
        secret_key="fake-e2e-secret-key-0123456789abcdefghij",
        data_dir=tmp_path / "data",
        dev=True,
        owner_github_id=1,
        bootstrap_token=BOOTSTRAP_TOKEN,
        bootstrap_car_key=CAR_KEY,
    )
    app = create_app(config)
    server = uvicorn.Server(uvicorn.Config(app, lifespan="on", log_level="warning"))
    sim_log = tmp_path / "sim.log"
    log_file = _open_log(sim_log)
    sim: Any = None
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.serve, [sock])
            await _wait_until(lambda: server.started, 15)

            command = [
                sys.executable,
                "-m",
                "scout_sim",
                "--url",
                f"ws://127.0.0.1:{port}/car/ws",
                "--key",
                CAR_KEY,
            ]
            sim = await anyio.open_process(command, stdout=log_file, stderr=subprocess.STDOUT)
            robot = app.state.scout.robot

            def online() -> bool:
                summary = robot.car("scout-1")
                return bool(summary and summary.online)

            try:
                await _wait_until(online, 20)
            except TimeoutError:
                pytest.fail(f"The simulated car never came online. Simulator output:\n{_tail(sim_log)}")

            http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"}, timeout=60)
            url = f"http://127.0.0.1:{port}/mcp"
            async with http, Client(streamable_http_client(url, http_client=http), mode="legacy") as client:
                status = await client.call_tool("status", {})
                assert not status.is_error, _text(status)
                assert isinstance(json.loads(_text(status)), dict)

                look = await client.call_tool("look", {"size": "small"})
                assert not look.is_error, _text(look)
                assert any(isinstance(c, types.ImageContent) for c in look.content)

                move = await client.call_tool("move", {"direction": "forward", "ms": 300})
                assert not move.is_error, _text(move)

            server.should_exit = True
    finally:
        if sim is not None and sim.returncode is None:
            sim.terminate()
            with anyio.move_on_after(5):
                await sim.wait()
            if sim.returncode is None:
                sim.kill()
        log_file.close()
        sock.close()
