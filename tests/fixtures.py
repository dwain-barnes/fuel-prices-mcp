"""Test data: invented forecourts around Bangor, Gwynedd (53.228, -4.128)."""

from fuel_prices_mcp.store import Station

BANGOR = (53.228, -4.128)


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
        opening_times={},
        prices={
            code: {"price_ppl": ppl, "updated": "2026-07-21T09:00:00.000Z"}
            for code, ppl in prices.items()
        },
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

# Raw API payload shapes for client/join tests
RAW_STATIONS = [
    {
        "node_id": "n1",
        "trading_name": "SuperMart Bangor",
        "brand_name": "SuperMart",
        "temporary_closure": False,
        "permanent_closure": False,
        "is_motorway_service_station": False,
        "is_supermarket_service_station": True,
        "location": {
            "address_line_1": "High Street",
            "city": "Bangor",
            "postcode": "LL57 1AA",
            "latitude": 53.230,
            "longitude": -4.130,
        },
        "amenities": ["car_wash"],
        "opening_times": {},
        "fuel_types": ["E10", "B7_STANDARD"],
    },
    {
        "node_id": "n6-closed",
        "trading_name": "Gone Garage",
        "brand_name": "Gone",
        "permanent_closure": True,
        "location": {"latitude": 53.2, "longitude": -4.1, "postcode": "LL57 9ZZ"},
    },
]

RAW_PRICES = [
    {
        "node_id": "n1",
        "trading_name": "SuperMart Bangor",
        "fuel_prices": [
            {"fuel_type": "E10", "price": 139.9, "price_last_updated": "2026-07-21T09:00:00.000Z"},
            {
                "fuel_type": "B7_STANDARD",
                "price": 146.9,
                "price_last_updated": "2026-07-21T09:00:00.000Z",
            },
        ],
    },
    {"node_id": "n-unknown", "fuel_prices": [{"fuel_type": "E10", "price": 999.9}]},
]
