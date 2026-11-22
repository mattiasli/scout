from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from scout.app import create_app
from scout.context import Scout

from .fakes import FakeGitHub, FakeRobot, authorize_params, csrf_from, make_config


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def robot(tmp_path: Path) -> FakeRobot:
    return FakeRobot(tmp_path)


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def make_app(tmp_path: Path, robot: FakeRobot, github: FakeGitHub) -> Callable[..., Starlette]:
    def factory(**overrides: Any) -> Starlette:
        config = make_config(tmp_path, **overrides)
        http = httpx.AsyncClient(transport=httpx.MockTransport(github.handler))
        return create_app(config, robot=robot, http_client=http)

    return factory


@pytest.fixture
def app(make_app: Callable[..., Starlette]) -> Starlette:
    return make_app()


@pytest.fixture
def scout(app: Starlette) -> Scout:
    state: Scout = app.state.scout
    return state


@pytest.fixture
def client(app: Starlette) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
async def running_app(app: Starlette) -> AsyncIterator[Starlette]:
    """The app with its lifespan running, for tests that talk to it through httpx2/MCP."""
    async with app.router.lifespan_context(app):
        yield app


SignIn = Callable[..., str]


@pytest.fixture
def sign_in(github: FakeGitHub) -> SignIn:
    """Sign in through the real OAuth routes (GitHub is faked). Returns the page's CSRF token."""

    def do(client: TestClient, github_id: int, login: str, name: str | None = None) -> str:
        client.cookies.clear()
        code = f"fake-code-{github_id}-{secrets.token_hex(4)}"
        github.add_user(code, github_id, login, name)
        response = client.get("/login", follow_redirects=False)
        assert response.status_code == 302, response.text
        params = authorize_params(response.headers["location"])
        github.challenges[code] = params["code_challenge"]
        response = client.get(
            "/auth/github/callback", params={"code": code, "state": params["state"]}, follow_redirects=False
        )
        assert response.status_code == 303, response.text
        return csrf_from(client.get("/account").text)

    return do
