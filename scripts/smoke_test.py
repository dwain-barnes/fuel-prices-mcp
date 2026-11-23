"""End-to-end check of a running deployment: auth, transport protections and real tool calls.

    MCP_URL=http://127.0.0.1:8000/mcp MCP_TOKEN=fpmcp_... uv run scripts/smoke_test.py

Exits non-zero on the first failed expectation. Live-data checks run only when the
server reports Fuel Finder credentials, so this also works in CI without secrets.
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

EXPECTED_TOOLS = {"fuel_find_cheapest", "fuel_nearby_stations", "fuel_area_summary", "fuel_data_status"}


def check(condition: bool, label: str) -> None:
    print(("PASS " if condition else "FAIL ") + label)
    if not condition:
        sys.exit(1)


async def main() -> None:
    url = os.environ.get("MCP_URL", "http://127.0.0.1:8000/mcp")
    token = os.environ["MCP_TOKEN"]
    json_headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    probe = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

    async with httpx2.AsyncClient(timeout=30) as raw:
        health = await raw.get(url.rsplit("/", 1)[0] + "/healthz")
        check(health.status_code == 200, "/healthz answers without authentication")
        response = await raw.post(url, content=probe, headers=json_headers)
        check(response.status_code == 401, "request without a token is rejected (401)")
        response = await raw.post(
            url, content=probe, headers={**json_headers, "Authorization": "Bearer fpmcp_nope"}
        )
        check(response.status_code == 401, "request with a wrong token is rejected (401)")
        response = await raw.post(
            url,
            content=probe,
            headers={**json_headers, "Authorization": f"Bearer {token}", "Origin": "https://evil.example"},
        )
        check(response.status_code == 403, "cross-site Origin is rejected (403, DNS-rebinding protection)")
        response = await raw.get(url, headers={"Authorization": f"Bearer {token}"})
        check(response.status_code == 405, "GET on the MCP endpoint is refused (405, no session streams)")

    for mode in ("auto", "legacy"):
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=120) as http:
            async with Client(streamable_http_client(url, http_client=http), mode=mode) as client:  # type: ignore[arg-type]
                label = f"[{mode}, protocol {client.protocol_version}]"
                tools = {tool.name for tool in (await client.list_tools()).tools}
                check(tools == EXPECTED_TOOLS, f"{label} tools listed: {sorted(tools)}")

                status = (await client.call_tool("fuel_data_status", {})).structured_content
                check(
                    isinstance(status["configured"], bool), f"{label} data status: {status['note'][:60]}..."
                )

                bad = await client.call_tool("fuel_find_cheapest", {"location": "Narnia"})
                check(bool(bad.is_error), f"{label} invalid location is a tool error, not a crash")
                too_long = await client.call_tool("fuel_find_cheapest", {"location": "A" * 500})
                check(bool(too_long.is_error), f"{label} over-long argument is rejected by the schema")

                if not status["configured"]:
                    print(f"SKIP {label} live-data checks (no Fuel Finder credentials on the server)")
                    continue
                result = await client.call_tool(
                    "fuel_find_cheapest", {"location": "SW1A 1AA", "radius_miles": 3}
                )
                check(not result.is_error, f"{label} live lookup succeeded")
                data = result.structured_content
                check(bool(data["results"]), f"{label} {len(data['results'])} stations near SW1A 1AA")


if __name__ == "__main__":
    asyncio.run(main())
