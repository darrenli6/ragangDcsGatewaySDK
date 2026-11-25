from __future__ import annotations

from collections.abc import Iterator

import pytest

from ragany_dcs_gateway_sdk import AsyncDCSGatewayClient, DCSGatewayClient, GatewayConfig
from ragany_dcs_gateway_sdk.testing import FakeGateway

BASE_URL = "http://192.168.1.88"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def config() -> GatewayConfig:
    return GatewayConfig(base_url=BASE_URL, max_retries=2, backoff_factor=0.1, backoff_max=1.0)


@pytest.fixture
def client(gateway: FakeGateway, config: GatewayConfig, sleeps: list[float]) -> Iterator[DCSGatewayClient]:
    with DCSGatewayClient(config, transport=gateway.transport(), sleep=sleeps.append) as c:
        yield c


@pytest.fixture
async def aclient(gateway: FakeGateway, config: GatewayConfig, sleeps: list[float]):
    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with AsyncDCSGatewayClient(config, transport=gateway.transport(), sleep=fake_sleep) as c:
        yield c
