"""In-memory dataset: stations joined with prices, refreshed on a TTL.

The full UK dataset (~8,500 forecourts) is fetched once, joined on node_id
and kept in memory for CACHE_TTL_SECONDS, so repeated tool calls don't
hammer the API. The 5-minute TTL matches the Fuel Finder Fair Use Policy's
freshness expectation for price information shown to motorists.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from . import fuelfinder
from .geo import haversine_miles

CACHE_TTL_SECONDS = 5 * 60

# API fuel_type codes -> human labels
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


@dataclass
class Station:
    node_id: str
    name: str
    brand: str | None
    postcode: str | None
    address: str | None
    city: str | None
    latitude: float | None
    longitude: float | None
    is_motorway: bool
    is_supermarket: bool
    amenities: list[str] = field(default_factory=list)
    opening_times: dict = field(default_factory=dict)
    prices: dict[str, dict] = field(default_factory=dict)  # fuel_type -> {price, updated}


_cache: dict = {"stations": None, "fetched_at": 0.0}


def normalise_fuel(fuel: str) -> str:
    code = FUEL_ALIASES.get(fuel.strip().lower())
    if code is None:
        raise fuelfinder.ApiError(
            f"Unknown fuel type '{fuel}'. Use one of: unleaded/E10, super/E5, "
            "diesel/B7, premium diesel, B10, HVO."
        )
    return code


def _join(stations_raw: list[dict], prices_raw: list[dict]) -> list[Station]:
    by_id: dict[str, Station] = {}
    for s in stations_raw:
        if s.get("permanent_closure"):
            continue
        loc = s.get("location") or {}
        by_id[s["node_id"]] = Station(
            node_id=s["node_id"],
            name=s.get("trading_name") or "",
            brand=s.get("brand_name"),
            postcode=loc.get("postcode"),
            address=loc.get("address_line_1"),
            city=loc.get("city"),
            latitude=loc.get("latitude"),
            longitude=loc.get("longitude"),
            is_motorway=bool(s.get("is_motorway_service_station")),
            is_supermarket=bool(s.get("is_supermarket_service_station")),
            amenities=s.get("amenities") or [],
            opening_times=s.get("opening_times") or {},
        )
    for p in prices_raw:
        st = by_id.get(p.get("node_id"))
        if st is None:
            continue
        for fp in p.get("fuel_prices") or []:
            code = fp.get("fuel_type")
            if code and fp.get("price"):
                st.prices[code] = {
                    "price_ppl": float(fp["price"]),
                    "updated": fp.get("price_last_updated"),
                }
    return [s for s in by_id.values() if s.latitude is not None and s.longitude is not None]


def get_stations(client: httpx.Client | None = None, force_refresh: bool = False) -> list[Station]:
    if (
        not force_refresh
        and _cache["stations"] is not None
        and time.time() - _cache["fetched_at"] < CACHE_TTL_SECONDS
    ):
        return _cache["stations"]
    own = client is None
    client = client or httpx.Client(timeout=60)
    try:
        stations = _join(fuelfinder.fetch_stations(client), fuelfinder.fetch_prices(client))
    finally:
        if own:
            client.close()
    _cache["stations"] = stations
    _cache["fetched_at"] = time.time()
    return stations


def cache_age_seconds() -> int | None:
    if _cache["stations"] is None:
        return None
    return int(time.time() - _cache["fetched_at"])


def nearby(
    stations: list[Station], lat: float, lon: float, radius_miles: float
) -> list[tuple[Station, float]]:
    out = []
    for s in stations:
        d = haversine_miles(lat, lon, s.latitude, s.longitude)
        if d <= radius_miles:
            out.append((s, round(d, 2)))
    out.sort(key=lambda t: t[1])
    return out


def price_is_stale(updated: str | None, days: int = 7) -> bool:
    if not updated:
        return True
    try:
        ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(ts.tzinfo) - ts > timedelta(days=days)
