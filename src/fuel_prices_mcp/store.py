"""In-memory dataset: stations joined with prices, refreshed on a TTL.

The full UK dataset (~8,500 forecourts) is fetched once, joined on node_id and
kept in memory for at most 5 minutes (the Fuel Finder Fair Use Policy's
freshness expectation), so repeated tool calls do not hammer the API.

Every upstream record is validated and sanitised here, once, at ingest: all
tool output is derived from these cleaned ``Station`` objects. Refreshes are
single-flight (concurrent tool calls share one download) and a failed refresh
is not retried for a short back-off period, so a burst of tool calls cannot be
turned into a burst of upstream requests.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import anyio
import httpx2

from fuel_prices_mcp.config import Settings
from fuel_prices_mcp.fuelfinder import CredentialsMissingError, FuelFinderClient
from fuel_prices_mcp.geo import Geocoder, Location, haversine_miles
from fuel_prices_mcp.sanitize import (
    clean_amenities,
    clean_coordinates,
    clean_identifier,
    clean_optional_text,
    clean_postcode,
    clean_price,
    clean_text,
    clean_timestamp,
)
from fuel_prices_mcp.upstream import InvalidInputError, UpstreamError, new_client

logger = logging.getLogger(__name__)

REFRESH_FAILURE_BACKOFF_SECONDS = 30.0

MAX_NAME_LEN = 120
MAX_BRAND_LEN = 80
MAX_ADDRESS_LEN = 200
MAX_CITY_LEN = 80

# API fuel_type codes -> human labels. Codes not listed here are ignored at ingest.
FUEL_LABELS = {
    "E10": "Unleaded (E10)",
    "E5": "Super unleaded (E5)",
    "B7_STANDARD": "Diesel (B7)",
    "B7_PREMIUM": "Premium diesel (B7)",
    "B10": "B10 diesel",
    "HVO": "HVO renewable diesel",
}
FUEL_ALIASES = {
    "unleaded": "E10",
    "petrol": "E10",
    "e10": "E10",
    "super": "E5",
    "super unleaded": "E5",
    "e5": "E5",
    "diesel": "B7_STANDARD",
    "b7": "B7_STANDARD",
    "b7_standard": "B7_STANDARD",
    "premium diesel": "B7_PREMIUM",
    "b7_premium": "B7_PREMIUM",
    "b10": "B10",
    "hvo": "HVO",
}


@dataclass(frozen=True, slots=True)
class Price:
    ppl: float
    updated: str | None


@dataclass(slots=True)
class Station:
    node_id: str
    name: str
    brand: str | None
    postcode: str | None
    address: str | None
    city: str | None
    latitude: float
    longitude: float
    is_motorway: bool
    is_supermarket: bool
    amenities: list[str] = field(default_factory=list)
    prices: dict[str, Price] = field(default_factory=dict)  # fuel code -> price


def normalise_fuel(fuel: str) -> str:
    code = FUEL_ALIASES.get(" ".join(fuel.lower().split()))
    if code is None:
        shown = clean_text(fuel, max_len=32)
        raise InvalidInputError(
            f"Unknown fuel type '{shown}'. Use one of: unleaded/E10, super/E5, diesel/B7, "
            "premium diesel, B10, HVO."
        )
    return code


def _flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() == "true"


def join(stations_raw: list[dict[str, Any]], prices_raw: list[dict[str, Any]]) -> list[Station]:
    """Validate, sanitise and join the raw API records. Anything malformed is dropped."""
    by_id: dict[str, Station] = {}
    for raw in stations_raw:
        if not isinstance(raw, dict) or _flag(raw.get("permanent_closure")):
            continue
        node_id = clean_identifier(raw.get("node_id"))
        loc = raw.get("location")
        loc = loc if isinstance(loc, dict) else {}
        coords = clean_coordinates(loc.get("latitude"), loc.get("longitude"))
        if node_id is None or coords is None:
            continue
        by_id[node_id] = Station(
            node_id=node_id,
            name=clean_optional_text(raw.get("trading_name"), max_len=MAX_NAME_LEN) or "",
            brand=clean_optional_text(raw.get("brand_name"), max_len=MAX_BRAND_LEN),
            postcode=clean_postcode(loc.get("postcode")),
            address=clean_optional_text(loc.get("address_line_1"), max_len=MAX_ADDRESS_LEN),
            city=clean_optional_text(loc.get("city"), max_len=MAX_CITY_LEN),
            latitude=coords[0],
            longitude=coords[1],
            is_motorway=_flag(raw.get("is_motorway_service_station")),
            is_supermarket=_flag(raw.get("is_supermarket_service_station")),
            amenities=clean_amenities(raw.get("amenities")),
        )
    for raw in prices_raw:
        if not isinstance(raw, dict):
            continue
        node_id = clean_identifier(raw.get("node_id"))
        station = by_id.get(node_id) if node_id else None
        fuel_prices = raw.get("fuel_prices")
        if station is None or not isinstance(fuel_prices, list):
            continue
        for entry in fuel_prices:
            if not isinstance(entry, dict):
                continue
            code = entry.get("fuel_type")
            price = clean_price(entry.get("price"))
            if code in FUEL_LABELS and price is not None:
                station.prices[code] = Price(price, clean_timestamp(entry.get("price_last_updated")))
    return list(by_id.values())


def nearby(
    stations: list[Station], lat: float, lon: float, radius_miles: float
) -> list[tuple[Station, float]]:
    out = []
    for station in stations:
        distance = haversine_miles(lat, lon, station.latitude, station.longitude)
        if distance <= radius_miles:
            out.append((station, round(distance, 2)))
    out.sort(key=lambda item: item[1])
    return out


def price_is_stale(updated: str | None, days: int = 7) -> bool:
    if not updated:
        return True
    try:
        ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(ts.tzinfo) - ts > timedelta(days=days)


@dataclass(frozen=True, slots=True)
class DataState:
    configured: bool
    stations_loaded: int | None
    cache_age_seconds: int | None
    last_error: str | None


class FuelData:
    """The server's single source of station data and geocoding."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._clock = clock
        self.finder = FuelFinderClient(settings, clock=clock)
        self._geocoder = Geocoder(clock=clock)
        self._stations: list[Station] | None = None
        self._fetched_at = 0.0
        self._failed_at: float | None = None
        self._last_error: str | None = None
        self._lock = anyio.Lock()

    def client(self) -> httpx2.AsyncClient:
        return new_client(self._settings, self._transport)

    def _fresh(self) -> bool:
        return (
            self._stations is not None and self._clock() - self._fetched_at < self._settings.cache_ttl_seconds
        )

    async def stations(self) -> list[Station]:
        if self._fresh():
            return self._stations  # type: ignore[return-value]
        async with self._lock:
            if self._fresh():  # another task refreshed while we waited
                return self._stations  # type: ignore[return-value]
            now = self._clock()
            if self._failed_at is not None and now - self._failed_at < REFRESH_FAILURE_BACKOFF_SECONDS:
                raise UpstreamError(self._last_error or "The Fuel Finder data is temporarily unavailable.")
            try:
                stations = await self._download()
            except UpstreamError as exc:
                self._record_failure(str(exc), backoff=not isinstance(exc, CredentialsMissingError))
                raise
            self._stations = stations
            self._fetched_at = self._clock()
            self._failed_at = None
            self._last_error = None
            logger.info("Loaded Fuel Finder data", extra={"event": {"stations": len(stations)}})
            return stations

    def _record_failure(self, message: str, *, backoff: bool) -> None:
        self._last_error = message
        if backoff:
            self._failed_at = self._clock()

    async def _download(self) -> list[Station]:
        timeout = self._settings.refresh_timeout
        try:
            with anyio.fail_after(timeout):
                async with self.client() as client:
                    stations_raw = await self.finder.fetch_stations(client)
                    prices_raw = await self.finder.fetch_prices(client)
        except TimeoutError as exc:
            raise UpstreamError(f"The Fuel Finder API did not respond within {timeout:g} seconds.") from exc
        except httpx2.HTTPError as exc:
            # Log the class only: messages can carry URLs and are not useful to the model.
            logger.warning("Fuel Finder request failed: %s", type(exc).__name__)
            raise UpstreamError("Could not reach the Fuel Finder API; try again shortly.") from exc
        return join(stations_raw, prices_raw)

    async def locate(self, text: str) -> Location:
        return await self._geocoder.resolve(text, self.client)

    def state(self) -> DataState:
        return DataState(
            configured=self.finder.configured,
            stations_loaded=len(self._stations) if self._stations is not None else None,
            cache_age_seconds=(int(self._clock() - self._fetched_at) if self._stations is not None else None),
            last_error=self._last_error,
        )
