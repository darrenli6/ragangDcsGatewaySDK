"""同步客户端测试：协议报文、批量读取、重试、异常映射、轮询、批次采集。"""

from __future__ import annotations

import httpx
import pytest

from ragany_dcs_gateway_sdk import (
    AuthenticationError,
    ConfigError,
    DCSGatewayClient,
    DCSGatewayError,
    GatewayConfig,
    GatewayHTTPError,
    GatewayOfflineError,
    GatewayTimeoutError,
    LevelPoint,
    PhPoint,
    PointValidationError,
    ResponseValidationError,
    TagMap,
    TagNotFoundError,
    TemperaturePoint,
    __version__,
)
from ragany_dcs_gateway_sdk.testing import FakeGateway

PH = "s.CP201.D122.ph"
TEMP = "s.CP201.D122.temp"
QQ1 = "s.CP201.D122.qq1"
QQ2 = "s.CP201.D122.qq2"


# --------------------------------------------------------------------------- #
# 构造 / 生命周期
# --------------------------------------------------------------------------- #
class TestLifecycle:
    def test_kwargs_config(self, gateway):
        with DCSGatewayClient(base_url="http://10.0.0.5", transport=gateway.transport()) as c:
            assert c.config.base_url == "http://10.0.0.5"
            assert "10.0.0.5" in repr(c)
        assert c.closed

    def test_config_and_kwargs_conflict(self):
        with pytest.raises(ConfigError):
            DCSGatewayClient(GatewayConfig(), base_url="http://x")

    def test_invalid_kwargs(self):
        with pytest.raises(ConfigError):
            DCSGatewayClient(base_url="not-a-url")


# --------------------------------------------------------------------------- #
# 报文格式（与协议文档一致）
# --------------------------------------------------------------------------- #
class TestWireFormat:
    def test_read_tag_request_matches_doc(self, client, gateway):
        client.read_tags([QQ1, QQ2])
        fn, body, headers = gateway.calls[-1]
        assert fn == "ReadTag"
        assert body == {"data": [{"name": QQ1}, {"name": QQ2}]}
        assert headers["content-type"] == "application/json"
        assert headers["user-agent"].endswith(__version__)

    def test_browse_request_matches_doc(self, client, gateway):
        client.browse()
        client.browse(" s.CP201 ")
        assert gateway.calls_to("BrowseTag") == [{"node": ""}, {"node": "s.CP201"}]

    def test_url(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={"data": [{"name": QQ1, "value": "9"}]})

        with DCSGatewayClient(transport=httpx.MockTransport(handler)) as c:
            c.read_tag(QQ1)
        assert seen == ["http://192.168.1.88/soap/ReadTag"]

    def test_doc_example_response(self):
        doc_response = {"data": [{"name": QQ1, "value": "9"}, {"name": QQ2, "value": "0"}]}
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json=doc_response))
        with DCSGatewayClient(transport=transport) as c:
            values = c.read_tags([QQ1, QQ2])
        assert values[QQ1].as_int() == 9
        assert values[QQ2].as_bool() is False

    def test_auth_header_sent(self):
        gw = FakeGateway(token="abc")
        with DCSGatewayClient(token="abc", transport=gw.transport()) as c:
            assert c.read_tag(PH).as_float() == 6.85
        assert gw.calls[0][2]["authorization"] == "Bearer abc"


# --------------------------------------------------------------------------- #
# BrowseTag
# --------------------------------------------------------------------------- #
class TestBrowse:
    def test_browse_all(self, client):
        root = client.browse()
        assert root.name == "VarDevice"
        assert [d.name for d in root.drivers] == ["s"]

    def test_list_tags_all(self, client):
        tags = {t.path: t for t in client.list_tags()}
        assert tags[PH].value == 6.85
        assert tags[QQ1].writable
        assert "s.CP201.D122.Alarm.high_temp" in tags

    def test_list_tags_subnode_paths_are_readable(self, client):
        tags = client.list_tags("s.CP201")
        assert tags[0].path == PH
        # 浏览得到的路径可直接用于 ReadTag
        values = client.read_tags([t.path for t in tags])
        assert len(values) == len(tags)

    def test_browse_invalid_node(self, client):
        with pytest.raises(ValueError):
            client.browse("s..x")

    def test_browse_unknown_node_http_error(self, client):
        with pytest.raises(GatewayHTTPError) as ei:
            client.browse("s.NOPE")
        assert ei.value.status_code == 404

    def test_browse_malformed(self, client, gateway):
        gateway.respond_with("BrowseTag", httpx.Response(200, json={"tags": [{"d": "no name"}]}))
        with pytest.raises(ResponseValidationError):
            client.browse()


