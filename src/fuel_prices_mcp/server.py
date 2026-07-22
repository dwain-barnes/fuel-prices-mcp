"""fuel-prices-mcp — MCP server for UK fuel prices over stdio.

Data: the official Fuel Finder open data scheme (Motor Fuel Price (Open
Data) Regulations 2025) — every UK forecourt, prices updated within 30
minutes of a change. Geocoding via postcodes.io (keyless). Ranking and
statistics are computed locally.
"""

from __future__ import annotations

from statistics import median

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import store
from .geo import resolve_location

mcp = FastMCP(
    "fuelprices",
    instructions=(
        "UK fuel price tools using the official Fuel Finder open data (all UK "
        "forecourts, 30-minute freshness). Use fuel_find_cheapest for 'cheapest "
        "petrol near X'; fuel_area_summary for whether a price is good locally. "
        "Prices are pence per litre."
    ),
)

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)

ATTRIBUTION = (
    "Contains public sector information from the Fuel Finder scheme, licensed "
    "under the Open Government Licence v3.0. Prices are reported by forecourts "
    "and can occasionally lag or err — treat pump price as final. Spotted a "
    "wrong price? Report it: https://www.fuel-finder.service.gov.uk/motorist/price-report"
)


class StationPrice(BaseModel):
    name: str
    brand: str | None
    address: str | None
    postcode: str | None
    distance_miles: float
    price_ppl: float = Field(description="Pence per litre")
    vs_area_median_ppl: float | None = Field(
        None, description="Negative = cheaper than the local median"
    )
    price_updated: str | None
    stale_price: bool = Field(description="True if the price is over 7 days old")
    is_supermarket: bool
    is_motorway: bool
    node_id: str


class CheapestReport(BaseModel):
    location: str
    fuel: str
    radius_miles: float
    stations_in_radius: int
    area_median_ppl: float | None
    results: list[StationPrice]
    savings_note: str
    attribution: str = ATTRIBUTION


class NearbyStation(BaseModel):
    name: str
    brand: str | None
    address: str | None
    postcode: str | None
    distance_miles: float
    prices: dict[str, float] = Field(description="Fuel label -> pence per litre")
    amenities: list[str]
    is_supermarket: bool
    is_motorway: bool
    node_id: str


class NearbyReport(BaseModel):
    location: str
    radius_miles: float
    count: int
    stations: list[NearbyStation]
    attribution: str = ATTRIBUTION


class FuelStat(BaseModel):
    fuel: str
    stations_selling: int
    min_ppl: float
    median_ppl: float
    max_ppl: float
    cheapest_station: str
    cheapest_postcode: str | None


class AreaSummary(BaseModel):
    location: str
    radius_miles: float
    stations_in_radius: int
    stats: list[FuelStat]
    spread_note: str
    attribution: str = ATTRIBUTION


class DataStatus(BaseModel):
    configured: bool
    stations_loaded: int | None
    cache_age_seconds: int | None
    note: str


def _stations():
    return store.get_stations()


@mcp.tool(name="fuel_find_cheapest", annotations=_READ_ONLY)
def fuel_find_cheapest(
    location: str = Field(description="UK postcode ('LL57 2DG'), outcode ('LL57') or 'lat,lon'"),
    fuel: str = Field(
        default="unleaded",
        description="unleaded/E10, super/E5, diesel/B7, premium diesel, B10 or HVO",
    ),
    radius_miles: float = Field(default=5, ge=0.5, le=50),
    top_k: int = Field(default=10, ge=1, le=25),
) -> CheapestReport:
    """Find the cheapest fuel near a location, ranked by price, with each
    station's price compared to the local median. Prices are pence per litre
    from the official Fuel Finder data (updated within 30 minutes)."""
    code = store.normalise_fuel(fuel)
    lat, lon, desc = resolve_location(location)
    in_radius = store.nearby(_stations(), lat, lon, radius_miles)
    priced = [(s, d) for s, d in in_radius if code in s.prices]
    prices = [s.prices[code]["price_ppl"] for s, _ in priced]
    area_median = round(median(prices), 1) if prices else None
    priced.sort(key=lambda t: t[0].prices[code]["price_ppl"])
    results = [
        StationPrice(
            name=s.name,
            brand=s.brand,
            address=s.address,
            postcode=s.postcode,
            distance_miles=d,
            price_ppl=s.prices[code]["price_ppl"],
            vs_area_median_ppl=(
                round(s.prices[code]["price_ppl"] - area_median, 1) if area_median else None
            ),
            price_updated=s.prices[code]["updated"],
            stale_price=store.price_is_stale(s.prices[code]["updated"]),
            is_supermarket=s.is_supermarket,
            is_motorway=s.is_motorway,
            node_id=s.node_id,
        )
        for s, d in priced[:top_k]
    ]
    if len(prices) >= 2:
        spread = max(prices) - min(prices)
        savings = (
            f"Spread in this area: {spread:.1f}p/litre between cheapest and dearest — "
            f"about £{spread * 55 / 100:.2f} on a 55-litre tank."
        )
    else:
        savings = "Not enough stations selling this fuel here to compare — try a wider radius."
    return CheapestReport(
        location=desc,
        fuel=store.FUEL_LABELS[code],
        radius_miles=radius_miles,
        stations_in_radius=len(priced),
        area_median_ppl=area_median,
        results=results,
        savings_note=savings,
    )


