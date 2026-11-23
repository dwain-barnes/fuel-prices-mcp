from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from fuel_prices_mcp.config import ConfigError, load_settings

HASH = hashlib.sha256(b"token").hexdigest()


def env(**values: str) -> dict[str, str]:
    return {f"FUEL_MCP_{key}": value for key, value in values.items()}


def test_defaults_are_stdio_and_loopback() -> None:
    settings = load_settings({})
    assert settings.transport == "stdio"
    assert settings.host == "127.0.0.1"
    assert settings.auth_mode == "none"
    assert settings.cache_ttl_seconds == 300
    assert settings.fuel_finder_configured is False


def test_http_without_auth_refuses_to_start() -> None:
    with pytest.raises(ConfigError, match="requires authentication"):
        load_settings(env(TRANSPORT="http"))


def test_http_with_explicit_auth_none_is_allowed() -> None:
    settings = load_settings(env(TRANSPORT="http", AUTH_MODE="none"))
    assert settings.auth_mode == "none"


def test_token_hashes_with_labels() -> None:
    other = hashlib.sha256(b"other").hexdigest()
    settings = load_settings(env(TRANSPORT="http", AUTH_TOKEN_HASHES=f"claude:{HASH}, {other.upper()}"))
    assert settings.auth_mode == "token"
    assert settings.token_hashes == {bytes.fromhex(HASH): "claude", bytes.fromhex(other): "token-2"}


def test_token_hashes_file(tmp_path: Path) -> None:
    hashes = tmp_path / "hashes"
    hashes.write_text(f"# comment\nlaptop:{HASH}  # trailing comment\n\n", encoding="utf-8")
    settings = load_settings(env(TRANSPORT="http", AUTH_TOKEN_HASHES_FILE=str(hashes)))
    assert settings.token_hashes == {bytes.fromhex(HASH): "laptop"}


@pytest.mark.parametrize("value", ["not-a-hash", "abc", f"bad label:{HASH}", f"{HASH},{HASH}"])
def test_bad_token_hashes_rejected(value: str) -> None:
    with pytest.raises(ConfigError):
        load_settings(env(TRANSPORT="http", AUTH_TOKEN_HASHES=value))


def test_stdio_ignores_http_auth_settings() -> None:
    settings = load_settings(env(AUTH_TOKEN_HASHES=HASH))
    assert settings.auth_mode == "none"
    assert settings.token_hashes == {}


def _oauth_env(**overrides: str) -> dict[str, str]:
    values = {
        "TRANSPORT": "http",
        "OAUTH_ISSUER": "https://auth.example.com/realms/main",
        "OAUTH_JWKS_URL": "https://auth.example.com/realms/main/certs",
        "OAUTH_RESOURCE_URL": "https://mcp.example.com/mcp",
    }
    values.update(overrides)
    return env(**values)


def test_oauth_settings() -> None:
    settings = load_settings(_oauth_env())
    assert settings.auth_mode == "oauth"
    assert settings.oauth is not None
    assert settings.oauth.required_scopes == ("fuelprices:read",)
    assert settings.oauth.algorithms == ("RS256", "ES256")


def test_oauth_requires_https() -> None:
    with pytest.raises(ConfigError, match="https"):
        load_settings(_oauth_env(OAUTH_JWKS_URL="http://auth.example.com/certs"))


def test_oauth_allows_http_on_loopback() -> None:
    settings = load_settings(_oauth_env(OAUTH_RESOURCE_URL="http://localhost:8000/mcp"))
    assert settings.oauth is not None


@pytest.mark.parametrize("alg", ["HS256", "none"])
def test_oauth_rejects_symmetric_and_none_algorithms(alg: str) -> None:
    with pytest.raises(ConfigError, match="unsupported"):
        load_settings(_oauth_env(OAUTH_ALGORITHMS=alg))


