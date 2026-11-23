# Security model

This document describes what fuel-prices-mcp protects against, how, and what it leaves to you.
It maps each control to the [MCP 2026-07-28 specification](https://modelcontextprotocol.io/specification/2026-07-28)
(and its [security best practices](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices))
and to the attack classes collected in [awesome-mcp-security](https://github.com/Puliczek/awesome-mcp-security).

## What the server is

A small, read-only MCP server with four tools:

| Tool | Effect |
| --- | --- |
| `fuel_find_cheapest` | Geocodes a UK location (postcodes.io) and ranks cached Fuel Finder prices near it. |
| `fuel_nearby_stations` | Same inputs; lists nearby forecourts with all their prices. |
| `fuel_area_summary` | Same inputs; min/median/max per fuel for an area. |
| `fuel_data_status` | Reports the server's own state (credentials present, cache age, last error). Makes no outbound calls. |

Nothing is written to disk, nothing is executed, and no tool takes a URL, file path or command.

## Trust boundaries

```
 MCP client / LLM ──HTTP + bearer token──▶ mcp container ──HTTPS, fixed hosts──▶ Fuel Finder API
  (untrusted input)    127.0.0.1 only       (uid 10001,      server's own OAuth     (third-party data)
                                             read-only rootfs) client credentials
                                                     └──────── postcode only ───▶ api.postcodes.io
```

* **Everything arriving from the client is untrusted**: tool arguments and headers.
* **The LLM is untrusted as a source of intent**: it may have been prompt-injected elsewhere.
* **Fuel Finder data is third-party input.** Station names, brands, addresses and amenities are
  self-reported by forecourt operators, so they are treated as hostile text that will end up in a model's
  context (indirect prompt injection).
* **postcodes.io responses are semi-trusted**: only the coordinates are used, and they are validated.
* **The server's Fuel Finder credentials are the asset to protect.** Misuse could exhaust the quota or get
  the account locked or revoked, so they must never leak and the server must not be usable as an open proxy.

## Controls

### Protocol and transport (MCP 2026-07-28)

| Requirement | How it is met |
| --- | --- |
| Servers MUST validate `Origin` to prevent DNS rebinding; invalid → 403 | Always on, including in containers where the server binds 0.0.0.0. Unlisted `Origin` → 403, unlisted `Host` → 421. Configure with `FUEL_MCP_ALLOWED_ORIGINS` / `_HOSTS`; wildcards are refused. |
| SHOULD bind to localhost when running locally | Default bind is `127.0.0.1`; compose publishes the port on `127.0.0.1` only. |
| SHOULD authenticate all connections | HTTP refuses to start without auth unless `FUEL_MCP_AUTH_MODE=none` is set explicitly. Auth is deny-by-default for every path except `/healthz`. |
| Stateless protocol; no `Mcp-Session-Id`; GET/DELETE → 405 | Built on the official Python SDK 2.x (implements 2026-07-28: per-request `_meta`, `server/discover`, `resultType`). Legacy clients (2025-xx) are served statelessly too, so no session IDs are ever minted and there is no session to hijack. Non-POST requests to `/mcp` get 405. |
| `Mcp-Method` / `Mcp-Name` header ↔ body validation (`HeaderMismatch`, -32020) | Enforced by the SDK; covered by tests. |
| Tools: servers MUST validate inputs, implement access controls, rate-limit, sanitise outputs | See the sections below. |
| Deterministic `tools/list` | The tool list is fixed at start-up, in a fixed order, and never changes (`listChanged: false`, no `subscriptions/listen`). |
| Stdio: log to stderr only; take credentials from the environment | All logging goes to stderr; stdout carries only JSON-RPC. HTTP auth settings are ignored on stdio, as the spec directs. |
| Bounded requests | Request bodies are capped at 64 KiB (tool arguments are a postcode and a few numbers). |

### Authorization (OAuth 2.1 resource-server mode)

| Best practice | How it is met |
| --- | --- |
| MUST NOT accept tokens not issued for this server (audience) | JWT `aud` must equal `FUEL_MCP_OAUTH_RESOURCE_URL` (RFC 8707 / RFC 9068); the SDK re-checks the resource. |
| Token passthrough is forbidden | MCP client tokens are never forwarded anywhere. Upstream calls use only the server's own Fuel Finder client credentials, which are never accepted from, or returned to, clients. |
| Protected Resource Metadata (RFC 9728) and `WWW-Authenticate` discovery | Served by the SDK at `/.well-known/oauth-protected-resource/mcp`; 401s carry `resource_metadata`. |
| Scope minimisation | One narrow scope (`fuelprices:read` by default) is required and advertised. |
| Signature/algorithm safety | Asymmetric algorithms only (`HS*` and `none` cannot be configured); issuer, expiry and signature are verified; keys come from the configured JWKS over HTTPS. |
| Don't let forged tokens amplify traffic | JWKS is cached; an unknown `kid` triggers at most one refetch per 30 s. |
| SSRF from metadata URLs | Not applicable server-side: the only auth-related outbound URL (JWKS) is operator-configured and must be HTTPS (HTTP only on loopback). |

Static-token mode stores only SHA-256 hashes of 256-bit random tokens (`gen-token`), compares in constant
time, and never keeps or logs the raw token. Audit logs show the token's label, not the token.

### Upstream API calls

This is what sets this server apart from a self-contained one: it calls two third-party APIs on every
cache miss, with credentials of its own.

| Risk | Mitigation |
| --- | --- |
| **SSRF** | No tool argument becomes a URL, host or path, with one exception: a postcode, after it has matched an anchored pattern and been canonicalised to `[A-Z0-9 ]`. Hosts are fixed in code. `FUEL_FINDER_BASE_URL` is operator-only and must be HTTPS, without credentials, query or fragment. |
| **Credentials bounced by a redirect** | Redirects are never followed, so the client secret and access token only ever go to the configured Fuel Finder host. |
| **Hostile or huge responses** | Bodies are streamed and capped after decompression (32 MiB per Fuel Finder page, 64 KiB for the token response, 256 KiB for postcodes.io), at most 100 batches and 50,000 records, JSON only, 30 s per request and 180 s per refresh. |
| **Using the server to amplify traffic or exhaust the quota** | The dataset is cached for 5 minutes and refreshed single-flight (concurrent calls share one download); a failed refresh is not retried for 30 s; tool calls are rate-limited per principal; `/healthz` never calls upstream. |
| **Account lockout** | Fuel Finder locks credentials after failed attempts. A rejected credential pauses token requests for 5 minutes instead of retrying on every tool call and extending the lockout. |
| **Credential exposure** | The client secret is read from `FUEL_FINDER_CLIENT_SECRET_FILE` (a Docker secret in compose) or the environment, held in a wrapper that never renders in reprs, logs or errors, returned by no tool, and sent only in the token request body. The access token lives in memory only. Configuration errors name the variable but never echo its value. |
| **Privacy** | Only the postcode leaves the process, and successful lookups are cached in memory for 24 hours so it is not re-sent. `lat,lon` input needs no lookup at all. |
| **Egress** | The container needs HTTPS to two hosts only. `FUEL_MCP_EGRESS_PROXY` routes all outbound traffic through an allowlisting proxy (for example Smokescreen or Squid). |

### Attack classes from awesome-mcp-security

| Attack class | Mitigation here |
| --- | --- |
| **Tool poisoning / line jumping** (malicious instructions hidden in tool metadata that act before a tool is called) | Descriptions are short, factual and static. Annotations are truthful: `readOnlyHint: true` and `destructiveHint: false` everywhere; `openWorldHint: true` for the three tools that read third-party data, `false` for `fuel_data_status`. |
| **Rug pulls** (tool definitions change after approval) | The tool set is fixed at process start and never changes at runtime. Dependencies are hash-pinned in `uv.lock`, base images are digest-pinned, so a rebuild cannot silently pull different code. |
| **Output poisoning / indirect prompt injection** ("no output from your MCP server is safe") | The main risk for this server, because forecourt operators control station names. Every upstream string is sanitised once at ingest: NFC-normalised, stripped of control, format, bidi-override and zero-width characters and ANSI/OSC sequences, collapsed to one line and length-capped (name 120, brand 80, address 200, city 80). Values with a known shape are validated and dropped if they do not fit: postcodes, node IDs, amenity codes (`[a-z0-9_]`), ISO 8601 timestamps (passed through unmodified when valid), prices (finite, 50–500 p/litre), coordinates (UK bounding box) and fuel codes (known set only). Results are structured (`outputSchema` + `structuredContent`, extra fields forbidden); operator-supplied fields are labelled as third-party data in the schema, and the server instructions tell the model to treat them as data. Caller input echoed in error messages is sanitised and capped too. |
| **Deceptive ANSI terminal codes** | CSI/OSC escape sequences and all C0/C1 controls are removed from output. Logs are JSON, so values are escaped. |
| **Command injection / chained-command bypass** | No subprocess, no shell, no file access in any tool. |
| **Path traversal / arbitrary file read** | No tool reads files. The only files the process reads are the secret and token-hash files named in operator configuration. |
| **Data exfiltration / toxic flows** (one tool reads secrets, another sends them out) | No tool writes anything or lets the model choose a destination. The only tool input that leaves the process is a validated postcode, sent to a fixed host. Credentials never appear in tool output. |
| **Insecure credential storage** | Bearer-token hashes only; the OAuth mode keeps no credentials at all. The Fuel Finder secret is a Docker secret file: not in the image, not in `docker inspect`, not in the process environment. Tokens are not accepted from tool arguments, so they never pass through the model context. |
| **Confused deputy** | The server holds one upstream credential for public open data and exposes only fixed, read-only queries over it, so a client cannot make it do anything a tool does not already do. It performs no third-party authorization on anyone's behalf (no consent flows, no OAuth proxying). |
| **Conversation / data harvesting** | The server receives only what a tool call sends; it requests nothing else (no sampling, no elicitation, no roots). |
| **Denial of service** | Per-principal token-bucket rate limit (60/min, burst 20); 64 KiB body cap; schema limits on every argument (location ≤ 64 characters, radius ≤ 50 miles, at most 25/40 results); uvicorn connection cap; single-flight refresh; container memory, CPU and PID limits. |
| **Information leakage in errors** | Clients get short, generic messages (for example "The Fuel Finder API returned HTTP 503; try again shortly."). Hostnames, URLs and exception text go only to server logs, and the SDK hides unexpected exceptions entirely. |

### Container and supply chain

* **Image**: multi-stage build; runtime has no compiler, no `pip`, no shell user; runs as uid 10001; `PYTHONSAFEPATH`.
* **Runtime**: read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`, memory, CPU and PID limits, `noexec,nosuid,nodev` tmpfs, `init`.
* **Network**: the port is published on `127.0.0.1` only; the server needs outbound HTTPS to two hosts and nothing else.
* **Secrets**: the Fuel Finder client secret is mounted as a Docker secret; `.env`, `secrets/` and `.git` are kept out of the build context by the `.dockerignore` allowlist.
* **Pinned inputs**: `uv.lock` with hashes, base images by digest (`Dockerfile`), GitHub Actions by commit SHA, all kept current by Dependabot.
* **Published images** (`ghcr.io/beecho01/fuel-prices-mcp`) are built only by CI after the tests pass and after the image has been smoke-tested under the restrictions above. They carry an SBOM and signed SLSA build provenance (`gh attestation verify`).

### Audit logging

One JSON line per tool call on stderr (`logger: fuel_prices_mcp.audit`): principal (token label or OAuth
client/subject), tool, outcome, duration, the kind of location (`postcode`, `outcode` or `latlon`) and the
number of results. Rate-limit rejections are logged too. Locations, tokens and credentials are never logged.

## Residual risks and your responsibilities

* **Prices are self-reported.** Forecourts report their own prices under the statutory scheme; errors and
  lag happen. Treat the pump price as final.
* **Sanitisation is not immunity from prompt injection.** It removes hidden and control characters and
  keeps every field to one short line, but a forecourt can still choose a manipulative name in plain
  words. Hosts should treat tool output as data, keep a human in the loop for consequential actions and not
  let this server's output trigger other tools unchecked.
* **Postcodes go to a third party.** postcodes.io sees the postcode and the server's IP address. Pass
  `lat,lon` to avoid it.
* **One upstream credential is shared by every client.** All authenticated clients share the server's Fuel
  Finder quota. Issue one token per client so each can be revoked on its own; the rate limit is per principal.
* **Egress is not restricted by default.** The container can reach the internet. Set
  `FUEL_MCP_EGRESS_PROXY` or apply a network policy that allows only `www.fuel-finder.service.gov.uk` and
  `api.postcodes.io`.
* **No TLS built in.** The server listens on plain HTTP bound to loopback. To reach it from other machines,
  put a TLS-terminating reverse proxy in front, add its hostname to `FUEL_MCP_ALLOWED_HOSTS`, and set
  `FUEL_MCP_FORWARDED_ALLOW_IPS` to the proxy's address.
* **No failed-authentication throttling.** Tokens carry 256 bits of entropy, so guessing isn't feasible. Put a
  proxy or WAF in front if you expose the port beyond loopback.
* **The Fair Use Policy applies to you** if you build a public-facing service on top of this server.
* **The human in the loop is the client's job.** Hosts should show tool calls and allow users to deny them,
  as the spec recommends.

## Reporting a vulnerability

Please report security issues privately through GitHub's [private vulnerability reporting](https://docs.github.com/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) on this repository rather than in a public issue.
