"""The Scout server as one Starlette app: website, MCP endpoint and the car's WebSocket."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import BaseRoute, Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.websockets import WebSocket

from scout.captures import CaptureLinks
from scout.config import Config
from scout.context import Scout
from scout.contracts import CarSettings, RobotAPI
from scout.db import Store
from scout.driver import DriverLock
from scout.github import GitHubOAuth
from scout.images import ImageStore
from scout.mcp_endpoint import McpEndpoint, build_mcp_server, new_session_manager
from scout.security import SecurityHeadersMiddleware, install_log_redaction
from scout.service import ToolService
from scout.web import routes as web_routes
from scout.web.templating import make_templates

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
BOOTSTRAP_CAR_ID = "scout-1"


def _build_robot(config: Config, store: Store) -> RobotAPI:
    from scout.robot import Robot  # imported here so the app side never needs robot internals

    async def authenticate_car(key: str) -> str | None:
        return store.car_id_for_key(key)

    async def car_settings(car_id: str) -> CarSettings:
        car = store.get_car(car_id)
        return car.settings if car is not None else CarSettings()

    robot: RobotAPI = Robot(
        data_dir=config.data_dir,
        authenticate_car=authenticate_car,
        car_settings=car_settings,
        allow_insecure=config.dev,
    )
    return robot


def create_app(
    config: Config | None = None,
    *,
    robot: RobotAPI | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> Starlette:
    """Build the app. Pass ``robot`` (for example a fake) and ``http_client`` in tests."""
    config = config or Config.from_env()
    install_log_redaction()
    config.data_dir.mkdir(parents=True, exist_ok=True)

    store = Store(config.db_path, owner_github_id=config.owner_github_id)
    if config.owner_github_id is not None:
        store.ensure_owner()
    if config.bootstrap_car_key:
        store.ensure_car_key(BOOTSTRAP_CAR_ID, config.bootstrap_car_key)
    if robot is None:
        robot = _build_robot(config, store)

    http = http_client or httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "scout-server"})
    github = (
        GitHubOAuth(
            config.github_client_id or "",
            config.github_client_secret or "",
            f"{config.public_url}/auth/github/callback",
            http,
        )
        if config.github_configured
        else None
    )
    lock = DriverLock()
    images = ImageStore()
    capture_links = CaptureLinks(config.public_url, config.secret_key)
    tools = ToolService(
        store=store,
        robot=robot,
        lock=lock,
        images=images,
        capture_links=capture_links,
        public_url=config.public_url,
    )
    templates: Jinja2Templates = make_templates(WEB_DIR / "templates", config)
    scout = Scout(
        config=config,
        store=store,
        robot=robot,
        lock=lock,
        images=images,
        capture_links=capture_links,
        tools=tools,
        mcp_server=build_mcp_server(tools, website_url=config.public_url),
        templates=templates,
        http=http,
        owns_http=http_client is None,
        github=github,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await scout.robot.start()
        try:
            scout.mcp_manager = new_session_manager(scout.mcp_server, config)
            async with scout.mcp_manager.run():
                log.info("Scout is up at %s", config.public_url)
                yield
        finally:
            scout.mcp_manager = None
            await scout.robot.close()
            if scout.owns_http:
                await scout.http.aclose()
            scout.store.close()

    async def car_socket(websocket: WebSocket) -> None:
        await scout.robot.handle_car_socket(websocket)

    mcp = McpEndpoint(
        resolve=scout.resolve_token,
        manager=lambda: scout.mcp_manager,
        account_url=f"{config.public_url}/account",
    )
    routes: list[BaseRoute] = [
        Route("/mcp", endpoint=mcp),
        Route("/mcp/", endpoint=mcp),
        Route("/mcp/{token}", endpoint=mcp),
        WebSocketRoute("/car/ws", endpoint=car_socket),
        Mount("/static", app=StaticFiles(directory=WEB_DIR / "static"), name="static"),
        *web_routes.routes(),
    ]
    cookie = "__Host-scout" if config.secure_cookies else "scout_session"
    app = Starlette(
        debug=False,
        routes=routes,
        middleware=[
            Middleware(SecurityHeadersMiddleware, hsts=config.secure_cookies),
            Middleware(
                SessionMiddleware,
                secret_key=config.secret_key,
                session_cookie=cookie,
                max_age=14 * 24 * 3600,
                same_site="lax",
                https_only=config.secure_cookies,
            ),
        ],
        exception_handlers=web_routes.exception_handlers(),
        lifespan=lifespan,
        max_body_size=2 * 1024 * 1024,
    )
    app.state.scout = scout
    return app
