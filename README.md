# fuel-prices-mcp

![fuel-prices-mcp finding the cheapest fuel](assets/demo.gif)

**UK fuel prices MCP server**: ask Claude *"where's the cheapest unleaded near LL57?"* and get real answers from the official **Fuel Finder** open data: every UK forecourt, prices updated within 30 minutes of a change, as required by the Motor Fuel Price (Open Data) Regulations 2025.

This is a hardened, containerised version of [dwain-barnes/fuel-prices-mcp](https://github.com/dwain-barnes/fuel-prices-mcp):

* Implements MCP **2026-07-28** (stateless requests, `server/discover`) and still serves clients on
  2025-era protocol versions.
* Streamable HTTP with bearer-token or OAuth 2.1 authentication, or stdio.
* Read-only tools with structured results, sanitised third-party data, and rate, size and time limits.
* Ships as one locked-down container: non-root, read-only filesystem, no capabilities, loopback-only port.

See [SECURITY.md](SECURITY.md) for the threat model and how each control maps to the MCP spec and to
[awesome-mcp-security](https://github.com/Puliczek/awesome-mcp-security).

## Tools

| Tool | What it does |
|---|---|
| `fuel_find_cheapest` | Cheapest stations for a fuel near a postcode, ranked by price, each compared to the local median, with the tank-of-fuel savings spread |
| `fuel_nearby_stations` | Forecourts near a location, nearest first, with all fuel prices, brand and amenities |
| `fuel_area_summary` | Min/median/max per fuel for an area: "is 142.9 actually a good price here?" |
| `fuel_data_status` | Diagnostics: credentials configured, stations loaded, cache age, last refresh error |

Locations are a UK postcode (`LL57 2DG`), an outward code (`LL57`) or `lat,lon`. Fuels: unleaded (E10),
super unleaded (E5), diesel (B7), premium diesel, B10 and HVO. Prices are pence per litre. All tools are
annotated read-only and return structured results (`outputSchema`). Errors (unknown postcode, upstream
down, rate limited) come back as tool errors with a short message the model can act on.

## How it fits together

```
 Claude / Cursor / VS Code
 POST /mcp  (Authorization: Bearer …)
            │
            ▼
 ┌──────────────────────────┐   HTTPS, own OAuth client credentials   ┌──────────────────┐
 │ mcp  (fuel-prices-mcp)   │ ──────────────────────────────────────▶ │ Fuel Finder API  │
 │ 127.0.0.1:8000 only      │                                         └──────────────────┘
 │ read-only, uid 10001     │   HTTPS, postcode only                  ┌──────────────────┐
 └──────────────────────────┘ ──────────────────────────────────────▶ │ api.postcodes.io │
                                                                      └──────────────────┘
```

The server fetches the full UK dataset (stations and prices, batch-paginated), joins and sanitises it,
and keeps it in memory for at most 5 minutes, so repeated questions don't hammer the API while staying
inside the Fair Use Policy's freshness expectation. Postcodes resolve via [postcodes.io](https://postcodes.io)
(keyless); distances and statistics are computed locally. Permanently closed stations are excluded; prices
older than 7 days are flagged as stale.

## Quick start (Docker Compose)

Requires Docker with Compose v2.

**1. Register for Fuel Finder access (free).** The API needs a GOV.UK One Login:
[start here](https://www.developer.fuel-finder.service.gov.uk/fuel-finder/get-started-ifr/onelogin). You'll
get an OAuth client ID and secret.

**2. Give the server its credentials.** The client secret is mounted as a Docker secret, so it never
appears in `docker inspect` or the container's environment.

```bash
mkdir secrets
printf '%s' 'YOUR-CLIENT-SECRET' > secrets/fuel_finder_client_secret   # git-ignored
cp .env.example .env                                                   # then set FUEL_FINDER_CLIENT_ID
```

On Linux, the container runs as uid 10001, so that user must be able to read the file. Keep the
directory private and the file readable: `chmod 700 secrets && chmod 644 secrets/fuel_finder_client_secret`.

**3. Create a bearer token.** The token goes to your MCP client; only its hash goes on the server.

```bash
docker compose run --rm --no-deps mcp gen-token --label my-laptop
# put the printed "my-laptop:3f2a…" line in .env as FUEL_MCP_AUTH_TOKEN_HASHES
```

**4. Start it.**

```bash
docker compose up -d --build   # or drop --build to pull ghcr.io/beecho01/fuel-prices-mcp once CI has published it
docker compose ps              # should become "healthy"
docker compose run --rm --no-deps mcp check   # verifies the Fuel Finder credentials and both APIs
```

The server now listens on `http://127.0.0.1:8000/mcp` (loopback only). Check the whole chain:

```bash
MCP_TOKEN=fpmcp_… uv run scripts/smoke_test.py
```

### Connect a client

Replace `<token>` with the `fpmcp_…` token from step 3, and keep it out of version control.

**Claude Code**

```bash
claude mcp add --transport http fuelprices http://127.0.0.1:8000/mcp --header "Authorization: Bearer <token>"
```

**Cursor** (`~/.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "fuelprices": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

**VS Code** (`.vscode/mcp.json`). VS Code prompts for the token and stores it securely:

```json
{
  "inputs": [
    { "type": "promptString", "id": "fuelprices-token", "description": "Fuel prices MCP token", "password": true }
  ],
  "servers": {
    "fuelprices": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp",
      "headers": { "Authorization": "Bearer ${input:fuelprices-token}" }
    }
  }
}
```

### Stdio through Docker (Claude Desktop and other local clients)

With stdio the client launches the server itself, so no port and no bearer token are needed (the MCP
spec's recommended model for local servers). Configure the client to run a throwaway, locked-down
container. `-e NAME` without a value passes the variable from the `env` block, which keeps the secret out
of the command line:

```json
{
  "mcpServers": {
    "fuelprices": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm", "--init",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--memory", "512m", "--pids-limit", "64",
        "-e", "FUEL_FINDER_CLIENT_ID", "-e", "FUEL_FINDER_CLIENT_SECRET",
        "ghcr.io/beecho01/fuel-prices-mcp:latest", "serve", "--transport", "stdio"
      ],
      "env": {
        "FUEL_FINDER_CLIENT_ID": "your-client-id",
        "FUEL_FINDER_CLIENT_SECRET": "your-client-secret"
      }
    }
  }
}
```

To keep the secret out of the client config too, mount a file instead:
`"-v", "/path/to/secret:/run/secrets/ff:ro", "-e", "FUEL_FINDER_CLIENT_SECRET_FILE=/run/secrets/ff"`.

## Example

> **You:** Cheapest diesel within 10 miles of CF10 1EP?
>
> **Claude** (via `fuel_find_cheapest`): The cheapest is 146.9p/litre at a supermarket forecourt 2.1 miles away, 5.0p under the local median. The spread in your area is 16.0p/litre, which is about £8.80 on a 55-litre tank, so it's worth the detour past the two nearest stations.

## Authentication

HTTP always requires authentication: anyone who can reach the port could otherwise spend the server's
Fuel Finder quota. The server refuses to start without it unless you set `FUEL_MCP_AUTH_MODE=none`, which
is only appropriate for a trusted, loopback-only setup.

**Bearer tokens (default).** `fuel-prices-mcp gen-token` creates a 256-bit random token and prints its
SHA-256. Configure one or more `label:hash` entries in `FUEL_MCP_AUTH_TOKEN_HASHES`, or one per line in a
file named by `FUEL_MCP_AUTH_TOKEN_HASHES_FILE`. Labels show up in audit logs and key the rate limiter. To
revoke a token, remove its line and restart.

**OAuth 2.1 (resource-server mode).** Use this when you already have an identity provider (Keycloak, Entra
ID, Auth0, Authentik and so on). The server validates JWT access tokens locally against the provider's JWKS
and publishes [Protected Resource Metadata](https://datatracker.ietf.org/doc/html/rfc9728) at
`/.well-known/oauth-protected-resource/mcp`, so spec-compliant clients can discover where to log in.

```bash
FUEL_MCP_OAUTH_ISSUER=https://auth.example.com/realms/main
FUEL_MCP_OAUTH_JWKS_URL=https://auth.example.com/realms/main/protocol/openid-connect/certs
FUEL_MCP_OAUTH_RESOURCE_URL=https://mcp.example.com/mcp    # tokens must carry this as "aud"
FUEL_MCP_OAUTH_REQUIRED_SCOPES=fuelprices:read
```

Configure your provider to put the resource URL in the token's `aud` claim and to grant the
`fuelprices:read` scope. Tokens issued for any other audience are rejected. Client tokens are never
forwarded upstream; the server only ever uses its own Fuel Finder credentials.

## Container image

Every push to `main` publishes a multi-arch image (linux/amd64, linux/arm64) to GitHub Container Registry,
built by [.github/workflows/docker-publish.yml](.github/workflows/docker-publish.yml) after the tests pass and
after the image has been smoke-tested read-only, non-root and without capabilities:

| Tag | Meaning |
| --- | --- |
| `latest`, `main` | Current `main` branch |
| `1.2.3`, `1.2` | Release tags (`git tag v1.2.3 && git push --tags`) |
| `sha-<short>` | One specific commit |

Pin a release tag (or a digest) for deployments, for example `MCP_IMAGE=ghcr.io/beecho01/fuel-prices-mcp:1.0.0`
in `.env`. Each image carries an SBOM and signed SLSA build provenance, which you can check with the GitHub CLI:

```bash
gh attestation verify oci://ghcr.io/beecho01/fuel-prices-mcp:latest --owner beecho01
```

## Exposing it beyond localhost

The compose file binds `127.0.0.1` only. To use the server from other machines:

1. Put a TLS-terminating reverse proxy in front (Caddy, Traefik, nginx).
2. Add the public hostname to `FUEL_MCP_ALLOWED_HOSTS` (and its origin to `FUEL_MCP_ALLOWED_ORIGINS` if
   browsers are involved). Otherwise the DNS-rebinding protection rejects requests with 421 or 403.
3. Set `FUEL_MCP_FORWARDED_ALLOW_IPS` to the proxy's address so client IPs in logs are correct.
4. Prefer OAuth mode, or at least one token per client so each can be revoked on its own.
5. Restrict egress to `www.fuel-finder.service.gov.uk` and `api.postcodes.io`, for example with an
   allowlisting proxy set in `FUEL_MCP_EGRESS_PROXY`.

## Configuration

Server settings are environment variables prefixed with `FUEL_MCP_`; the Fuel Finder credentials keep their
original names. Invalid values stop the server at startup with a clear message that never echoes a secret.

| Variable | Default | Purpose |
| --- | --- | --- |
| `FUEL_FINDER_CLIENT_ID` | unset | Fuel Finder OAuth client ID. |
| `FUEL_FINDER_CLIENT_SECRET` / `FUEL_FINDER_CLIENT_SECRET_FILE` | unset | The client secret, or a file containing it (Docker secret). Set one, not both. `FUEL_FINDER_CLIENT_ID_FILE` works too. |
| `FUEL_FINDER_BASE_URL` | `https://www.fuel-finder.service.gov.uk` | Upstream API (HTTPS only). |
| `FUEL_MCP_TRANSPORT` | `stdio` (`http` in compose) | `stdio` or `http`. Also `serve --transport`. |
| `FUEL_MCP_HOST` / `FUEL_MCP_PORT` | `127.0.0.1` / `8000` (`0.0.0.0` in the image) | HTTP bind address. |
| `FUEL_MCP_HTTP_PATH` | `/mcp` | MCP endpoint path. |
| `FUEL_MCP_ALLOWED_HOSTS` | `127.0.0.1:*,localhost:*,[::1]:*` | Accepted `Host` headers (`:*` = any port). |
| `FUEL_MCP_ALLOWED_ORIGINS` | `http://127.0.0.1:*,http://localhost:*,http://[::1]:*` | Accepted `Origin` headers. Requests without `Origin` are allowed. |
| `FUEL_MCP_HTTP_MAX_CONNECTIONS` | `64` | Concurrent HTTP connections before 503. |
| `FUEL_MCP_FORWARDED_ALLOW_IPS` | unset | Trust `X-Forwarded-*` from these proxy IPs. |
| `FUEL_MCP_CACHE_TTL_SECONDS` | `300` | How long the dataset is cached (60–300; the Fair Use Policy expects at most 5 minutes). |
| `FUEL_MCP_UPSTREAM_TIMEOUT` / `FUEL_MCP_REFRESH_TIMEOUT` | `30` / `180` | Seconds per upstream request / per full refresh. |
| `FUEL_MCP_RATE_LIMIT_PER_MINUTE` / `FUEL_MCP_RATE_LIMIT_BURST` | `60` / `20` | Per-principal token bucket (all tools). |
| `FUEL_MCP_AUTH_MODE` | auto | `token`, `oauth` or `none`. Inferred from the settings below. |
| `FUEL_MCP_AUTH_TOKEN_HASHES` | unset | `label:sha256hex` entries, comma-separated. |
| `FUEL_MCP_AUTH_TOKEN_HASHES_FILE` | unset | File with one entry per line (`#` comments allowed). |
| `FUEL_MCP_OAUTH_ISSUER` / `_JWKS_URL` / `_RESOURCE_URL` | unset | OAuth resource-server mode (HTTPS required, except loopback). |
| `FUEL_MCP_OAUTH_REQUIRED_SCOPES` | `fuelprices:read` | Comma-separated scopes every token must carry. |
| `FUEL_MCP_OAUTH_ALGORITHMS` | `RS256,ES256` | Accepted JWT algorithms (asymmetric only). |
| `FUEL_MCP_OAUTH_LEEWAY_SECONDS` | `30` | Clock-skew allowance. |
| `FUEL_MCP_LOG_LEVEL` / `FUEL_MCP_LOG_FORMAT` | `INFO` / `json` | Logs go to stderr; one JSON audit line per tool call. |
| `FUEL_MCP_EGRESS_PROXY` | unset | Compose only: passed to the container as `HTTPS_PROXY`. |

## Running without Docker

You need [uv](https://docs.astral.sh/uv/). Install from this repository (pin a tag for reproducibility):

```bash
uvx --from git+https://github.com/beecho01/fuel-prices-mcp@v1.0.0 fuel-prices-mcp           # stdio
uvx --from git+https://github.com/beecho01/fuel-prices-mcp@v1.0.0 fuel-prices-mcp check     # verify credentials
```

Note that `uvx fuel-prices-mcp` on its own installs the original 0.1 package from PyPI, not this version.
Set the credentials as environment variables in your shell or your MCP client's `env` block. With no
arguments the server speaks stdio, so 0.1 client configurations keep working once they point at this build.

## Fair Use Policy compliance

This server is built to comply with the Fuel Finder Aggregator Fair Use Policy: prices are presented unbiased and ranked by objective criteria (price, distance), prices and valid timestamps pass through unmodified (malformed values are dropped, never rewritten), the in-memory cache refreshes within the policy's 5-minute freshness expectation, and every response includes the official [Report a Discrepancy](https://www.fuel-finder.service.gov.uk/motorist/price-report) link. If you build a public-facing service on top of this server, the Fair Use Policy applies to you directly; read it when you register.

## Limitations

- Prices are self-reported by forecourts under the statutory scheme; errors and lag happen. Treat the pump price as final.
- The scheme covers road fuel at registered UK forecourts; it does not include EV charging or LPG prices.
- Postcode geocoding needs internet access to postcodes.io; `lat,lon` input works without it.
- Not affiliated with the CMA, DESNZ or GOV.UK.

## Privacy

Your Fuel Finder credentials stay on the machine running the server. Lookups go directly from the server to
the government API and postcodes.io: no other third parties, no telemetry. postcodes.io sees the postcodes
you ask about (use `lat,lon` to avoid that); the server caches lookups in memory so it doesn't re-send them.
Audit logs record which tool was called, by whom and with what outcome, never the location.

## Upgrading from 0.1

* Tool names, arguments and result fields are unchanged. `fuel_data_status` gains a `last_error` field.
* `FUEL_FINDER_CLIENT_ID` / `FUEL_FINDER_CLIENT_SECRET` keep their names; `FUEL_FINDER_CLIENT_SECRET_FILE`
  is new.
* The `fuel-prices-mcp` command still defaults to stdio. New subcommands: `serve --transport http`,
  `gen-token`, `check` and `healthcheck`.
* Built on MCP Python SDK 2.x. Station names, addresses and amenities are now sanitised, and malformed
  upstream records (implausible prices, coordinates outside the UK) are dropped.

## Development

```bash
uv sync                 # installs dev tools too
uv run pytest           # unit, tool and in-process HTTP tests against a fake upstream
uv run ruff check . && uv run ruff format --check .
```

The tests never touch the real APIs or need credentials. To check a real deployment, run
`scripts/smoke_test.py` as shown above; it also runs live lookups when the server has credentials.

Dependencies are locked in `uv.lock`. After changing `pyproject.toml`, run `uv lock`. Base images are pinned
by digest in the `Dockerfile`, and Dependabot proposes updates to them, to `uv.lock` and to the pinned actions.

## Credits

Built by **Dwain Barnes / EryriLabs** ([HuggingFace](https://huggingface.co/EryriLabs)); hardened and
containerised in this fork. Contains public sector information from the [Fuel Finder scheme](https://www.developer.fuel-finder.service.gov.uk/), licensed under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/). Geocoding by [postcodes.io](https://postcodes.io).
