"""Streamable HTTP application assembly and server startup."""

from __future__ import annotations

import logging
from typing import Any

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fuel_prices_mcp.auth import BearerAuthGate, StaticTokenVerifier
from fuel_prices_mcp.config import Settings

logger = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({"/healthz"})

_SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"cache-control", b"no-store"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
    (b"cross-origin-resource-policy", b"same-origin"),
)


class SecurityHeadersMiddleware:
    """Adds conservative security headers to every HTTP response that does not already set them."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers.extend((name, value) for name, value in _SECURITY_HEADERS if name not in present)
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, send_with_headers)


class PostOnlyMiddleware:
    """Answer anything but POST on the MCP endpoint with 405.

    2026-07-28 removed the GET stream and sessions (DELETE); servers SHOULD reply 405.
    Older clients treat 405 on GET as "no standalone stream" and carry on, and it stops a
    caller from parking idle SSE streams on a stateless server.
    """

    def __init__(self, app: ASGIApp, path: str) -> None:
        self._app = app
        self._paths = {path, path + "/"}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] in self._paths and scope["method"] != "POST":
            await send(
                {
                    "type": "http.response.start",
                    "status": 405,
                    "headers": [(b"allow", b"POST"), (b"content-length", b"0")],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        await self._app(scope, receive, send)


def build_http_app(settings: Settings, mcp: MCPServer[Any]) -> ASGIApp:
    # Host/Origin validation (DNS-rebinding protection) is always on, also inside containers
    # where the server must bind 0.0.0.0. Requests with a bad Origin get 403, bad Host 421.
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(settings.allowed_hosts),
        allowed_origins=list(settings.allowed_origins),
    )
    app: ASGIApp = mcp.streamable_http_app(
        streamable_http_path=settings.http_path,
        # No server-side sessions: every request stands alone (2026-07-28 semantics), and
        # older clients get the SDK's stateless legacy path, so there is no session to hijack.
        stateless_http=True,
        max_request_body_size=settings.max_http_body_bytes,
        transport_security=transport_security,
        host=settings.host,
    )
    if settings.auth_mode == "token":
        app = BearerAuthGate(app, StaticTokenVerifier(settings.token_hashes), public_paths=PUBLIC_PATHS)
    elif settings.auth_mode == "none":
        logger.warning("HTTP transport is running WITHOUT authentication (FUEL_MCP_AUTH_MODE=none)")
    return SecurityHeadersMiddleware(PostOnlyMiddleware(app, settings.http_path))


def uvicorn_config(settings: Settings, app: ASGIApp) -> uvicorn.Config:
    return uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_config=None,
        log_level=settings.log_level.lower(),
        server_header=False,
        proxy_headers=settings.forwarded_allow_ips is not None,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        limit_concurrency=settings.http_max_connections,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=10,
        ws="none",
        lifespan="on",
    )


def run_http(settings: Settings, mcp: MCPServer[Any]) -> None:
    config = uvicorn_config(settings, build_http_app(settings, mcp))
    logger.info(
        "Serving MCP over Streamable HTTP",
        extra={
            "event": {
                "host": settings.host,
                "port": settings.port,
                "path": settings.http_path,
                "auth_mode": settings.auth_mode,
            }
        },
    )
    uvicorn.Server(config).run()
