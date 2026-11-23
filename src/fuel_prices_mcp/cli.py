"""Command-line entry point: ``fuel-prices-mcp [serve|gen-token|check|healthcheck]``.

With no arguments it serves MCP over stdio, exactly like 0.1, so existing
client configurations (``uvx fuel-prices-mcp``) keep working.
"""

from __future__ import annotations

import argparse
import logging
import sys
import urllib.error
import urllib.request

import anyio

from fuel_prices_mcp import __version__
from fuel_prices_mcp.auth import generate_token
from fuel_prices_mcp.config import ConfigError, load_settings
from fuel_prices_mcp.logs import configure_logging
from fuel_prices_mcp.upstream import UserFacingError

logger = logging.getLogger("fuel_prices_mcp")


def _serve(args: argparse.Namespace) -> int:
    try:
        settings = load_settings(transport=args.transport, host=args.host, port=args.port)
    except ConfigError as exc:
        print(f"fuel-prices-mcp: configuration error: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level, settings.log_format)

    from fuel_prices_mcp.server import build_server  # imported late so config errors print fast

    mcp = build_server(settings)
    logger.info(
        "fuel-prices-mcp %s starting",
        __version__,
        extra={
            "event": {
                "transport": settings.transport,
                "fuel_finder_configured": settings.fuel_finder_configured,
            }
        },
    )
    if not settings.fuel_finder_configured:
        logger.warning("Fuel Finder credentials are not configured; tools will report how to set them up")
    if settings.transport == "stdio":
        anyio.run(mcp.run_stdio_async)
    else:
        from fuel_prices_mcp.http import run_http

        run_http(settings, mcp)
    return 0


def _gen_token(args: argparse.Namespace) -> int:
    token, digest = generate_token()
    label = args.label
    print("Give this bearer token to the MCP client (shown once, store it in a secret manager):")
    print(f"  {token}")
    print()
    print("Add this entry to FUEL_MCP_AUTH_TOKEN_HASHES (comma-separated) on the server:")
    print(f"  {label}:{digest}" if label else f"  {digest}")
    return 0


def _check(_: argparse.Namespace) -> int:
    """Verify the upstream configuration end to end: credentials, Fuel Finder and postcodes.io."""
    try:
        settings = load_settings(transport="stdio")
    except ConfigError as exc:
        print(f"fuel-prices-mcp: configuration error: {exc}", file=sys.stderr)
        return 2

    from fuel_prices_mcp.store import FuelData

    data = FuelData(settings)

    async def run() -> int:
        status = 0
        try:
            async with data.client() as client:
                await data.finder.access_token(client)
            print("Fuel Finder: credentials accepted (access token issued).")
        except UserFacingError as exc:
            print(f"Fuel Finder: {exc}", file=sys.stderr)
            status = 1
        except Exception as exc:
            print(f"Fuel Finder: unreachable ({type(exc).__name__}).", file=sys.stderr)
            status = 1
        try:
            where = await data.locate("SW1A 1AA")
            print(f"postcodes.io: reachable (SW1A 1AA -> {where.latitude:.3f}, {where.longitude:.3f}).")
        except UserFacingError as exc:
            print(f"postcodes.io: {exc}", file=sys.stderr)
            status = 1
        return status

    return anyio.run(run)


def _healthcheck(args: argparse.Namespace) -> int:
    """Container HEALTHCHECK without curl/wget in the image. Probes loopback only."""
    url = f"http://127.0.0.1:{args.port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=args.timeout) as response:
            return 0 if response.status == 200 else 1
    except (urllib.error.URLError, OSError):
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fuel-prices-mcp", description="UK fuel prices MCP server.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Run the MCP server (default).")
    serve.add_argument("--transport", choices=("stdio", "http"), help="Overrides FUEL_MCP_TRANSPORT.")
    serve.add_argument("--host", help="HTTP bind address. Overrides FUEL_MCP_HOST.")
    serve.add_argument("--port", type=int, help="HTTP port. Overrides FUEL_MCP_PORT.")
    serve.set_defaults(func=_serve)

    gen = sub.add_parser("gen-token", help="Generate a bearer token and the hash to configure on the server.")
    gen.add_argument("--label", help="Name for the token's owner, shown in audit logs (e.g. claude-laptop).")
    gen.set_defaults(func=_gen_token)

    check = sub.add_parser(
        "check", help="Check the Fuel Finder credentials and that both APIs are reachable."
    )
    check.set_defaults(func=_check)

    health = sub.add_parser("healthcheck", help="Probe the local HTTP server (for container health checks).")
    health.add_argument("--port", type=int, default=8000)
    health.add_argument("--timeout", type=float, default=3.0)
    health.set_defaults(func=_healthcheck)
    return parser


_COMMANDS = frozenset({"serve", "gen-token", "check", "healthcheck"})


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    # `fuel-prices-mcp --transport http` is shorthand for `fuel-prices-mcp serve --transport http`.
    if not args_list or (args_list[0] not in _COMMANDS and args_list[0] not in ("-h", "--help", "--version")):
        args_list.insert(0, "serve")
    args = build_parser().parse_args(args_list)
    return int(args.func(args))
