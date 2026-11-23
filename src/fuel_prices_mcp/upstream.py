"""Shared plumbing for outbound HTTP calls (Fuel Finder and postcodes.io).

* Only fixed, operator-configured hosts are contacted. No URL, host or path
  segment is taken from tool arguments except a postcode that has already
  been validated against a strict pattern.
* Redirects are never followed, so credentials cannot be bounced to another host.
* Response bodies are read as a stream and capped (after decompression), so a
  misbehaving or compromised upstream cannot exhaust memory.
* ``HTTPS_PROXY`` and friends are honoured, so operators can force egress
  through an allowlisting proxy.
"""

from __future__ import annotations

import json
from typing import Any

import httpx2

from fuel_prices_mcp import __version__
from fuel_prices_mcp.config import Settings

USER_AGENT = f"fuel-prices-mcp/{__version__}"


class UserFacingError(Exception):
    """An error whose message is safe to show to the MCP client and the model.

    Messages never contain credentials, tokens, internal hostnames or raw upstream bodies.
    """


class InvalidInputError(UserFacingError):
    """The tool arguments were well-formed JSON but not usable (bad postcode, unknown fuel...)."""


class UpstreamError(UserFacingError):
    """An upstream API failed, refused us, or returned something unusable."""


def new_client(settings: Settings, transport: httpx2.AsyncBaseTransport | None = None) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=transport,
        timeout=httpx2.Timeout(settings.upstream_timeout, connect=10.0),
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        limits=httpx2.Limits(max_connections=8, max_keepalive_connections=4),
    )


async def request_json(
    client: httpx2.AsyncClient,
    method: str,
    url: str,
    *,
    max_bytes: int,
    service: str,
    **kwargs: Any,
) -> tuple[int, Any]:
    """Send a request and return ``(status, parsed JSON)``.

    The body is only read (and parsed) for 200 responses; for any other status the
    parsed value is None. Raises ``UpstreamError`` if the body exceeds ``max_bytes``
    or is not valid JSON.
    """
    async with client.stream(method, url, **kwargs) as response:
        status = response.status_code
        if status != 200:
            return status, None
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise UpstreamError(f"{service} sent an unexpectedly large response; refusing to read it.")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > max_bytes:
                raise UpstreamError(f"{service} sent an unexpectedly large response; refusing to read it.")
    try:
        return status, json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise UpstreamError(f"{service} returned a response that is not valid JSON.") from exc
