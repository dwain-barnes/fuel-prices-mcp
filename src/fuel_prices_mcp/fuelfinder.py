"""Fuel Finder API client (www.fuel-finder.service.gov.uk).

Auth: OAuth 2.0 client credentials against the Fuel Finder token endpoint;
tokens last an hour and are cached in memory only. Failed auth attempts are
rate limited server-side (HTTP 429, 5-minute lockout), so a rejected credential
puts this client into a matching cool-down instead of retrying on every tool
call and extending the lockout.

Data comes in numbered batches; we page until an empty batch (or a 404 past
the last one), with hard caps on batches, records and bytes.

The server's own Fuel Finder credentials are the only ones ever sent upstream.
The MCP client's bearer token is never forwarded (no token passthrough).
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Callable
from typing import Any

import httpx2

from fuel_prices_mcp.config import Settings
from fuel_prices_mcp.upstream import UpstreamError, request_json

logger = logging.getLogger(__name__)

SERVICE = "The Fuel Finder API"
TOKEN_PATH = "/api/v1/oauth/generate_access_token"  # noqa: S105 - a URL path, not a secret
STATIONS_PATH = "/api/v1/pfs"
PRICES_PATH = "/api/v1/pfs/fuel-prices"

MAX_BATCHES = 100  # safety cap; the full UK dataset is far smaller
MAX_RECORDS = 50_000  # ~8,500 forecourts today
MAX_PAGE_BYTES = 32 * 1024 * 1024
MAX_TOKEN_RESPONSE_BYTES = 64 * 1024
AUTH_COOLDOWN_SECONDS = 300.0  # matches Fuel Finder's lockout after failed attempts
TOKEN_EXPIRY_MARGIN = 60.0
DEFAULT_TOKEN_LIFETIME = 3600.0
MAX_TOKEN_LIFETIME = 86_400.0

_ACCESS_TOKEN_RE = re.compile(r"^[\x21-\x7e]{1,4096}$")

REGISTER = (
    "Register (free) via GOV.UK One Login at "
    "https://www.developer.fuel-finder.service.gov.uk/fuel-finder/get-started-ifr/onelogin "
    "then give the server FUEL_FINDER_CLIENT_ID and FUEL_FINDER_CLIENT_SECRET "
    "(or FUEL_FINDER_CLIENT_SECRET_FILE)."
)


class CredentialsMissingError(UpstreamError):
    """No Fuel Finder credentials are configured."""


def _token_lifetime(raw: object) -> float:
    try:
        lifetime = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_TOKEN_LIFETIME
    if not math.isfinite(lifetime):
        return DEFAULT_TOKEN_LIFETIME
    return min(max(lifetime, TOKEN_EXPIRY_MARGIN * 2), MAX_TOKEN_LIFETIME)


class FuelFinderClient:
    """Fetches the full station and price datasets. Not safe for concurrent use; callers serialise."""

    def __init__(self, settings: Settings, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._settings = settings
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._auth_blocked_until = 0.0
        self._auth_block_reason = ""

    @property
    def configured(self) -> bool:
        return self._settings.fuel_finder_configured

    def _url(self, path: str) -> str:
        return self._settings.fuel_finder_base_url + path

    def _block_auth(self, reason: str) -> UpstreamError:
        self._token = None
        self._auth_blocked_until = self._clock() + AUTH_COOLDOWN_SECONDS
        self._auth_block_reason = reason
        logger.warning(
            "Fuel Finder authentication failed; pausing token requests for %.0f s", AUTH_COOLDOWN_SECONDS
        )
        return UpstreamError(reason)

    async def access_token(self, client: httpx2.AsyncClient) -> str:
        now = self._clock()
        if self._token and now < self._token_expires_at - TOKEN_EXPIRY_MARGIN:
            return self._token
        if now < self._auth_blocked_until:
            wait = max(1, round(self._auth_blocked_until - now))
            raise UpstreamError(f"{self._auth_block_reason} Token requests are paused for another {wait} s.")
        settings = self._settings
        if not (settings.fuel_finder_client_id and settings.fuel_finder_client_secret):
            raise CredentialsMissingError(
                f"Fuel Finder credentials are not configured on the server. {REGISTER}"
            )

        status, payload = await request_json(
            client,
            "POST",
            self._url(TOKEN_PATH),
            json={
                "client_id": settings.fuel_finder_client_id,
                "client_secret": settings.fuel_finder_client_secret.reveal(),
            },
            max_bytes=MAX_TOKEN_RESPONSE_BYTES,
            service=SERVICE,
        )
        if status == 429:
            raise self._block_auth(
                "Fuel Finder is rate limiting authentication after failed attempts; wait 5 minutes. "
                "Check FUEL_FINDER_CLIENT_ID and FUEL_FINDER_CLIENT_SECRET before retrying."
            )
        if status in (400, 401, 403):
            raise self._block_auth(f"Fuel Finder rejected the server's credentials. {REGISTER}")
        if status != 200:
            raise UpstreamError(f"The Fuel Finder token endpoint returned HTTP {status}; try again shortly.")

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise UpstreamError("The Fuel Finder token response did not contain a usable access token.")
        token = data.get("access_token")
        if not isinstance(token, str) or not _ACCESS_TOKEN_RE.fullmatch(token):
            raise UpstreamError("The Fuel Finder token response did not contain a usable access token.")
        self._token = token
        self._token_expires_at = self._clock() + _token_lifetime(
            data.get("expires_in", DEFAULT_TOKEN_LIFETIME)
        )
        return token

    async def _page(self, client: httpx2.AsyncClient, path: str, batch: int, token: str) -> tuple[int, Any]:
        return await request_json(
            client,
            "GET",
            self._url(path),
            params={"batch-number": batch},
            headers={"Authorization": f"Bearer {token}"},
            max_bytes=MAX_PAGE_BYTES,
            service=SERVICE,
        )

    async def _fetch_batched(self, client: httpx2.AsyncClient, path: str) -> list[dict[str, Any]]:
        token = await self.access_token(client)
        records: list[dict[str, Any]] = []
        for batch in range(1, MAX_BATCHES + 1):
            status, page = await self._page(client, path, batch, token)
            if status == 401:  # token revoked or expired early: one fresh token, one retry
                self._token = None
                token = await self.access_token(client)
                status, page = await self._page(client, path, batch, token)
            if status == 429:
                raise UpstreamError("The Fuel Finder API rate limit was hit; wait a minute and try again.")
            if status == 404 and batch > 1:
                break  # the API 404s past the last batch rather than returning []
            if status != 200:
                raise UpstreamError(f"The Fuel Finder API returned HTTP {status}; try again shortly.")
            if not isinstance(page, list) or not page:
                break
            records.extend(item for item in page if isinstance(item, dict))
            if len(records) > MAX_RECORDS:
                raise UpstreamError(
                    f"The Fuel Finder API returned more than {MAX_RECORDS} records; refusing to load them."
                )
            if batch == MAX_BATCHES:
                raise UpstreamError(
                    f"The Fuel Finder API returned more than {MAX_BATCHES} batches; aborting to avoid an "
                    "endless loop. Please report this as a bug."
                )
        return records

    async def fetch_stations(self, client: httpx2.AsyncClient) -> list[dict[str, Any]]:
        """All forecourts: details, location and amenities."""
        return await self._fetch_batched(client, STATIONS_PATH)

    async def fetch_prices(self, client: httpx2.AsyncClient) -> list[dict[str, Any]]:
        """All forecourts' current fuel prices."""
        return await self._fetch_batched(client, PRICES_PATH)
