"""Structured tool results. Their JSON Schemas are published as each tool's ``outputSchema``.

Field names are unchanged from 0.1 so existing clients and evals keep working.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

ATTRIBUTION = (
    "Contains public sector information from the Fuel Finder scheme, licensed "
    "under the Open Government Licence v3.0. Prices are reported by forecourts "
    "and can occasionally lag or err; treat pump price as final. Spotted a "
    "wrong price? Report it: https://www.fuel-finder.service.gov.uk/motorist/price-report"
)

_REPORTED = "Reported by the forecourt operator (third-party data, sanitised to one line of plain text)."


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StationPrice(_Model):
    name: str = Field(description=f"Trading name. {_REPORTED}")
    brand: str | None = Field(description=f"Brand. {_REPORTED}")
    address: str | None = Field(description=f"First address line. {_REPORTED}")
    postcode: str | None = Field(description="UK postcode in 'OUT IN' form.")
    distance_miles: float
    price_ppl: float = Field(description="Pence per litre")
    vs_area_median_ppl: float | None = Field(None, description="Negative = cheaper than the local median")
    price_updated: str | None = Field(description="When the forecourt last updated this price (ISO 8601).")
    stale_price: bool = Field(description="True if the price is over 7 days old")
    is_supermarket: bool
    is_motorway: bool
    node_id: str = Field(description="Fuel Finder station identifier.")


class CheapestReport(_Model):
    location: str
    fuel: str
    radius_miles: float
    stations_in_radius: int
    area_median_ppl: float | None
    results: list[StationPrice]
    savings_note: str
    attribution: str = ATTRIBUTION


class NearbyStation(_Model):
    name: str = Field(description=f"Trading name. {_REPORTED}")
    brand: str | None = Field(description=f"Brand. {_REPORTED}")
    address: str | None = Field(description=f"First address line. {_REPORTED}")
    postcode: str | None = Field(description="UK postcode in 'OUT IN' form.")
    distance_miles: float
    prices: dict[str, float] = Field(description="Fuel label -> pence per litre")
    amenities: list[str] = Field(description="Amenity codes such as 'car_wash'.")
    is_supermarket: bool
    is_motorway: bool
    node_id: str = Field(description="Fuel Finder station identifier.")


class NearbyReport(_Model):
    location: str
    radius_miles: float
    count: int
    stations: list[NearbyStation]
    attribution: str = ATTRIBUTION


class FuelStat(_Model):
    fuel: str
    stations_selling: int
    min_ppl: float
    median_ppl: float
    max_ppl: float
    cheapest_station: str = Field(description=f"Trading name. {_REPORTED}")
    cheapest_postcode: str | None


class AreaSummary(_Model):
    location: str
    radius_miles: float
    stations_in_radius: int
    stats: list[FuelStat]
    spread_note: str
    attribution: str = ATTRIBUTION


class DataStatus(_Model):
    configured: bool = Field(description="Whether Fuel Finder credentials are configured on the server.")
    stations_loaded: int | None
    cache_age_seconds: int | None
    note: str
    last_error: str | None = Field(None, description="Why the last data refresh failed, if it did.")