@mcp.tool(name="fuel_nearby_stations", annotations=_READ_ONLY)
def fuel_nearby_stations(
    location: str = Field(description="UK postcode, outcode or 'lat,lon'"),
    radius_miles: float = Field(default=3, ge=0.5, le=50),
    max_results: int = Field(default=15, ge=1, le=40),
) -> NearbyReport:
    """List forecourts near a location (nearest first) with all their fuel
    prices, brand, amenities and motorway/supermarket flags."""
    lat, lon, desc = resolve_location(location)
    in_radius = store.nearby(_stations(), lat, lon, radius_miles)[:max_results]
    stations = [
        NearbyStation(
            name=s.name,
            brand=s.brand,
            address=s.address,
            postcode=s.postcode,
            distance_miles=d,
            prices={
                store.FUEL_LABELS[c]: p["price_ppl"]
                for c, p in sorted(s.prices.items())
                if c in store.FUEL_LABELS
            },
            amenities=s.amenities,
            is_supermarket=s.is_supermarket,
            is_motorway=s.is_motorway,
            node_id=s.node_id,
        )
        for s, d in in_radius
    ]
    return NearbyReport(
        location=desc, radius_miles=radius_miles, count=len(stations), stations=stations
    )


@mcp.tool(name="fuel_area_summary", annotations=_READ_ONLY)
def fuel_area_summary(
    location: str = Field(description="UK postcode, outcode or 'lat,lon'"),
    radius_miles: float = Field(default=10, ge=1, le=50),
) -> AreaSummary:
    """Per-fuel price statistics for an area: min/median/max and the cheapest
    station for each fuel. Use it to judge whether a price you've seen is
    actually good locally."""
    lat, lon, desc = resolve_location(location)
    in_radius = store.nearby(_stations(), lat, lon, radius_miles)
    stats: list[FuelStat] = []
    for code, label in store.FUEL_LABELS.items():
        sellers = [(s, s.prices[code]["price_ppl"]) for s, _ in in_radius if code in s.prices]
        if not sellers:
            continue
        sellers.sort(key=lambda t: t[1])
        prices = [p for _, p in sellers]
        stats.append(
            FuelStat(
                fuel=label,
                stations_selling=len(sellers),
                min_ppl=min(prices),
                median_ppl=round(median(prices), 1),
                max_ppl=max(prices),
                cheapest_station=sellers[0][0].name,
                cheapest_postcode=sellers[0][0].postcode,
            )
        )
    e10 = next((st for st in stats if st.fuel == store.FUEL_LABELS["E10"]), None)
    spread_note = (
        f"Unleaded spread here is {e10.max_ppl - e10.min_ppl:.1f}p/litre across "
        f"{e10.stations_selling} stations."
        if e10
        else "No unleaded price data in this radius."
    )
    return AreaSummary(
        location=desc,
        radius_miles=radius_miles,
        stations_in_radius=len(in_radius),
        stats=stats,
        spread_note=spread_note,
    )


@mcp.tool(name="fuel_data_status", annotations=_READ_ONLY)
def fuel_data_status() -> DataStatus:
    """Check the server's data: credentials configured, stations loaded, and
    cache age. Use this to diagnose empty results."""
    from . import fuelfinder

    configured = fuelfinder.credentials() is not None
    loaded = len(store._cache["stations"]) if store._cache["stations"] is not None else None
    note = (
        "Data refreshes automatically every 15 minutes on use."
        if configured
        else f"Not configured. {fuelfinder.REGISTER}"
    )
    return DataStatus(
        configured=configured,
        stations_loaded=loaded,
        cache_age_seconds=store.cache_age_seconds(),
        note=note,
    )


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
