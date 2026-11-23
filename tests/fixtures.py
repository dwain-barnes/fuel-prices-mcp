"""Test data: invented forecourts around Bangor, Gwynedd (53.228, -4.128), and a fake upstream.

``FakeUpstream`` is an ``httpx2.MockTransport`` handler that emulates both the Fuel
Finder API (token endpoint plus batched station/price endpoints) and postcodes.io,
so tool calls can be tested end to end without network access or credentials.
"""

from __future__ import annotations

import socket
from collections import Counter
from typing import Any

import anyio
import httpx2

from fuel_prices_mcp.fuelfinder import PRICES_PATH, STATIONS_PATH, TOKEN_PATH
from fuel_prices_mcp.store import Price, Station

BANGOR = (53.228, -4.128)
UPDATED = "2026-07-21T09:00:00.000Z"
FUEL_FINDER_HOST = "www.fuel-finder.service.gov.uk"
POSTCODES_HOST = "api.postcodes.io"


def station(node_id, name, lat, lon, prices, brand=None, supermarket=False, motorway=False):
    return Station(
        node_id=node_id,
        name=name,
        brand=brand or name.split()[0],
        postcode="LL57 1AA",
        address="High Street",
        city="Bangor",
        latitude=lat,
        longitude=lon,
        is_motorway=motorway,
        is_supermarket=supermarket,
        amenities=["car_wash"],
        prices={code: Price(ppl, UPDATED) for code, ppl in prices.items()},
    )


STATIONS = [
    station(
        "n1",
        "SuperMart Bangor",
        53.230,
        -4.130,
        {"E10": 139.9, "B7_STANDARD": 146.9},
        supermarket=True,
    ),
    station("n2", "Indie Garage", 53.235, -4.120, {"E10": 144.9, "E5": 158.9}),
    station("n3", "Coastal Fuels", 53.220, -4.140, {"E10": 141.9, "B7_STANDARD": 149.9}),
    station(
        "n4",
        "Motorway Services A55",
        53.250,
        -4.100,
        {"E10": 155.9, "B7_STANDARD": 162.9},
        motorway=True,
    ),
    station("n5", "Far Away Fuels", 53.500, -3.500, {"E10": 130.9}),  # ~30 miles away
]


def raw_station(s: Station) -> dict[str, Any]:
    """The Fuel Finder /api/v1/pfs record for a fixture station."""
    return {
        "node_id": s.node_id,
        "trading_name": s.name,
        "brand_name": s.brand,
        "temporary_closure": False,
        "permanent_closure": False,
        "is_motorway_service_station": s.is_motorway,
        "is_supermarket_service_station": s.is_supermarket,
        "location": {
            "address_line_1": s.address,
            "city": s.city,
            "postcode": s.postcode,
            "latitude": s.latitude,
            "longitude": s.longitude,
        },
        "amenities": list(s.amenities),
        "opening_times": {},
        "fuel_types": sorted(s.prices),
    }


def raw_prices(s: Station) -> dict[str, Any]:
    """The Fuel Finder /api/v1/pfs/fuel-prices record for a fixture station."""
    return {
        "node_id": s.node_id,
        "trading_name": s.name,
        "fuel_prices": [
            {"fuel_type": code, "price": p.ppl, "price_last_updated": p.updated}
            for code, p in s.prices.items()
        ],
    }


# Raw API payload shapes for join tests
RAW_STATIONS = [
    raw_station(STATIONS[0]),
    {
        "node_id": "n6-closed",
        "trading_name": "Gone Garage",
        "brand_name": "Gone",
        "permanent_closure": True,
        "location": {"latitude": 53.2, "longitude": -4.1, "postcode": "LL57 9ZZ"},
    },
]

RAW_PRICES = [
    raw_prices(STATIONS[0]),
    {"node_id": "n-unknown", "fuel_prices": [{"fuel_type": "E10", "price": 999.9}]},
]


class FakeUpstream:
    """Emulates Fuel Finder and postcodes.io. Counts every call so tests can assert on traffic."""

    def __init__(
        self,
        stations: list[dict[str, Any]] | None = None,
        prices: list[dict[str, Any]] | None = None,
        *,
        batch_size: int = 2,
        delay: float = 0.0,
    ) -> None:
        self.stations = [raw_station(s) for s in STATIONS] if stations is None else stations
        self.prices = [raw_prices(s) for s in STATIONS] if prices is None else prices
        self.batch_size = batch_size
        self.delay = delay
        self.calls: Counter[str] = Counter()
        self.requests: list[httpx2.Request] = []
        self.token_status = 200
        self.data_status: int | None = None  # force a status on the data endpoints
        self.postcodes: dict[str, tuple[float | None, float | None]] = {
            "LL571AA": BANGOR,
            "SW1A1AA": (51.501, -0.141),
            "GY11AA": (None, None),  # postcodes.io returns null coordinates for some postcodes
        }
        self.outcodes: dict[str, tuple[float, float]] = {"LL57": BANGOR}

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self)

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.delay:
            await anyio.sleep(self.delay)
        host, path = request.url.host, request.url.path
        if host == POSTCODES_HOST:
            return self._postcodes(path)
        if host == FUEL_FINDER_HOST:
            return self._fuel_finder(request, path)
        self.calls[f"unexpected:{host}"] += 1
        return httpx2.Response(599)

    def _postcodes(self, path: str) -> httpx2.Response:
        self.calls["postcodes"] += 1
        kind, _, code = path.strip("/").partition("/")
        table: dict[str, Any] = self.postcodes if kind == "postcodes" else self.outcodes
        if code not in table:
            return httpx2.Response(404, json={"status": 404, "error": "Postcode not found"})
        lat, lon = table[code]
        return httpx2.Response(200, json={"status": 200, "result": {"latitude": lat, "longitude": lon}})

    def _fuel_finder(self, request: httpx2.Request, path: str) -> httpx2.Response:
        if path == TOKEN_PATH:
            self.calls["token"] += 1
            if self.token_status != 200:
                return httpx2.Response(self.token_status, json={"success": False})
            return httpx2.Response(
                200, json={"success": True, "data": {"access_token": "tok", "expires_in": 3600}}
            )
        if path in (STATIONS_PATH, PRICES_PATH):
            batch = int(request.url.params["batch-number"])
            self.calls[f"{path}#{batch}"] += 1
            if request.headers.get("authorization") != "Bearer tok":
                return httpx2.Response(401)
            if self.data_status is not None:
                return httpx2.Response(self.data_status)
            records = self.stations if path == STATIONS_PATH else self.prices
            start = (batch - 1) * self.batch_size
            return httpx2.Response(200, json=records[start : start + self.batch_size])
        return httpx2.Response(404)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
