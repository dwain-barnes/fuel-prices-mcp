"""Runtime configuration, read from environment variables and validated at startup.

Server settings use the ``FUEL_MCP_`` prefix. The upstream Fuel Finder
credentials keep their original names (``FUEL_FINDER_CLIENT_ID`` and
``FUEL_FINDER_CLIENT_SECRET``) so existing MCP client configs keep working, and
each also accepts a ``_FILE`` variant (Docker/Kubernetes secrets) so the secret
does not have to live in the process environment.

The server refuses to start on an unsafe or ambiguous combination (fail
closed), for example an HTTP listener with no authentication configured.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

ENV_PREFIX = "FUEL_MCP_"
UPSTREAM_PREFIX = "FUEL_FINDER_"

DEFAULT_FUEL_FINDER_URL = "https://www.fuel-finder.service.gov.uk"

# Tool arguments are a postcode and a few numbers; nothing legitimate comes close to this.
MAX_HTTP_BODY_BYTES = 64 * 1024
MAX_SECRET_FILE_BYTES = 4096

Transport = Literal["stdio", "http"]
AuthMode = Literal["none", "token", "oauth"]
LogFormat = Literal["json", "text"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Asymmetric algorithms only: symmetric (HS*) keys and "none" are never accepted.
ALLOWED_JWT_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)
DEFAULT_SCOPE = "fuelprices:read"

_TOKEN_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_HTTP_PATH_RE = re.compile(r"^/[A-Za-z0-9/_-]{0,63}$")
_SCOPE_RE = re.compile(r"^[\x21\x23-\x5b\x5d-\x7e]+$")  # RFC 6749 scope-token
_CREDENTIAL_RE = re.compile(r"^[\x21-\x7e]{1,512}$")  # printable ASCII, no spaces


class ConfigError(ValueError):
    """Raised when the configuration is invalid or unsafe. Messages never contain secret values."""


class Secret:
    """Holds a credential without ever rendering it in reprs, logs or tracebacks."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('**********')"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class OAuthSettings:
    issuer: str
    jwks_url: str
    resource_url: str
    required_scopes: tuple[str, ...]
    algorithms: tuple[str, ...]
    leeway_seconds: int


@dataclass(frozen=True, slots=True)
class Settings:
    transport: Transport = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000
    http_path: str = "/mcp"
    allowed_hosts: tuple[str, ...] = ("127.0.0.1:*", "localhost:*", "[::1]:*")
    allowed_origins: tuple[str, ...] = ("http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*")
    http_max_connections: int = 64
    forwarded_allow_ips: str | None = None

    # Upstream data sources. The secret is wrapped so it cannot leak through repr().
    fuel_finder_base_url: str = DEFAULT_FUEL_FINDER_URL
    fuel_finder_client_id: str | None = None
    fuel_finder_client_secret: Secret | None = field(default=None, repr=False)
    upstream_timeout: float = 30.0
    refresh_timeout: float = 180.0
    # The Fuel Finder Fair Use Policy expects prices shown to motorists to be at most 5 minutes old.
    cache_ttl_seconds: int = 300

    rate_limit_per_minute: int = 60
    rate_limit_burst: int = 20

    auth_mode: AuthMode = "none"
    # sha256(token) -> label. Only hashes are held, so a leaked config does not leak tokens.
    token_hashes: Mapping[bytes, str] = field(default_factory=dict)
    oauth: OAuthSettings | None = None

    log_level: LogLevel = "INFO"
    log_format: LogFormat = "json"

    @property
    def fuel_finder_configured(self) -> bool:
        return bool(self.fuel_finder_client_id and self.fuel_finder_client_secret)

    @property
    def max_http_body_bytes(self) -> int:
        return MAX_HTTP_BODY_BYTES