# --------------------------------------------------------------------------- #
# ReadTag
# --------------------------------------------------------------------------- #
class TestRead:
    def test_read_single(self, client):
        tv = client.read_tag(PH)
        assert tv.name == PH and tv.as_float() == 6.85

    def test_read_preserves_order_and_dedupes(self, client, gateway):
        values = client.read_tags([TEMP, PH, TEMP])
        assert list(values) == [TEMP, PH]
        assert gateway.calls_to("ReadTag")[0] == {"data": [{"name": TEMP}, {"name": PH}]}

    def test_read_accepts_single_string(self, client):
        assert list(client.read_tags(PH)) == [PH]

    def test_read_empty_rejected(self, client):
        with pytest.raises(ValueError):
            client.read_tags([])

    def test_missing_tag_strict(self, client):
        with pytest.raises(TagNotFoundError) as ei:
            client.read_tags([PH, "s.CP201.D122.nope"])
        assert ei.value.tags == ("s.CP201.D122.nope",)
        assert isinstance(ei.value, DCSGatewayError)

    def test_missing_tag_lenient(self, client, caplog):
        values = client.read_tags([PH, "s.CP201.D122.nope"], strict=False)
        assert list(values) == [PH]
        assert "s.CP201.D122.nope" in caplog.text

    def test_read_tag_missing(self, client):
        with pytest.raises(TagNotFoundError):
            client.read_tag("s.x.y.z")

    def test_live_value_change(self, client, gateway):
        gateway.set_value(PH, "7.10")
        assert client.read_tag(PH).as_float() == 7.10

    def test_null_value_returned(self, client, gateway):
        gateway.set_value(PH, None)
        assert client.read_tag(PH).is_null

    def test_large_batch_is_chunked(self, gateway, sleeps):
        names = [f"s.CP201.D122.t{i}" for i in range(250)]
        for i, n in enumerate(names):
            gateway.set_value(n, str(i))
        with DCSGatewayClient(read_batch_size=100, transport=gateway.transport()) as c:
            values = c.read_tags(names)
        assert [len(b["data"]) for b in gateway.calls_to("ReadTag")] == [100, 100, 50]
        assert values[names[-1]].as_int() == 249
        assert list(values) == names

    def test_read_point(self, client):
        ph = client.read_point(PH, PhPoint)
        assert isinstance(ph, PhPoint) and ph.value == 6.85

    def test_read_point_out_of_range(self, client, gateway):
        gateway.set_value(PH, "15.2")
        with pytest.raises(PointValidationError):
            client.read_point(PH, PhPoint)

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(200, text="<html>500</html>"),
            httpx.Response(200, text=""),
            httpx.Response(200, json=[1, 2]),
            httpx.Response(200, json={"data": "x"}),
        ],
    )
    def test_malformed_read_response(self, client, gateway, response):
        gateway.respond_with("ReadTag", response)
        with pytest.raises(ResponseValidationError):
            client.read_tag(PH)

    def test_gbk_encoded_response(self, client, gateway):
        body = '{"data": [{"name": "s.CP201.D122.ph", "value": "正常"}]}'.encode("gbk")
        gateway.respond_with("ReadTag", httpx.Response(200, content=body))
        assert client.read_tag(PH).as_str() == "正常"


