"""Fuel Finder client: pagination, token handling, auth cool-down and response limits."""

from __future__ import annotations

import dataclasses

import httpx2
import pytest

from fixtures import FakeUpstream
from fuel_prices_mcp import fuelfinder
from fuel_prices_mcp.config import Settings
from fuel_prices_mcp.fuelfinder import (
    PRICES_PATH,
    STATIONS_PATH,
    CredentialsMissingError,
    FuelFinderClient,
)
from fuel_prices_mcp.upstream import UpstreamError, new_client

pytestmark = pytest.mark.anyio


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def client_for(settings: Settings, handler) -> httpx2.AsyncClient:
    return new_client(settings, httpx2.MockTransport(handler))


async def test_fetches_all_batches_until_empty(settings: Settings, upstream: FakeUpstream) -> None:
    finder = FuelFinderClient(settings)
    async with new_client(settings, upstream.transport()) as client:
        records = await finder.fetch_stations(client)
    assert [r["node_id"] for r in records] == ["n1", "n2", "n3", "n4", "n5"]
    assert upstream.calls[f"{STATIONS_PATH}#4"] == 1  # 3 full/partial batches, then an empty one


async def test_token_cached_across_fetches(settings: Settings, upstream: FakeUpstream) -> None:
    finder = FuelFinderClient(settings)
    async with new_client(settings, upstream.transport()) as client:
        await finder.fetch_stations(client)
        await finder.fetch_prices(client)
    assert upstream.calls["token"] == 1


async def test_credentials_sent_only_in_token_request_body(
    settings: Settings, upstream: FakeUpstream
) -> None:
    finder = FuelFinderClient(settings)
    async with new_client(settings, upstream.transport()) as client:
        await finder.fetch_stations(client)
    token_request, *data_requests = upstream.requests
    assert b'"client_secret":"secret"' in token_request.content.replace(b" ", b"")
    for request in data_requests:
        assert b"secret" not in request.content
        assert "secret" not in str(request.url)
        assert request.headers["authorization"] == "Bearer tok"


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_rejected_credentials_trigger_cooldown(
    settings: Settings, upstream: FakeUpstream, status: int
) -> None:
    clock = FakeClock()
    upstream.token_status = status
    finder = FuelFinderClient(settings, clock=clock)
    async with new_client(settings, upstream.transport()) as client:
        with pytest.raises(UpstreamError, match="rejected the server's credentials"):
            await finder.fetch_stations(client)
        # Fuel Finder locks accounts after repeated failures: do not keep trying.
        clock.now += 60
        with pytest.raises(UpstreamError, match="paused for another 240 s"):
            await finder.fetch_stations(client)
        assert upstream.calls["token"] == 1
        upstream.token_status = 200
        clock.now += 241
        assert await finder.fetch_stations(client)
    assert upstream.calls["token"] == 2


async def test_auth_lockout_message(settings: Settings, upstream: FakeUpstream) -> None:
    upstream.token_status = 429
    finder = FuelFinderClient(settings)
    async with new_client(settings, upstream.transport()) as client:
        with pytest.raises(UpstreamError, match="wait 5 minutes"):
            await finder.fetch_stations(client)


async def test_missing_credentials(settings: Settings, upstream: FakeUpstream) -> None:
    finder = FuelFinderClient(dataclasses.replace(settings, fuel_finder_client_secret=None))
    async with new_client(settings, upstream.transport()) as client:
        with pytest.raises(CredentialsMissingError, match="not configured"):
            await finder.fetch_stations(client)
    assert upstream.requests == []


async def test_404_past_last_batch_ends_pagination(settings: Settings) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == fuelfinder.TOKEN_PATH:
            return httpx2.Response(200, json={"data": {"access_token": "tok"}})
        batch = int(request.url.params["batch-number"])
        if batch <= 3:
            return httpx2.Response(200, json=[{"node_id": f"b{batch}"}])
        return httpx2.Response(404)

    async with client_for(settings, handler) as client:
        records = await FuelFinderClient(settings).fetch_stations(client)
    assert len(records) == 3


async def test_404_on_first_batch_is_an_error(settings: Settings, upstream: FakeUpstream) -> None:
    upstream.data_status = 404
    async with new_client(settings, upstream.transport()) as client:
        with pytest.raises(UpstreamError, match="404"):
            await FuelFinderClient(settings).fetch_stations(client)


async def test_expired_token_is_refreshed_once(settings: Settings) -> None:
    tokens = iter(["old", "new"])
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == fuelfinder.TOKEN_PATH:
            return httpx2.Response(200, json={"data": {"access_token": next(tokens)}})
        seen.append(request.headers["authorization"])
        if request.headers["authorization"] == "Bearer old":
            return httpx2.Response(401)
        return httpx2.Response(200, json=[])

    async with client_for(settings, handler) as client:
        assert await FuelFinderClient(settings).fetch_prices(client) == []
    assert seen == ["Bearer old", "Bearer new"]


async def test_redirects_are_not_followed(settings: Settings) -> None:
    hosts: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        hosts.append(request.url.host)
        return httpx2.Response(302, headers={"location": "https://attacker.example/steal"})

    async with client_for(settings, handler) as client:
        with pytest.raises(UpstreamError, match="HTTP 302"):
            await FuelFinderClient(settings).fetch_stations(client)
    assert hosts == ["www.fuel-finder.service.gov.uk"]  # the credentials never went anywhere else


async def test_oversized_response_is_refused(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fuelfinder, "MAX_PAGE_BYTES", 1024)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == fuelfinder.TOKEN_PATH:
            return httpx2.Response(200, json={"data": {"access_token": "tok"}})
        return httpx2.Response(200, json=[{"node_id": "x" * 100}] * 50)

    async with client_for(settings, handler) as client:
        with pytest.raises(UpstreamError, match="unexpectedly large"):
            await FuelFinderClient(settings).fetch_stations(client)


async def test_record_cap(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fuelfinder, "MAX_RECORDS", 3)
    upstream = FakeUpstream()
    async with new_client(settings, upstream.transport()) as client:
        with pytest.raises(UpstreamError, match="more than 3 records"):
            await FuelFinderClient(settings).fetch_prices(client)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"data": {"access_token": "bad token with spaces"}}',
        b'{"data": {"access_token": "line\\nbreak"}}',
        b'{"data": []}',
        b"[]",
    ],
)
async def test_unusable_token_responses(settings: Settings, body: bytes) -> None:
    async with client_for(settings, lambda request: httpx2.Response(200, content=body)) as client:
        with pytest.raises(UpstreamError):
            await FuelFinderClient(settings).access_token(client)


async def test_non_dict_records_are_skipped(settings: Settings) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == fuelfinder.TOKEN_PATH:
            return httpx2.Response(200, json={"data": {"access_token": "tok"}})
        if request.url.params["batch-number"] == "1":
            return httpx2.Response(200, json=[{"node_id": "a"}, "junk", 7, None, ["x"]])
        return httpx2.Response(200, json=[])

    async with client_for(settings, handler) as client:
        assert await FuelFinderClient(settings).fetch_stations(client) == [{"node_id": "a"}]


async def test_prices_path_is_used(settings: Settings, upstream: FakeUpstream) -> None:
    async with new_client(settings, upstream.transport()) as client:
        await FuelFinderClient(settings).fetch_prices(client)
    assert upstream.calls[f"{PRICES_PATH}#1"] == 1
