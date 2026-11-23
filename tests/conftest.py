from __future__ import annotations

import pytest

from fixtures import FakeUpstream
from fuel_prices_mcp.config import Secret, Settings
from fuel_prices_mcp.store import FuelData


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings() -> Settings:
    return Settings(fuel_finder_client_id="cid", fuel_finder_client_secret=Secret("secret"))


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


@pytest.fixture
def data(settings: Settings, upstream: FakeUpstream) -> FuelData:
    return FuelData(settings, transport=upstream.transport())