# --------------------------------------------------------------------------- #
# 重试与异常映射
# --------------------------------------------------------------------------- #
class TestRetry:
    def test_recovers_after_transient_failures(self, client, gateway, sleeps):
        gateway.fail_next("connect", 503)
        assert client.read_tag(PH).as_float() == 6.85
        assert len(gateway.calls) == 3
        assert len(sleeps) == 2
        assert 0.075 <= sleeps[0] <= 0.125  # backoff 0.1 ±25%
        assert 0.15 <= sleeps[1] <= 0.25

    def test_offline_after_retries_exhausted(self, client, gateway):
        gateway.fail_next("connect", "connect", "connect")
        with pytest.raises(GatewayOfflineError) as ei:
            client.read_tag(PH)
        assert ei.value.attempts == 3
        assert ei.value.endpoint == "ReadTag"
        assert not isinstance(ei.value, GatewayTimeoutError)
        assert isinstance(ei.value.__cause__, httpx.ConnectError)

    def test_timeout_error(self, client, gateway):
        gateway.fail_next("timeout", "timeout", "timeout")
        with pytest.raises(GatewayTimeoutError):
            client.read_tag(PH)

    def test_timeout_is_offline_subclass(self, client, gateway):
        gateway.fail_next("timeout", "timeout", "timeout")
        with pytest.raises(GatewayOfflineError):
            client.read_tag(PH)

    def test_retryable_status_exhausted(self, client, gateway):
        gateway.fail_next(503, 503, 503)
        with pytest.raises(GatewayHTTPError) as ei:
            client.read_tag(PH)
        assert ei.value.status_code == 503

    def test_retry_after_header_respected(self, sleeps):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "0.8"})
            return httpx.Response(200, json={"data": [{"name": PH, "value": "7"}]})

        with DCSGatewayClient(
            max_retries=1,
            backoff_factor=0.01,
            backoff_max=5,
            transport=httpx.MockTransport(handler),
            sleep=sleeps.append,
        ) as c:
            assert c.read_tag(PH).as_int() == 7
        assert sleeps == [0.8]

    def test_no_retry_on_client_error(self, client, gateway):
        gateway.fail_next(400)
        with pytest.raises(GatewayHTTPError) as ei:
            client.read_tag(PH)
        assert ei.value.status_code == 400
        assert len(gateway.calls) == 1

    def test_no_retry_when_disabled(self, gateway, sleeps):
        gateway.fail_next("connect")
        with (
            DCSGatewayClient(max_retries=0, transport=gateway.transport(), sleep=sleeps.append) as c,
            pytest.raises(GatewayOfflineError),
        ):
            c.read_tag(PH)
        assert sleeps == [] and len(gateway.calls) == 1

    def test_authentication_error(self):
        gw = FakeGateway(token="right")
        with (
            DCSGatewayClient(token="wrong", transport=gw.transport()) as c,
            pytest.raises(AuthenticationError) as ei,
        ):
            c.read_tag(PH)
        assert ei.value.status_code == 401
        assert len(gw.calls) == 1  # 鉴权失败不重试

    def test_error_str_contains_endpoint(self, client, gateway):
        gateway.fail_next(500)
        with pytest.raises(GatewayHTTPError, match=r"^\[ReadTag\] 网关返回 HTTP 500"):
            client.read_tag(PH)


# --------------------------------------------------------------------------- #
# 批次采集
# --------------------------------------------------------------------------- #
TAG_MAP = TagMap(
    {
        "ph": (PH, PhPoint),
        "temperature": (TEMP, TemperaturePoint),
        "level": ("s.CP201.D122.level", LevelPoint),
    }
)


class TestBatch:
    def test_read_batch_single_request(self, client, gateway):
        batch = client.read_batch(TAG_MAP, batch_id="B001", unit_id="F-101")
        assert batch.ok
        assert batch.ph is not None and batch.ph.value == 6.85
        assert batch.temperature is not None and batch.temperature.value == 37.2
        assert batch.to_record()["level"] == 68.5
        assert len(gateway.calls_to("ReadTag")) == 1

    def test_partial_failure_isolated(self, client, gateway):
        gateway.set_value(PH, "99")
        del gateway.values["s.CP201.D122.level"]
        batch = client.read_batch(TAG_MAP)
        assert not batch.ok
        assert set(batch.errors) == {"ph", "level"}
        assert "点位不存在" in batch.errors["level"]
        assert batch.temperature is not None

    def test_offline_still_raises(self, client, gateway):
        gateway.fail_next("connect", "connect", "connect")
        with pytest.raises(GatewayOfflineError):
            client.read_batch(TAG_MAP)


# --------------------------------------------------------------------------- #
# 轮询与健康检查
# --------------------------------------------------------------------------- #
class TestPoll:
    def test_poll_cycles(self, client, gateway, sleeps):
        results = list(client.poll([PH], interval=2.0, max_cycles=3))
        assert len(results) == 3
        assert len(sleeps) == 2  # 最后一个周期后不再等待
        assert all(0 < s <= 2.0 for s in sleeps)

    def test_poll_sees_changing_values(self, client, gateway):
        it = client.poll([PH], interval=0.01)
        assert next(it)[PH].as_float() == 6.85
        gateway.set_value(PH, "6.5")
        assert next(it)[PH].as_float() == 6.5

    def test_poll_recovers_with_on_error(self, client, gateway):
        errors = []
        gateway.fail_next("connect", "connect", "connect")  # 第一个周期重试耗尽
        results = list(client.poll([PH], interval=0.01, max_cycles=2, on_error=errors.append))
        assert len(errors) == 1 and isinstance(errors[0], GatewayOfflineError)
        assert len(results) == 1 and results[0][PH].as_float() == 6.85

    def test_poll_raises_without_on_error(self, client, gateway):
        gateway.fail_next("connect", "connect", "connect")
        with pytest.raises(GatewayOfflineError):
            next(client.poll([PH]))

    def test_is_online(self, client, gateway):
        assert client.is_online()
        assert client.is_online(probe_tag=PH)
        gateway.fail_next("connect", "connect", "connect")
        assert not client.is_online()
        gateway.fail_next(503, 503, 503)
        assert not client.is_online(probe_tag=PH)
