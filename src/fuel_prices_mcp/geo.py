"""Location handling: postcode geocoding (postcodes.io, keyless) and
great-circle distance."""

from __future__ import annotations

import re
from math import asin, cos, radians, sin, sqrt

import httpx

from .fuelfinder import ApiError

POSTCODES_IO = "https://api.postcodes.io"
EARTH_RADIUS_MILES = 3958.8

_POSTCODE_RE = re.compile(r"^[A-Za-z]{1,2}\d[A-Za-z0-9]?\s*\d[A-Za-z]{2}$")
_OUTCODE_RE = re.compile(r"^[A-Za-z]{1,2}\d[A-Za-z0-9]?$")
_LATLON_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * asin(sqrt(a))


def resolve_location(location: str, client: httpx.Client | None = None) -> tuple[float, float, str]:
    """Turn a postcode, outcode or 'lat,lon' string into coordinates.

    Returns (latitude, longitude, description).
    """
    m = _LATLON_RE.match(location)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if not (49.5 <= lat <= 61.0 and -8.5 <= lon <= 2.0):
            raise ApiError(
                f"Coordinates ({lat}, {lon}) are outside the UK. Pass a UK postcode or UK lat,lon."
            )
        return lat, lon, f"{lat:.4f}, {lon:.4f}"

    query = location.strip().upper()
    own = client is None
    client = client or httpx.Client(timeout=15)
    try:
        if _POSTCODE_RE.match(query):
            resp = client.get(f"{POSTCODES_IO}/postcodes/{query.replace(' ', '')}")
        elif _OUTCODE_RE.match(query):
            resp = client.get(f"{POSTCODES_IO}/outcodes/{query}")
        else:
            raise ApiError(
                f"'{location}' is not a UK postcode (e.g. 'LL57 2DG'), outcode "
                "(e.g. 'LL57') or 'lat,lon' pair."
            )
        if resp.status_code == 404:
            raise ApiError(f"Postcode '{location}' not found. Check for typos.")
        if resp.status_code != 200:
            raise ApiError(f"postcodes.io returned {resp.status_code}; try again shortly.")
        result = resp.json()["result"]
        return result["latitude"], result["longitude"], query
    finally:
        if own:
            client.close()
