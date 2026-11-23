"""The real HTTP stack (uvicorn + Starlette + SDK) with authentication and transport protections."""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from fixtures import FakeUpstream, free_port
from fuel_prices_mcp.auth import generate_token
from fuel_prices_mcp.config import OAuthSettings, Settings
from fuel_prices_mcp.http import build_http_app, uvicorn_config
from fuel_prices_mcp.server import build_server
from fuel_prices_mcp.store import FuelData

pytestmark = pytest.mark.anyio

PROTOCOL = "2026-07-28"


@dataclasses.dataclass
class Running:
    base: str
    token: str | None


def _serve(settings: Settings) -> Iterator[Running]:
    data = FuelData(settings, transport=FakeUpstream().transport())
    server = uvicorn.Server(uvicorn_config(settings, build_http_app(settings, build_server(settings, data))))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    try:
        yield Running(base=f"http://127.0.0.1:{settings.port}", token=None)
    finally:
        server.should_exit = True
        thread.join(10)


@pytest.fixture
def token_server(settings: Settings) -> Iterator[Running]:
    token, digest = generate_token()
    http_settings = dataclasses.replace(
        settings,
        transport="http",
        port=free_port(),
        auth_mode="token",
        token_hashes={bytes.fromhex(digest): "test-client"},
    )
    for running in _serve(http_settings):
        running.token = token
        yield running


def _rpc(method: str, params: dict[str, Any] | None = None, request_id: int = 1) -> dict[str, Any]:
    params = dict(params or {})
    params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "pytest", "version": "1"},
    }
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def _headers(method: str, token: str | None = None, name: str | None = None, **extra: str) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL,
        "Mcp-Method": method,
    }
    if name:
        headers["Mcp-Name"] = name
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers.update(extra)
    return headers


def _post(running: Running, body: Any, headers: dict[str, str]) -> httpx2.Response:
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return httpx2.post(f"{running.base}/mcp", content=content, headers=headers, timeout=10)


def _json_from(response: httpx2.Response) -> dict[str, Any]:
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data_lines = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
        return json.loads(data_lines[-1])
    return response.json()


def test_health_endpoint_is_public_and_minimal(token_server: Running) -> None:
    health = httpx2.get(f"{token_server.base}/healthz")
    assert health.status_code == 200 and health.text == "ok"
    assert health.headers["x-content-type-options"] == "nosniff"
    assert health.headers["cache-control"] == "no-store"
    assert "server" not in health.headers


def test_missing_or_wrong_token_is_401(token_server: Running) -> None:
    body = _rpc("tools/list")
    missing = _post(token_server, body, _headers("tools/list"))
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"].startswith("Bearer")
    wrong = _post(token_server, body, _headers("tools/list", token="fpmcp_wrong"))
    assert wrong.status_code == 401
    # Any other path is denied too: authentication is deny-by-default, not per-route.
    assert httpx2.get(f"{token_server.base}/anything").status_code == 401


def test_valid_token_lists_tools(token_server: Running) -> None:
    response = _post(token_server, _rpc("tools/list"), _headers("tools/list", token=token_server.token))
    assert response.status_code == 200
    payload = _json_from(response)
    assert payload["result"]["resultType"] == "complete"
    assert [tool["name"] for tool in payload["result"]["tools"]] == [
        "fuel_find_cheapest",
        "fuel_nearby_stations",
        "fuel_area_summary",
        "fuel_data_status",
    ]
    assert payload["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "fuel-prices-mcp"


def test_hostile_origin_is_403(token_server: Running) -> None:
    response = _post(
        token_server,
        _rpc("tools/list"),
        _headers("tools/list", token=token_server.token, Origin="https://evil.example"),
    )
    assert response.status_code == 403


def test_unexpected_host_is_rejected(token_server: Running) -> None:
    response = _post(
        token_server,
        _rpc("tools/list"),
        _headers("tools/list", token=token_server.token, Host="attacker.example"),
    )
    assert response.status_code == 421


def test_oversized_body_is_413(token_server: Running, settings: Settings) -> None:
    huge = b"{" + b" " * (settings.max_http_body_bytes + 10) + b"}"
    response = _post(
        token_server, huge, _headers("tools/call", token=token_server.token, name="fuel_data_status")
    )
    assert response.status_code == 413


def test_header_body_mismatch_is_rejected(token_server: Running) -> None:
    body = _rpc("tools/call", {"name": "fuel_data_status", "arguments": {}})
    response = _post(
        token_server, body, _headers("tools/call", token=token_server.token, name="fuel_find_cheapest")
    )
    assert response.status_code == 400
    assert _json_from(response)["error"]["code"] == -32020  # HeaderMismatch


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT"])
def test_only_post_is_served(token_server: Running, method: str) -> None:
    response = httpx2.request(
        method,
        f"{token_server.base}/mcp",
        headers={"Authorization": f"Bearer {token_server.token}", "Accept": "text/event-stream"},
        timeout=5,
    )
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"


def test_no_session_ids_are_minted(token_server: Running) -> None:
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token_server.token}",
    }
    response = httpx2.post(f"{token_server.base}/mcp", json=initialize, headers=headers, timeout=10)
    assert response.status_code == 200
    assert "mcp-session-id" not in response.headers


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_sdk_client_round_trip(token_server: Running, mode: str) -> None:
    """Both a 2026-07-28 client and a legacy (initialize-handshake) client can use the tools."""
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token_server.token}"}, timeout=30
    ) as http:
        transport = streamable_http_client(f"{token_server.base}/mcp", http_client=http)
        async with Client(transport, mode=mode) as client:  # type: ignore[arg-type]
            result = await client.call_tool("fuel_find_cheapest", {"location": "LL57 1AA"})
            if mode == "auto":
                assert client.protocol_version == PROTOCOL
    assert result.structured_content["results"][0]["name"] == "SuperMart Bangor"


# -- OAuth resource-server mode ----------------------------------------------------------------------


@pytest.fixture
def oauth_server(settings: Settings) -> Iterator[Running]:
    port = free_port()
    oauth = OAuthSettings(
        issuer="https://auth.example.com",
        jwks_url="https://auth.example.com/jwks",
        resource_url=f"http://127.0.0.1:{port}/mcp",
        required_scopes=("fuelprices:read",),
        algorithms=("RS256",),
        leeway_seconds=0,
    )
    yield from _serve(
        dataclasses.replace(settings, transport="http", port=port, auth_mode="oauth", oauth=oauth)
    )


def test_oauth_protected_resource_metadata(oauth_server: Running) -> None:
    unauth = _post(oauth_server, _rpc("tools/list"), _headers("tools/list"))
    assert unauth.status_code == 401
    assert "resource_metadata=" in unauth.headers["www-authenticate"]

    metadata = httpx2.get(f"{oauth_server.base}/.well-known/oauth-protected-resource/mcp").json()
    assert metadata["resource"] == f"{oauth_server.base}/mcp"
    assert metadata["authorization_servers"] in (["https://auth.example.com/"], ["https://auth.example.com"])
    assert metadata["scopes_supported"] == ["fuelprices:read"]
