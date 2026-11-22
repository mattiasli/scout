"""The MCP endpoint: Streamable HTTP at ``/mcp`` (token in the Authorization header) and
``/mcp/<token>`` (token in the path, for clients that only take a URL, such as Grok Bot).

Built on the MCP SDK's low-level ``Server`` (handlers for ``tools/list`` and
``tools/call``) and a ``StreamableHTTPSessionManager`` in stateless mode: every POST
stands alone, so a server restart or redeploy never strands a client with a dead
session, and nothing per client is kept in memory. Responses are SSE streams, which keep
long calls such as ``follow`` alive behind proxies.

The token is checked here, before the SDK sees the request. The SDK only ever sees the
path ``/mcp``, so the token never reaches its logs.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import mcp_types as types
from mcp.server import Server, ServerRequestContext
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.types import Receive, Scope, Send

from scout import __version__
from scout.config import Config
from scout.service import Caller, ToolService, text_result
from scout.tools import INSTRUCTIONS, TOOLS, TOOLS_BY_NAME

CALLER_SCOPE_KEY = "scout.caller"


def build_mcp_server(service: ToolService, *, website_url: str) -> Server[Any]:
    tools = [spec.to_tool() for spec in TOOLS]

    async def list_tools(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def call_tool(
        ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        caller = caller_from_context(ctx)
        if caller is None:  # the endpoint always sets it; this is a guard, not a code path
            return text_result("Scout couldn't tell who you are. Check your token.", error=True)
        return await service.call(caller, params.name, params.arguments)

    def input_schema(name: str) -> dict[str, Any] | None:
        spec = TOOLS_BY_NAME.get(name)
        return spec.input_schema if spec is not None else None

    return Server(
        "scout",
        version=__version__,
        title="Scout",
        description="See through and drive a small mecanum robot car.",
        instructions=INSTRUCTIONS,
        website_url=website_url,
        get_tool_input_schema=input_schema,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def caller_from_context(ctx: ServerRequestContext[Any]) -> Caller | None:
    request = ctx.request
    if isinstance(request, Request):
        caller = request.scope.get(CALLER_SCOPE_KEY)
        if isinstance(caller, Caller):
            return caller
    return None


def transport_security(config: Config) -> TransportSecuritySettings:
    """DNS-rebinding protection: only the public host (plus localhost in development)."""
    hosts = [config.public_host, f"{config.public_host}:*"]
    origins = [config.public_url]
    if config.dev:
        hosts += ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]:*"]
        origins += ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )


def new_session_manager(server: Server[Any], config: Config) -> StreamableHTTPSessionManager:
    # A session manager can only run once, so the app makes a new one in every lifespan.
    return StreamableHTTPSessionManager(
        app=server,
        stateless=True,
        json_response=False,
        security_settings=transport_security(config),
        max_request_body_size=1024 * 1024,
    )


def _bearer(scope: Scope) -> str | None:
    for key, value in scope.get("headers", []):
        if key == b"authorization":
            text = value.decode("latin-1").strip()
            if text[:7].lower() == "bearer ":
                return text[7:].strip() or None
            return None
    return None


async def _send_json(
    send: Send, status: int, body: dict[str, Any], headers: list[tuple[bytes, bytes]]
) -> None:
    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
                (b"cache-control", b"no-store"),
                *headers,
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class McpEndpoint:
    """ASGI app for ``/mcp``, ``/mcp/`` and ``/mcp/{token}``."""

    def __init__(
        self,
        *,
        resolve: Callable[[str], Caller | None],
        manager: Callable[[], StreamableHTTPSessionManager | None],
        account_url: str,
    ) -> None:
        self._resolve = resolve
        self._manager = manager
        self._account_url = account_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover - routes only send HTTP here
            return
        path_token = scope.get("path_params", {}).get("token")
        token = path_token or _bearer(scope)
        caller = self._resolve(token) if token else None
        if caller is None:
            error = "invalid_token" if token else "missing_token"
            description = (
                "This Scout token isn't valid (it may have been revoked)."
                if token
                else "Scout needs a token: send Authorization: Bearer <token>, or use /mcp/<token>."
            )
            await _send_json(
                send,
                401,
                {"error": error, "error_description": f"{description} Get one at {self._account_url}"},
                [(b"www-authenticate", f'Bearer realm="Scout", error="{error}"'.encode())],
            )
            return
        manager = self._manager()
        if manager is None:
            await _send_json(
                send, 503, {"error": "starting", "error_description": "Scout is starting up."}, []
            )
            return
        child = dict(scope)
        child["path"] = "/mcp"
        child["raw_path"] = b"/mcp"
        child["path_params"] = {}
        child[CALLER_SCOPE_KEY] = caller
        # Lets the SDK tie any stateful session to the credential that opened it.
        child["user"] = AuthenticatedUser(
            AccessToken(
                token="",
                client_id=f"scout-token-{caller.token_id if caller.token_id is not None else 'bootstrap'}",
                scopes=[],
                subject=str(caller.user_id),
            )
        )
        await manager.handle_request(child, receive, send)
