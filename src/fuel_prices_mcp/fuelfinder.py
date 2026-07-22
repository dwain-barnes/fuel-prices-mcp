"""Fuel Finder API client (www.fuel-finder.service.gov.uk).

Auth: OAuth client credentials against the Fuel Finder token endpoint;
tokens last an hour and are cached. Failed auth attempts are rate limited
server-side (HTTP 429, 5-minute lockout), so we never retry a failed
credential automatically.

Data comes in numbered batches; we page until an empty batch.
"""

from __future__ import annotations

import os
import time

import httpx

BASE_URL = "https://www.fuel-finder.service.gov.uk"
MAX_BATCHES = 100  # safety cap; the full UK dataset is far smaller

REGISTER = (
    "Register (free) via GOV.UK One Login at "
    "https://www.developer.fuel-finder.service.gov.uk/fuel-finder/get-started-ifr/onelogin "
    "then set FUEL_FINDER_CLIENT_ID and FUEL_FINDER_CLIENT_SECRET in the MCP "
    "server's environment."
)

_token_cache: dict = {"token": None, "expires_at": 0.0}


class ApiError(Exception):
    """Raised with an actionable message when a Fuel Finder call fails."""


def credentials() -> tuple[str, str] | None:
    cid = os.environ.get("FUEL_FINDER_CLIENT_ID")
    secret = os.environ.get("FUEL_FINDER_CLIENT_SECRET")
    if cid and secret:
        return cid, secret
    return None


def base_url() -> str:
    return os.environ.get("FUEL_FINDER_BASE_URL", BASE_URL)


def _get_token(client: httpx.Client) -> str:
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    creds = credentials()
    if creds is None:
        raise ApiError(f"Fuel Finder credentials not configured. {REGISTER}")
    resp = client.post(
        f"{base_url()}/api/v1/oauth/generate_access_token",
        json={"client_id": creds[0], "client_secret": creds[1]},
    )
    if resp.status_code == 429:
        raise ApiError(
            "Fuel Finder auth is rate limited after failed attempts — wait 5 minutes. "
            "Double-check FUEL_FINDER_CLIENT_ID and FUEL_FINDER_CLIENT_SECRET before retrying."
        )
    if resp.status_code in (400, 401):
        raise ApiError(f"Fuel Finder rejected the credentials. {REGISTER}")
    if resp.status_code != 200:
        raise ApiError(f"Fuel Finder token endpoint returned {resp.status_code}.")
    data = resp.json().get("data") or {}
    token = data.get("access_token")
    if not token:
        raise ApiError("Fuel Finder token response had no access_token.")
    _token_cache["token"] = token
    _token_cache["expires_at"] = time.time() + int(data.get("expires_in", 3600))
    return token


def _fetch_batched(path: str, client: httpx.Client) -> list[dict]:
    token = _get_token(client)
    records: list[dict] = []
    for batch in range(1, MAX_BATCHES + 1):
        resp = client.get(
            f"{base_url()}{path}",
            params={"batch-number": batch},
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 401:
            _token_cache["token"] = None
            token = _get_token(client)
            resp = client.get(
                f"{base_url()}{path}",
                params={"batch-number": batch},
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code == 429:
            raise ApiError("Fuel Finder rate limit hit. Wait a minute and try again.")
        if resp.status_code == 404 and batch > 1:
            break  # the API 404s past the last batch rather than returning []
        if resp.status_code != 200:
            raise ApiError(f"Fuel Finder returned {resp.status_code} for {path}.")
        page = resp.json()
        if not isinstance(page, list) or not page:
            break
        records.extend(page)
        if batch == MAX_BATCHES:
            raise ApiError(
                f"Fuel Finder returned more than {MAX_BATCHES} batches for {path} — "
                "aborting to avoid an endless loop. Please report this as a bug."
            )
    return records


def fetch_stations(client: httpx.Client | None = None) -> list[dict]:
    """All forecourts: details, location, amenities, opening times."""
    own = client is None
    client = client or httpx.Client(timeout=60)
    try:
        return _fetch_batched("/api/v1/pfs", client)
    finally:
        if own:
            client.close()


def fetch_prices(client: httpx.Client | None = None) -> list[dict]:
    """All forecourts' current fuel prices."""
    own = client is None
    client = client or httpx.Client(timeout=60)
    try:
        return _fetch_batched("/api/v1/pfs/fuel-prices", client)
    finally:
        if own:
            client.close()