def _get(env: Mapping[str, str], name: str, prefix: str = ENV_PREFIX) -> str | None:
    value = env.get(prefix + name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _parse_int(env: Mapping[str, str], name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{ENV_PREFIX}{name} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{ENV_PREFIX}{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _parse_float(
    env: Mapping[str, str], name: str, default: float, *, minimum: float, maximum: float
) -> float:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{ENV_PREFIX}{name} must be a number, got {raw!r}") from exc
    if not minimum <= value <= maximum:  # also rejects NaN
        raise ConfigError(f"{ENV_PREFIX}{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _parse_list(env: Mapping[str, str], name: str) -> tuple[str, ...] | None:
    raw = _get(env, name)
    if raw is None:
        return None
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _parse_choice(env: Mapping[str, str], name: str, choices: tuple[str, ...], default: str) -> str:
    raw = _get(env, name)
    if raw is None:
        return default
    value = raw.lower() if choices[0].islower() else raw.upper()
    if value not in choices:
        raise ConfigError(f"{ENV_PREFIX}{name} must be one of {', '.join(choices)}, got {raw!r}")
    return value


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _require_secure_url(name: str, url: str, *, allow_path: bool = True) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ConfigError(f"{name} must be an absolute http(s) URL, got {url!r}")
    if parts.scheme == "http" and not _is_loopback_host(parts.hostname):
        raise ConfigError(f"{name} must use https (http is only allowed for loopback), got {url!r}")
    if parts.username or parts.password:
        raise ConfigError(f"{name} must not embed credentials")
    if parts.fragment or parts.query:
        raise ConfigError(f"{name} must not contain a query string or fragment")
    if not allow_path and parts.path not in ("", "/"):
        raise ConfigError(f"{name} must not contain a path")
    return url


def _read_credential(env: Mapping[str, str], name: str) -> str | None:
    """Read ``FUEL_FINDER_<name>`` or the file named by ``FUEL_FINDER_<name>_FILE``.

    Error messages name the variable but never echo its value.
    """
    var = UPSTREAM_PREFIX + name
    direct = _get(env, name, UPSTREAM_PREFIX)
    path = _get(env, name + "_FILE", UPSTREAM_PREFIX)
    if direct and path:
        raise ConfigError(f"Set either {var} or {var}_FILE, not both")
    value = direct
    if path:
        try:
            with open(path, "rb") as handle:
                raw = handle.read(MAX_SECRET_FILE_BYTES + 1)
        except OSError as exc:
            raise ConfigError(f"Cannot read {var}_FILE: {exc.strerror}") from exc
        if len(raw) > MAX_SECRET_FILE_BYTES:
            raise ConfigError(f"{var}_FILE is larger than {MAX_SECRET_FILE_BYTES} bytes")
        try:
            value = raw.decode("utf-8-sig").strip()  # tolerate a BOM from Windows editors
        except UnicodeDecodeError as exc:
            raise ConfigError(f"{var}_FILE is not UTF-8 text") from exc
        if not value:
            raise ConfigError(f"{var}_FILE is empty")
    if value is not None and not _CREDENTIAL_RE.fullmatch(value):
        raise ConfigError(f"{var} must be 1-512 printable ASCII characters without spaces")
    return value


def _parse_token_hashes(env: Mapping[str, str]) -> dict[bytes, str]:
    entries: list[str] = list(_parse_list(env, "AUTH_TOKEN_HASHES") or ())
    hashes_file = _get(env, "AUTH_TOKEN_HASHES_FILE")
    if hashes_file:
        try:
            text = Path(hashes_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"Cannot read {ENV_PREFIX}AUTH_TOKEN_HASHES_FILE: {exc.strerror}") from exc
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            entries.extend(part.strip() for part in line.split(",") if part.strip())

    result: dict[bytes, str] = {}
    for index, entry in enumerate(entries, start=1):
        label, sep, digest = entry.rpartition(":")
        if not sep:
            label, digest = f"token-{index}", entry
        digest = digest.strip().lower()
        label = label.strip()
        if not _TOKEN_HASH_RE.fullmatch(digest):
            raise ConfigError(
                f"Token hash #{index} is not a hex SHA-256 digest. "
                "Generate tokens with `fuel-prices-mcp gen-token`."
            )
        if not _LABEL_RE.fullmatch(label):
            raise ConfigError(f"Token label {label!r} must match {_LABEL_RE.pattern}")
        key = bytes.fromhex(digest)
        if key in result:
            raise ConfigError(f"Token hash #{index} is listed more than once")
        result[key] = label
    return result


def _parse_oauth(env: Mapping[str, str]) -> OAuthSettings:
    issuer = _get(env, "OAUTH_ISSUER")
    jwks_url = _get(env, "OAUTH_JWKS_URL")
    resource_url = _get(env, "OAUTH_RESOURCE_URL")
    missing = [
        ENV_PREFIX + name
        for name, value in (
            ("OAUTH_ISSUER", issuer),
            ("OAUTH_JWKS_URL", jwks_url),
            ("OAUTH_RESOURCE_URL", resource_url),
        )
        if value is None
    ]
    if missing:
        raise ConfigError(f"OAuth mode requires: {', '.join(missing)}")
    if not (issuer and jwks_url and resource_url):  # pragma: no cover - narrowed above
        raise ConfigError("OAuth settings incomplete")

    algorithms = _parse_list(env, "OAUTH_ALGORITHMS") or ("RS256", "ES256")
    bad = sorted(set(algorithms) - ALLOWED_JWT_ALGORITHMS)
    if bad:
        raise ConfigError(
            f"{ENV_PREFIX}OAUTH_ALGORITHMS contains unsupported values {bad}; "
            f"allowed: {', '.join(sorted(ALLOWED_JWT_ALGORITHMS))}"
        )
    scopes = _parse_list(env, "OAUTH_REQUIRED_SCOPES")
    scopes = (DEFAULT_SCOPE,) if scopes is None else scopes
    for scope in scopes:
        if not _SCOPE_RE.fullmatch(scope):
            raise ConfigError(f"Invalid OAuth scope {scope!r}")

    return OAuthSettings(
        issuer=_require_secure_url(ENV_PREFIX + "OAUTH_ISSUER", issuer),
        jwks_url=_require_secure_url(ENV_PREFIX + "OAUTH_JWKS_URL", jwks_url),
        resource_url=_require_secure_url(ENV_PREFIX + "OAUTH_RESOURCE_URL", resource_url),
        required_scopes=tuple(scopes),
        algorithms=tuple(algorithms),
        leeway_seconds=_parse_int(env, "OAUTH_LEEWAY_SECONDS", 30, minimum=0, maximum=300),
    )


def load_settings(
    env: Mapping[str, str] | None = None,
    *,
    transport: str | None = None,
    host: str | None = None,
    port: int | None = None,
) -> Settings:
    """Build validated settings from the environment, with optional CLI overrides."""
    env = os.environ if env is None else env

    resolved_transport = transport or _parse_choice(env, "TRANSPORT", ("stdio", "http"), "stdio")
    if resolved_transport not in ("stdio", "http"):
        raise ConfigError(f"Unknown transport {resolved_transport!r}")

    http_path = _get(env, "HTTP_PATH") or "/mcp"
    if (
        not _HTTP_PATH_RE.fullmatch(http_path)
        or http_path == "/healthz"
        or http_path.startswith("/.well-known")
    ):
        raise ConfigError(f"{ENV_PREFIX}HTTP_PATH must look like /mcp, got {http_path!r}")

    base_url = _get(env, "BASE_URL", UPSTREAM_PREFIX) or DEFAULT_FUEL_FINDER_URL
    base_url = _require_secure_url(UPSTREAM_PREFIX + "BASE_URL", base_url).rstrip("/")
    client_id = _read_credential(env, "CLIENT_ID")
    client_secret = _read_credential(env, "CLIENT_SECRET")

    token_hashes = _parse_token_hashes(env)
    oauth_configured = _get(env, "OAUTH_ISSUER") is not None
    explicit_mode = _get(env, "AUTH_MODE")
    if explicit_mode is None:
        auth_mode = "oauth" if oauth_configured else ("token" if token_hashes else "none")
    else:
        auth_mode = _parse_choice(env, "AUTH_MODE", ("none", "token", "oauth"), "none")

    oauth: OAuthSettings | None = None
    if resolved_transport == "http":
        if auth_mode == "token" and not token_hashes:
            raise ConfigError(
                f"Token auth requires {ENV_PREFIX}AUTH_TOKEN_HASHES or {ENV_PREFIX}AUTH_TOKEN_HASHES_FILE"
            )
        if auth_mode == "oauth":
            oauth = _parse_oauth(env)
        if auth_mode == "none" and explicit_mode is None:
            raise ConfigError(
                "The HTTP transport requires authentication: anyone who can reach the port could "
                "otherwise spend this server's Fuel Finder quota. Configure bearer tokens "
                f"({ENV_PREFIX}AUTH_TOKEN_HASHES, see `fuel-prices-mcp gen-token`) or OAuth "
                f"({ENV_PREFIX}OAUTH_*). To run without authentication on a trusted loopback-only "
                f"deployment, set {ENV_PREFIX}AUTH_MODE=none explicitly."
            )
    else:
        # stdio: the client owns the process; the spec says not to use HTTP auth here.
        auth_mode = "none"
        token_hashes = {}

    allowed_hosts = _parse_list(env, "ALLOWED_HOSTS")
    allowed_origins = _parse_list(env, "ALLOWED_ORIGINS")
    for value in (*(allowed_hosts or ()), *(allowed_origins or ())):
        if value == "*" or any(ch.isspace() for ch in value):
            raise ConfigError(f"Wildcard or malformed host/origin entry {value!r} is not allowed")

    defaults = Settings()
    return Settings(
        transport=resolved_transport,  # type: ignore[arg-type]
        host=host or _get(env, "HOST") or defaults.host,
        port=port or _parse_int(env, "PORT", defaults.port, minimum=1, maximum=65535),
        http_path=http_path,
        allowed_hosts=allowed_hosts if allowed_hosts is not None else defaults.allowed_hosts,
        allowed_origins=allowed_origins if allowed_origins is not None else defaults.allowed_origins,
        http_max_connections=_parse_int(env, "HTTP_MAX_CONNECTIONS", 64, minimum=1, maximum=10_000),
        forwarded_allow_ips=_get(env, "FORWARDED_ALLOW_IPS"),
        fuel_finder_base_url=base_url,
        fuel_finder_client_id=client_id,
        fuel_finder_client_secret=Secret(client_secret) if client_secret else None,
        upstream_timeout=_parse_float(env, "UPSTREAM_TIMEOUT", 30.0, minimum=1, maximum=120),
        refresh_timeout=_parse_float(env, "REFRESH_TIMEOUT", 180.0, minimum=10, maximum=900),
        cache_ttl_seconds=_parse_int(env, "CACHE_TTL_SECONDS", 300, minimum=60, maximum=300),
        rate_limit_per_minute=_parse_int(env, "RATE_LIMIT_PER_MINUTE", 60, minimum=1, maximum=100_000),
        rate_limit_burst=_parse_int(env, "RATE_LIMIT_BURST", 20, minimum=1, maximum=100_000),
        auth_mode=auth_mode,  # type: ignore[arg-type]
        token_hashes=token_hashes,
        oauth=oauth,
        log_level=_parse_choice(env, "LOG_LEVEL", ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"), "INFO"),  # type: ignore[arg-type]
        log_format=_parse_choice(env, "LOG_FORMAT", ("json", "text"), "json"),  # type: ignore[arg-type]
    )
