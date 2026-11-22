"""Everything the app side shares between requests, kept on ``app.state.scout``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.templating import Jinja2Templates

from scout.captures import CaptureLinks
from scout.config import Config
from scout.contracts import RobotAPI
from scout.db import Store
from scout.driver import DriverLock
from scout.github import GitHubOAuth
from scout.images import ImageStore
from scout.security import same_secret
from scout.service import Caller, ToolService


@dataclass
class Scout:
    config: Config
    store: Store
    robot: RobotAPI
    lock: DriverLock
    images: ImageStore
    capture_links: CaptureLinks
    tools: ToolService
    mcp_server: Server[Any]
    templates: Jinja2Templates
    http: httpx.AsyncClient
    owns_http: bool
    github: GitHubOAuth | None
    mcp_manager: StreamableHTTPSessionManager | None = None
    _bootstrap_user_id: int | None = field(default=None, repr=False)

    def resolve_token(self, plain: str) -> Caller | None:
        """Who a personal MCP token (or the bootstrap token) belongs to, or None."""
        bootstrap = self.config.bootstrap_token
        if bootstrap is not None and same_secret(plain, bootstrap):
            if self._bootstrap_user_id is None:
                owner = self.store.ensure_owner()
                if owner is None:  # pragma: no cover - Config requires an owner for the bootstrap token
                    return None
                self._bootstrap_user_id = owner.id
            return Caller(user_id=self._bootstrap_user_id, token_id=None)
        token = self.store.find_token(plain)
        if token is None:
            return None
        return Caller(user_id=token.user_id, token_id=token.id)

    def mcp_url(self) -> str:
        return f"{self.config.public_url}/mcp"
