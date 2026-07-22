"""End-to-end tests through an in-process MCP client, data layer mocked."""

import pytest
from fixtures import BANGOR, STATIONS
from mcp.shared.memory import create_connected_server_and_client_session

from fuel_prices_mcp import server, store


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def stubbed(monkeypatch):
    monkeypatch.setattr(server, "_stations", lambda: STATIONS)
    monkeypatch.setattr(server, "resolve_location", lambda loc, client=None: (*BANGOR, loc.upper()))


@pytest.mark.anyio
async def test_tools_listed():
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        tools = await client.list_tools()
        assert {t.name for t in tools.tools} == {
            "fuel_find_cheapest",
            "fuel_nearby_stations",
            "fuel_area_summary",
            "fuel_data_status",
        }
        for t in tools.tools:
            assert t.annotations.readOnlyHint is True


@pytest.mark.anyio
async def test_find_cheapest_ranks_by_price(stubbed):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        result = await client.call_tool(
            "fuel_find_cheapest", {"location": "LL57 1AA", "fuel": "unleaded"}
        )
        assert result.isError is False
        data = result.structuredContent
        names = [r["name"] for r in data["results"]]
        assert names[0] == "SuperMart Bangor"  # 139.9 is cheapest E10 in radius
        assert "Far Away Fuels" not in names  # outside 5-mile default radius
        prices = [r["price_ppl"] for r in data["results"]]
        assert prices == sorted(prices)
        cheapest = data["results"][0]
        assert cheapest["vs_area_median_ppl"] < 0
        assert "tank" in data["savings_note"]


@pytest.mark.anyio
async def test_find_cheapest_diesel_alias(stubbed):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        result = await client.call_tool(
            "fuel_find_cheapest", {"location": "LL57", "fuel": "diesel"}
        )
        data = result.structuredContent
        assert data["fuel"] == "Diesel (B7)"
        assert all(r["price_ppl"] > 0 for r in data["results"])


@pytest.mark.anyio
async def test_nearby_stations_nearest_first(stubbed):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        result = await client.call_tool(
            "fuel_nearby_stations", {"location": "LL57 1AA", "radius_miles": 5}
        )
        data = result.structuredContent
        dists = [s["distance_miles"] for s in data["stations"]]
        assert dists == sorted(dists)
        assert data["stations"][0]["prices"]  # labelled prices present


@pytest.mark.anyio
async def test_area_summary_stats(stubbed):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        result = await client.call_tool(
            "fuel_area_summary", {"location": "LL57 1AA", "radius_miles": 5}
        )
        data = result.structuredContent
        e10 = next(s for s in data["stats"] if s["fuel"] == "Unleaded (E10)")
        assert e10["min_ppl"] == 139.9
        assert e10["max_ppl"] == 155.9
        assert e10["cheapest_station"] == "SuperMart Bangor"
        assert "spread" in data["spread_note"].lower()


@pytest.mark.anyio
async def test_data_status_unconfigured(monkeypatch):
    monkeypatch.delenv("FUEL_FINDER_CLIENT_ID", raising=False)
    monkeypatch.delenv("FUEL_FINDER_CLIENT_SECRET", raising=False)
    store._cache.update({"stations": None, "fetched_at": 0.0})
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        result = await client.call_tool("fuel_data_status", {})
        data = result.structuredContent
        assert data["configured"] is False
        assert "Register" in data["note"]
