"""Bearer-token authentication for the HTTP transport.

Two verifiers are provided:

* ``StaticTokenVerifier`` - pre-shared random tokens, configured as SHA-256
  hashes so the server never stores the secrets themselves.
* ``JWTTokenVerifier`` - OAuth 2.1 resource-server mode. Validates JWT access
  tokens issued by an external authorization server: signature (JWKS),
  issuer, expiry and, critically, that the audience is *this* MCP server
  (RFC 8707 / RFC 9068). Tokens issued for any other resource are refused and
  tokens are never forwarded anywhere (no token passthrough): upstream calls
  use only the server's own Fuel Finder credentials.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import anyio
import jwt
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import AccessToken
from starlette.authentication import AuthCredentials, UnauthenticatedUser
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from fuel_prices_mcp.config import OAuthSettings

logger = logging.getLogger(__name__)

MAX_TOKEN_LENGTH = 8192
TOKEN_BYTES = 32
TOKEN_PREFIX = "fpmcp_"  # noqa: S105 - a public prefix that makes leaked tokens easy to spot in scans


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def generate_token() -> tuple[str, str]:
    """Return a new random bearer token and its hex SHA-256 digest."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    return token, hash_token(token).hex()


def _fingerprint(token: str) -> str:
    """Short, non-reversible identifier so the raw token is never retained or logged."""
    return "sha256:" + hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()[:12]


class StaticTokenVerifier:
    """Accepts tokens whose SHA-256 digest is in the configured set."""

    def __init__(self, token_hashes: Mapping[bytes, str]) -> None:
        if not token_hashes:
            raise ValueError("at least one token hash is required")
        self._hashes = dict(token_hashes)

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return None
        digest = hash_token(token)
        label: str | None = None
        # Compare against every entry so timing does not reveal which (if any) matched.
        for known, known_label in self._hashes.items():
            if hmac.compare_digest(digest, known):
                label = known_label
        if label is None:
            return None
        return AccessToken(token=_fingerprint(token), client_id=label, scopes=[], subject=label)


