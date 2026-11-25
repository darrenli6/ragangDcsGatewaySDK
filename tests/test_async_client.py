"""异步客户端测试：与同步客户端行为一致。"""

from __future__ import annotations

import pytest

from ragany_dcs_gateway_sdk import (
    AsyncDCSGatewayClient,
    AuthenticationError,
    GatewayOfflineError,
    GatewayTimeoutError,
    PhPoint,
    TagMap,
    TagNotFoundError,
    TemperaturePoint,
)
from ragany_dcs_gateway_sdk.testing import FakeGateway

pytestmark = pytest.mark.anyio

PH = "s.CP201.D122.ph"
TEMP = "s.CP201.D122.temp"


async def test_browse_and_list(aclient):
    root = await aclient.browse()
    assert root.name == "VarDevice"
    paths = [t.path for t in await aclient.list_tags("s.CP201.D122")]
    assert PH in paths and "s.CP201.D122.Alarm.high_temp" in paths


async def test_read_tags(aclient):
    values = await aclient.read_tags([PH, TEMP])
    assert values[PH].as_float() == 6.85
    assert values[TEMP].as_float() == 37.2


async def test_read_point_and_tag(aclient):
    assert (await aclient.read_tag(PH)).as_float() == 6.85
    assert (await aclient.read_point(PH, PhPoint)).display() == "6.85 pH"


async def test_missing_tag(aclient):
    with pytest.raises(TagNotFoundError):
        await aclient.read_tag("s.x.y.z")
    assert await aclient.read_tags([PH, "s.x.y.z"], strict=False)


async def test_chunked_concurrent(gateway):
    names = [f"s.D.t{i}" for i in range(25)]
    for n in names:
        gateway.set_value(n, "1")
    async with AsyncDCSGatewayClient(read_batch_size=10, transport=gateway.transport()) as c:
        values = await c.read_tags(names)
    assert list(values) == names
    assert sorted(len(b["data"]) for b in gateway.calls_to("ReadTag")) == [5, 10, 10]


async def test_retry_then_success(aclient, gateway, sleeps):
    gateway.fail_next("timeout", 502)
    assert (await aclient.read_tag(PH)).as_float() == 6.85
    assert len(sleeps) == 2


async def test_offline_and_timeout(aclient, gateway):
    gateway.fail_next("connect", "connect", "connect")
    with pytest.raises(GatewayOfflineError):
        await aclient.read_tag(PH)
    gateway.fail_next("timeout", "timeout", "timeout")
    with pytest.raises(GatewayTimeoutError):
        await aclient.read_tag(PH)


async def test_auth_failure():
    gw = FakeGateway(token="right")
    async with AsyncDCSGatewayClient(token="wrong", transport=gw.transport()) as c:
        with pytest.raises(AuthenticationError):
            await c.read_tag(PH)
    assert c.closed


async def test_read_batch(aclient):
    tm = TagMap({"ph": (PH, PhPoint), "temp": (TEMP, TemperaturePoint)})
    batch = await aclient.read_batch(tm, batch_id="B1", unit_id="F-101")
    assert batch.ok and batch.to_record()["temp"] == 37.2


async def test_poll(aclient, gateway, sleeps):
    errors = []
    seen = []
    gateway.fail_next("connect", "connect", "connect")
    async for values in aclient.poll([PH], interval=1.0, max_cycles=3, on_error=errors.append):
        seen.append(values[PH].as_float())
    assert seen == [6.85, 6.85]
    assert len(errors) == 1


async def test_poll_raises(aclient, gateway):
    gateway.fail_next("connect", "connect", "connect")
    with pytest.raises(GatewayOfflineError):
        async for _ in aclient.poll([PH], max_cycles=1):
            pass


async def test_is_online(aclient, gateway):
    assert await aclient.is_online()
    assert await aclient.is_online(probe_tag=PH)
    gateway.fail_next("connect", "connect", "connect")
    assert not await aclient.is_online()
