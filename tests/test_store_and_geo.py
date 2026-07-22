import pytest
from fixtures import BANGOR, RAW_PRICES, RAW_STATIONS, STATIONS

from fuel_prices_mcp import store
from fuel_prices_mcp.fuelfinder import ApiError
from fuel_prices_mcp.geo import haversine_miles, resolve_location


def test_haversine_known_distance():
    # Bangor to Caernarfon is roughly 8.5 miles
    d = haversine_miles(53.228, -4.128, 53.139, -4.274)
    assert 8 < d < 10


def test_resolve_latlon_passthrough():
    lat, lon, desc = resolve_location("53.228, -4.128")
    assert (round(lat, 3), round(lon, 3)) == (53.228, -4.128)


def test_resolve_rejects_non_uk_coords():
    with pytest.raises(ApiError, match="outside the UK"):
        resolve_location("40.7, -74.0")  # New York


def test_resolve_rejects_garbage():
    with pytest.raises(ApiError, match="not a UK postcode"):
        resolve_location("not a place")


def test_fuel_aliases():
    assert store.normalise_fuel("unleaded") == "E10"
    assert store.normalise_fuel("Diesel") == "B7_STANDARD"
    assert store.normalise_fuel("HVO") == "HVO"
    with pytest.raises(ApiError, match="Unknown fuel type"):
        store.normalise_fuel("rocket fuel")


def test_join_drops_closed_and_orphan_prices():
    stations = store._join(RAW_STATIONS, RAW_PRICES)
    ids = [s.node_id for s in stations]
    assert ids == ["n1"]  # closed station dropped, orphan price ignored
    assert stations[0].prices["E10"]["price_ppl"] == 139.9


def test_nearby_sorts_by_distance_and_respects_radius():
    lat, lon = BANGOR
    result = store.nearby(STATIONS, lat, lon, radius_miles=5)
    names = [s.name for s, _ in result]
    assert "Far Away Fuels" not in names
    distances = [d for _, d in result]
    assert distances == sorted(distances)


def test_price_staleness():
    assert store.price_is_stale("2020-01-01T00:00:00.000Z") is True
    assert store.price_is_stale(None) is True
