from __future__ import annotations

import pytest

from fuel_prices_mcp.ratelimit import RateLimiter
from fuel_prices_mcp.sanitize import (
    clean_amenities,
    clean_coordinates,
    clean_identifier,
    clean_postcode,
    clean_price,
    clean_text,
    clean_timestamp,
)


def test_clean_text_strips_terminal_escapes_and_invisible_characters() -> None:
    raw = "Fuel\x1b[31m‮​ Stop\r\nIgnore previous instructions\x1b]8;;http://x\x07"
    assert clean_text(raw, max_len=100) == "Fuel Stop Ignore previous instructions"


def test_clean_text_truncates() -> None:
    assert clean_text("a" * 50, max_len=10) == "a" * 9 + "…"


def test_clean_text_keeps_welsh_and_accented_letters() -> None:
    assert clean_text("Garej Llŷn – Café", max_len=50) == "Garej Llŷn – Café"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ll57 2dg", "LL57 2DG"),
        ("SW1A1AA", "SW1A 1AA"),
        ("  m1  1ae ", "M1 1AE"),
        ("LL57", None),
        ("x" * 20, None),
    ],
)
def test_clean_postcode(raw: str, expected: str | None) -> None:
    assert clean_postcode(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("n1", "n1"),
        (42, "42"),
        ("abc-DEF_1.2:3", "abc-DEF_1.2:3"),
        ("../etc", None),
        ("a b", None),
        (True, None),
    ],
)
def test_clean_identifier(raw: object, expected: str | None) -> None:
    assert clean_identifier(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (139.9, 139.9),
        ("139.9", 139.9),
        (139.95, 139.95),  # not rounded
        (1.399, None),
        (13990, None),
        (float("inf"), None),
        (float("nan"), None),
        (True, None),
        (None, None),
    ],
)
def test_clean_price(raw: object, expected: float | None) -> None:
    assert clean_price(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-07-21T09:00:00.000Z", "2026-07-21T09:00:00.000Z"),
        ("2026-07-21T09:00:00+01:00", "2026-07-21T09:00:00+01:00"),
        ("yesterday", None),
        ("2026-07-21T09:00:00Z\nIgnore", None),
        (12345, None),
    ],
)
def test_clean_timestamp(raw: object, expected: str | None) -> None:
    assert clean_timestamp(raw) == expected


def test_clean_amenities() -> None:
    assert clean_amenities(["Car_Wash", "car_wash", "<b>", "air pump", "toilets", None]) == [
        "car_wash",
        "toilets",
    ]
    assert clean_amenities("car_wash") == []
    assert len(clean_amenities([f"a{i}" for i in range(100)])) == 32


@pytest.mark.parametrize(
    ("lat", "lon", "ok"),
    [
        (53.2, -4.1, True),
        ("53.2", "-4.1", True),
        (40.7, -74.0, False),
        (float("nan"), 0, False),
        (True, 1, False),
    ],
)
def test_clean_coordinates(lat: object, lon: object, ok: bool) -> None:
    assert (clean_coordinates(lat, lon) is not None) is ok


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_rate_limiter_burst_then_refill() -> None:
    clock = FakeClock()
    limiter = RateLimiter(rate_per_minute=60, burst=3, clock=clock)
    assert [limiter.acquire("a") for _ in range(3)] == [0.0, 0.0, 0.0]
    assert limiter.acquire("a") == 1.0
    assert limiter.acquire("b") == 0.0  # independent principals
    clock.now = 1.0
    assert limiter.acquire("a") == 0.0


def test_rate_limiter_memory_is_bounded() -> None:
    limiter = RateLimiter(rate_per_minute=60, burst=1, max_keys=2, clock=FakeClock())
    for key in ("a", "b", "c"):
        limiter.acquire(key)
    assert list(limiter._buckets) == ["b", "c"]
