"""Location handling: postcode geocoding (postcodes.io, keyless) and great-circle distance.

Only three input shapes are accepted (full postcode, outward code, or a UK
``lat,lon`` pair), each matched by an anchored pattern with bounded repetition
before anything leaves the process. The postcode is the only piece of tool
input that is ever sent upstream, and only after it has been canonicalised to
letters, digits and one space.

Successful lookups are cached in memory (bounded LRU, 24 h) so repeated
questions do not re-send the user's postcode to a third party.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt
from typing import Literal

import httpx2

from fuel_prices_mcp.sanitize import OUTCODE_RE, clean_coordinates, clean_postcode, clean_text
from fuel_prices_mcp.upstream import InvalidInputError, UpstreamError, request_json

POSTCODES_IO = "https://api.postcodes.io"
SERVICE = "postcodes.io"
EARTH_RADIUS_MILES = 3958.8
MAX_LOCATION_LEN = 64
MAX_RESPONSE_BYTES = 256 * 1024
CACHE_SIZE = 4096
CACHE_TTL_SECONDS = 24 * 3600.0

_LATLON_RE = re.compile(r"^\s*(-?\d{1,3}(?:\.\d{1,10})?)\s*,\s*(-?\d{1,3}(?:\.\d{1,10})?)\s*$")

LocationKind = Literal["postcode", "outcode", "latlon"]


@dataclass(frozen=True, slots=True)
class Location:
    latitude: float
    longitude: float
    description: str
    kind: LocationKind


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * asin(sqrt(a))


def parse_location(text: str) -> tuple[LocationKind, str] | Location:
    """Classify ``text`` without any network access.

    Returns a finished ``Location`` for coordinates, or ``(kind, canonical)`` for a
    postcode or outward code that still needs geocoding.
    """
    if len(text) > MAX_LOCATION_LEN:
        raise InvalidInputError(f"Location must be at most {MAX_LOCATION_LEN} characters.")
    match = _LATLON_RE.fullmatch(text)
    if match:
        coords = clean_coordinates(float(match.group(1)), float(match.group(2)))
        if coords is None:
            raise InvalidInputError(
                "Those coordinates are outside the UK. Pass a UK postcode or a UK 'lat,lon' pair."
            )
        lat, lon = coords
        return Location(lat, lon, f"{lat:.4f}, {lon:.4f}", "latlon")

    postcode = clean_postcode(text)
    if postcode:
        return "postcode", postcode
    outcode = " ".join(text.upper().split())
    if OUTCODE_RE.fullmatch(outcode):
        return "outcode", outcode
    shown = clean_text(text, max_len=MAX_LOCATION_LEN)
    raise InvalidInputError(
        f"'{shown}' is not a UK postcode (e.g. 'LL57 2DG'), outward code (e.g. 'LL57') or 'lat,lon' pair."
    )


class Geocoder:
    def __init__(self, *, base_url: str = POSTCODES_IO, clock: Callable[[], float] = time.monotonic) -> None:
        self._base_url = base_url
        self._clock = clock
        self._cache: OrderedDict[str, tuple[float, Location]] = OrderedDict()

    def _cached(self, key: str) -> Location | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        stored_at, location = entry
        if self._clock() - stored_at > CACHE_TTL_SECONDS:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return location

    def _store(self, key: str, location: Location) -> None:
        self._cache[key] = (self._clock(), location)
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)

    async def resolve(self, text: str, client_factory: Callable[[], httpx2.AsyncClient]) -> Location:
        parsed = parse_location(text)
        if isinstance(parsed, Location):
            return parsed
        kind, canonical = parsed
        key = f"{kind}:{canonical}"
        cached = self._cached(key)
        if cached is not None:
            return cached

        # canonical is [A-Z0-9 ] only (validated above), so it is safe as a path segment.
        if kind == "postcode":
            url = f"{self._base_url}/postcodes/{canonical.replace(' ', '')}"
        else:
            url = f"{self._base_url}/outcodes/{canonical}"
        async with client_factory() as client:
            try:
                status, payload = await request_json(
                    client, "GET", url, max_bytes=MAX_RESPONSE_BYTES, service=SERVICE
                )
            except httpx2.HTTPError as exc:
                raise UpstreamError(
                    "Could not reach postcodes.io to look up that postcode; try again shortly."
                ) from exc
        if status == 404:
            raise InvalidInputError(f"Postcode '{canonical}' was not found. Check for typos.")
        if status == 429:
            raise UpstreamError("postcodes.io is rate limiting lookups; try again in a minute.")
        if status != 200:
            raise UpstreamError(f"postcodes.io returned HTTP {status}; try again shortly.")
        result = payload.get("result") if isinstance(payload, dict) else None
        coords = clean_coordinates(
            result.get("latitude") if isinstance(result, dict) else None,
            result.get("longitude") if isinstance(result, dict) else None,
        )
        if coords is None:
            raise UpstreamError(
                f"postcodes.io has no UK coordinates for '{canonical}'. Try a nearby postcode or 'lat,lon'."
            )
        location = Location(coords[0], coords[1], canonical, kind)
        self._store(key, location)
        return location