class JWKSCache:
    """Caches an issuer's JSON Web Key Set with throttled refreshes.

    An unknown ``kid`` triggers at most one refetch per ``min_refresh_interval``
    so forged tokens cannot be used to hammer the authorization server.
    """

    def __init__(
        self,
        url: str,
        *,
        ttl: float = 300.0,
        min_refresh_interval: float = 30.0,
        timeout: float = 10.0,
        max_bytes: int = 256 * 1024,
        fetch: Callable[[str], Mapping[str, Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._ttl = ttl
        self._min_refresh_interval = min_refresh_interval
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._fetch = fetch or self._http_fetch
        self._clock = clock
        self._keys: dict[str | None, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._lock = anyio.Lock()

    def _http_fetch(self, url: str) -> Mapping[str, Any]:
        # The URL is operator-configured and restricted to https (or loopback http) in config.
        headers = {"Accept": "application/json", "User-Agent": "fuel-prices-mcp"}
        request = urllib.request.Request(url, headers=headers)  # noqa: S310
        with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
            body = response.read(self._max_bytes + 1)
        if len(body) > self._max_bytes:
            raise ValueError("JWKS document too large")
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("JWKS document is not a JSON object")
        return data

    async def _refresh(self) -> None:
        async with self._lock:
            now = self._clock()
            if self._fetched_at is not None and now - self._fetched_at < self._min_refresh_interval:
                return  # another task just refreshed
            try:
                data = await anyio.to_thread.run_sync(self._fetch, self._url)
                jwk_set = jwt.PyJWKSet.from_dict(dict(data))
            except Exception as exc:
                # Keep serving the previous keys; record the attempt to throttle retries.
                self._fetched_at = now
                logger.warning("Failed to refresh JWKS: %s", type(exc).__name__)
                return
            keys: dict[str | None, jwt.PyJWK] = {}
            for key in jwk_set.keys:
                if key.public_key_use in (None, "sig"):
                    keys[key.key_id] = key
            self._keys = keys
            self._fetched_at = now

    def _lookup(self, kid: str | None) -> jwt.PyJWK | None:
        if kid is None and len(self._keys) == 1:
            return next(iter(self._keys.values()))
        return self._keys.get(kid)

    async def get_key(self, kid: str | None) -> jwt.PyJWK | None:
        now = self._clock()
        if self._fetched_at is None or now - self._fetched_at >= self._ttl:
            await self._refresh()
        key = self._lookup(kid)
        if (
            key is None
            and self._fetched_at is not None
            and now - self._fetched_at >= self._min_refresh_interval
        ):
            await self._refresh()
            key = self._lookup(kid)
        return key


def _parse_scopes(claims: Mapping[str, Any]) -> list[str]:
    raw = claims.get("scope", claims.get("scp"))
    if isinstance(raw, str):
        return raw.split()
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str)]
    return []


def _audiences(claims: Mapping[str, Any]) -> Iterable[str]:
    aud = claims.get("aud")
    if isinstance(aud, str):
        return [aud]
    if isinstance(aud, list):
        return [item for item in aud if isinstance(item, str)]
    return []


class JWTTokenVerifier:
    """Validates JWT access tokens for an OAuth 2.1 protected resource."""

    def __init__(self, settings: OAuthSettings, *, jwks: JWKSCache | None = None) -> None:
        self._settings = settings
        self._jwks = jwks or JWKSCache(settings.jwks_url)
        self._resource = settings.resource_url.rstrip("/")

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return None
        try:
            header = jwt.get_unverified_header(token)
            alg = header.get("alg")
            if alg not in self._settings.algorithms:
                raise jwt.InvalidAlgorithmError(f"algorithm {alg!r} not allowed")
            key = await self._jwks.get_key(header.get("kid"))
            if key is None:
                raise jwt.InvalidKeyError("unknown signing key")
            claims = jwt.decode(
                token,
                key=key.key,
                algorithms=[alg],
                issuer=self._settings.issuer,
                leeway=self._settings.leeway_seconds,
                options={"require": ["exp", "iss", "aud"], "verify_aud": False},
            )
            if not any(aud.rstrip("/") == self._resource for aud in _audiences(claims)):
                raise jwt.InvalidAudienceError("token was not issued for this resource")
        except jwt.PyJWTError as exc:
            logger.info("Rejected bearer token: %s", type(exc).__name__)
            return None

        client_id = claims.get("client_id") or claims.get("azp") or "unknown"
        subject = claims.get("sub")
        return AccessToken(
            token=_fingerprint(token),
            client_id=str(client_id),
            scopes=_parse_scopes(claims),
            expires_at=int(claims["exp"]),
            resource=self._settings.resource_url,
            subject=str(subject) if subject is not None else None,
            claims={"iss": claims["iss"]},
        )


class BearerAuthGate:
    """ASGI middleware requiring a valid bearer token on every path except ``public_paths``.

    Used for static-token mode; OAuth mode uses the SDK's built-in resource-server wiring,
    which also publishes Protected Resource Metadata (RFC 9728).
    """

    def __init__(self, app: ASGIApp, verifier: StaticTokenVerifier, *, public_paths: frozenset[str]) -> None:
        self._app = app
        self._backend = BearerAuthBackend(verifier)
        self._public_paths = public_paths
        self._protected = RequireAuthMiddleware(AuthContextMiddleware(app), required_scopes=[])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self._public_paths:
            await self._app(scope, receive, send)
            return
        result = await self._backend.authenticate(HTTPConnection(scope))
        if result is None:
            scope["auth"], scope["user"] = AuthCredentials(), UnauthenticatedUser()
        else:
            scope["auth"], scope["user"] = result
        await self._protected(scope, receive, send)
