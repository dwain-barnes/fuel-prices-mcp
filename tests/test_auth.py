from __future__ import annotations

import json
import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from fuel_prices_mcp.auth import JWKSCache, JWTTokenVerifier, StaticTokenVerifier, generate_token, hash_token
from fuel_prices_mcp.config import OAuthSettings

pytestmark = pytest.mark.anyio

ISSUER = "https://auth.example.com/realms/main"
RESOURCE = "https://mcp.example.com/mcp"
SCOPE = "fuelprices:read"


async def test_static_token_verifier() -> None:
    token, digest = generate_token()
    assert token.startswith("fpmcp_") and len(token) > 40
    assert hash_token(token).hex() == digest
    verifier = StaticTokenVerifier({bytes.fromhex(digest): "laptop"})

    access = await verifier.verify_token(token)
    assert access is not None
    assert access.client_id == "laptop"
    assert token not in access.token  # the raw secret is not retained

    assert await verifier.verify_token(token + "x") is None
    assert await verifier.verify_token("") is None
    assert await verifier.verify_token("a" * 10_000) is None


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks(key: rsa.RSAPrivateKey, kid: str = "k1") -> dict[str, Any]:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return {"keys": [jwk]}


class CountingFetch:
    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document
        self.calls = 0

    def __call__(self, url: str) -> dict[str, Any]:
        self.calls += 1
        return self.document


def _settings(**overrides: Any) -> OAuthSettings:
    values: dict[str, Any] = {
        "issuer": ISSUER,
        "jwks_url": "https://auth.example.com/certs",
        "resource_url": RESOURCE,
        "required_scopes": (SCOPE,),
        "algorithms": ("RS256",),
        "leeway_seconds": 0,
    }
    values.update(overrides)
    return OAuthSettings(**values)


def _token(key: rsa.RSAPrivateKey, *, kid: str = "k1", alg: str = "RS256", **claims: Any) -> str:
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "aud": RESOURCE,
        "sub": "user-1",
        "client_id": "claude",
        "iat": now,
        "exp": now + 300,
    }
    payload.update(claims)
    return jwt.encode(
        {k: v for k, v in payload.items() if v is not None}, key, algorithm=alg, headers={"kid": kid}
    )


@pytest.fixture
def verifier(rsa_key: rsa.RSAPrivateKey) -> tuple[JWTTokenVerifier, CountingFetch]:
    fetch = CountingFetch(_jwks(rsa_key))
    return JWTTokenVerifier(_settings(), jwks=JWKSCache("https://auth.example.com/certs", fetch=fetch)), fetch


async def test_valid_jwt(
    verifier: tuple[JWTTokenVerifier, CountingFetch], rsa_key: rsa.RSAPrivateKey
) -> None:
    access = await verifier[0].verify_token(_token(rsa_key, scope=f"{SCOPE} other"))
    assert access is not None
    assert access.client_id == "claude"
    assert access.subject == "user-1"
    assert access.scopes == [SCOPE, "other"]
    assert access.resource == RESOURCE
    assert access.claims == {"iss": ISSUER}


async def test_audience_list_and_trailing_slash(
    verifier: tuple[JWTTokenVerifier, CountingFetch], rsa_key: rsa.RSAPrivateKey
) -> None:
    assert await verifier[0].verify_token(_token(rsa_key, aud=["other-api", RESOURCE + "/"])) is not None


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "https://other-service.example.com"},  # token issued for another resource
        {"aud": None},
        {"iss": "https://evil.example.com"},
        {"exp": int(time.time()) - 10},
        {"exp": None},
    ],
)
async def test_rejected_claims(
    verifier: tuple[JWTTokenVerifier, CountingFetch], rsa_key: rsa.RSAPrivateKey, claims: dict[str, Any]
) -> None:
    assert await verifier[0].verify_token(_token(rsa_key, **claims)) is None


async def test_rejects_wrong_key_and_disallowed_algorithms(
    verifier: tuple[JWTTokenVerifier, CountingFetch], rsa_key: rsa.RSAPrivateKey
) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert await verifier[0].verify_token(_token(other)) is None
    hs = jwt.encode(
        {"iss": ISSUER, "aud": RESOURCE, "exp": int(time.time()) + 60}, "secret" * 8, algorithm="HS256"
    )
    assert await verifier[0].verify_token(hs) is None
    unsigned = jwt.encode(
        {"iss": ISSUER, "aud": RESOURCE, "exp": int(time.time()) + 60}, None, algorithm="none"
    )
    assert await verifier[0].verify_token(unsigned) is None
    assert await verifier[0].verify_token("not.a.jwt") is None


async def test_unknown_kid_refetch_is_throttled(
    verifier: tuple[JWTTokenVerifier, CountingFetch], rsa_key: rsa.RSAPrivateKey
) -> None:
    jwt_verifier, fetch = verifier
    assert await jwt_verifier.verify_token(_token(rsa_key)) is not None
    assert fetch.calls == 1
    for _ in range(5):
        assert await jwt_verifier.verify_token(_token(rsa_key, kid="forged")) is None
    assert fetch.calls == 1  # within min_refresh_interval: no extra fetches


async def test_jwks_fetch_failure_keeps_serving_cached_keys(rsa_key: rsa.RSAPrivateKey) -> None:
    now = [0.0]
    document = _jwks(rsa_key)
    calls = {"n": 0}

    def flaky(url: str) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("network down")
        return document

    cache = JWKSCache("https://x", fetch=flaky, ttl=10, min_refresh_interval=1, clock=lambda: now[0])
    assert await cache.get_key("k1") is not None
    now[0] = 20.0  # TTL expired, refresh fails
    assert await cache.get_key("k1") is not None
    assert calls["n"] == 2