def test_oauth_missing_values() -> None:
    with pytest.raises(ConfigError, match="OAUTH_JWKS_URL"):
        load_settings(env(TRANSPORT="http", OAUTH_ISSUER="https://auth.example.com"))


def test_wildcard_origin_rejected() -> None:
    with pytest.raises(ConfigError, match="Wildcard"):
        load_settings(env(ALLOWED_ORIGINS="*"))


@pytest.mark.parametrize("path", ["/healthz", "/.well-known/x", "mcp", "/a b"])
def test_reserved_or_malformed_http_path(path: str) -> None:
    with pytest.raises(ConfigError, match="HTTP_PATH"):
        load_settings(env(HTTP_PATH=path))


# -- upstream credentials ----------------------------------------------------------------------------


def test_credentials_from_environment_keep_original_names() -> None:
    settings = load_settings({"FUEL_FINDER_CLIENT_ID": "cid", "FUEL_FINDER_CLIENT_SECRET": "s3cret!"})
    assert settings.fuel_finder_configured
    assert settings.fuel_finder_client_secret is not None
    assert settings.fuel_finder_client_secret.reveal() == "s3cret!"


def test_secret_never_appears_in_repr() -> None:
    settings = load_settings({"FUEL_FINDER_CLIENT_ID": "cid", "FUEL_FINDER_CLIENT_SECRET": "s3cret!"})
    assert "s3cret" not in repr(settings)
    assert "s3cret" not in str(settings.fuel_finder_client_secret)


def test_secret_from_file(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("s3cret!\n", encoding="utf-8")
    settings = load_settings({"FUEL_FINDER_CLIENT_ID": "cid", "FUEL_FINDER_CLIENT_SECRET_FILE": str(secret)})
    assert settings.fuel_finder_client_secret is not None
    assert settings.fuel_finder_client_secret.reveal() == "s3cret!"


def test_secret_env_and_file_together_is_ambiguous(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("a", encoding="utf-8")
    with pytest.raises(ConfigError, match="not both"):
        load_settings({"FUEL_FINDER_CLIENT_SECRET": "b", "FUEL_FINDER_CLIENT_SECRET_FILE": str(secret)})


@pytest.mark.parametrize("content", ["", "   \n", "has space", "x" * 600])
def test_bad_secret_file_contents(tmp_path: Path, content: str) -> None:
    secret = tmp_path / "secret"
    secret.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_settings({"FUEL_FINDER_CLIENT_SECRET_FILE": str(secret)})
    if content.strip():
        assert content.strip() not in str(info.value)  # the value is never echoed


def test_missing_secret_file() -> None:
    with pytest.raises(ConfigError, match="Cannot read FUEL_FINDER_CLIENT_SECRET_FILE"):
        load_settings({"FUEL_FINDER_CLIENT_SECRET_FILE": "/nonexistent/secret"})


def test_invalid_credential_value_is_not_echoed() -> None:
    with pytest.raises(ConfigError) as info:
        load_settings({"FUEL_FINDER_CLIENT_SECRET": "pa ss\x07word"})
    assert "pa ss" not in str(info.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.fuel-finder.service.gov.uk",
        "https://user:pw@example.com",
        "ftp://example.com",
        "https://example.com/?x=1",
    ],
)
def test_upstream_base_url_must_be_https(url: str) -> None:
    with pytest.raises(ConfigError, match="FUEL_FINDER_BASE_URL"):
        load_settings({"FUEL_FINDER_BASE_URL": url})


def test_upstream_base_url_override() -> None:
    settings = load_settings({"FUEL_FINDER_BASE_URL": "https://mirror.example.com/"})
    assert settings.fuel_finder_base_url == "https://mirror.example.com"


@pytest.mark.parametrize("ttl", ["30", "301", "nan"])
def test_cache_ttl_stays_within_fair_use(ttl: str) -> None:
    with pytest.raises(ConfigError):
        load_settings(env(CACHE_TTL_SECONDS=ttl))
