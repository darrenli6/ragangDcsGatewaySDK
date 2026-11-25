# ragany_dcs_gateway_sdk

工业 DCS 网关 HTTP 采集 SDK（发酵工艺场景）。基于 **httpx**（同步 + 异步、连接池、超时、重试）与 **Pydantic v2**（类型安全的点位模型）。

协议原文见 [docs/gateway_api_spec.md](docs/gateway_api_spec.md)：`POST http://<网关>/soap/BrowseTag`、`POST http://<网关>/soap/ReadTag`。

## 特性

- **同步 / 异步双客户端**，API 完全一致：`DCSGatewayClient` / `AsyncDCSGatewayClient`
- **工业级通信**：连接池复用；连接/读取超时分离；连接失败、超时、429/502/503/504 自动重试（指数退避 + 抖动，支持 `Retry-After`）
- **完整异常体系**：网关离线、超时、鉴权失败、点位不存在、报文非法、量程越限，全部继承 `DCSGatewayError`
- **工艺点位模型**：`PhPoint`（0~14）、`TemperaturePoint`（-50~200℃，覆盖 121℃ SIP）、`LevelPoint`、`DissolvedOxygenPoint`、`PressurePoint`，NaN/Inf 识别为仪表故障
- **批次采集**：`TagMap` 把工艺逻辑名映射到现场点位，一次请求得到 `BatchData`；单个仪表异常不影响整批
- **周期采集**：`poll()` 固定周期不漂移，`on_error` 回调实现断线自动恢复
- **大批量自动分片**：超过 `read_batch_size` 自动拆分请求（异步版并发发送）
- **报文兼容**：UTF-8 / UTF-8 BOM / GBK 响应自动解码；数据类型 1~8 按协议转换并做位宽范围检查
- **离线开发**：内置 `FakeGateway` 模拟网关，无需现场设备即可开发与测试
- 完整类型标注（`py.typed`，mypy strict 通过），测试覆盖率 98%

## 安装

```bash
pip install -e .            # 运行依赖：httpx, pydantic
pip install -e ".[dev]"     # 开发依赖：pytest, pytest-cov, mypy, ruff
```

需要 Python 3.10+。

## 快速开始

```python
from ragany_dcs_gateway_sdk import DCSGatewayClient, PhPoint, TemperaturePoint

with DCSGatewayClient(base_url="http://192.168.1.88") as client:
    # 浏览点位结构（BrowseTag），path 可直接用于 ReadTag
    for tag in client.list_tags():
        print(tag.path, tag.description, tag.data_type, tag.value)

    # 批量读原始值（ReadTag）
    values = client.read_tags(["s.CP201.D122.qq1", "s.CP201.D122.qq2"])
    print(values["s.CP201.D122.qq1"].as_int())

    # 读工艺点位（带量程校验）
    ph = client.read_point("s.CP201.D122.ph", PhPoint)
    print(ph.display())   # "6.85 pH"
```

### 批次快照

```python
from ragany_dcs_gateway_sdk import TagMap, PhPoint, TemperaturePoint, LevelPoint

F101 = TagMap({
    "ph":          ("s.CP201.D122.ph",    PhPoint),
    "temperature": ("s.CP201.D122.temp",  TemperaturePoint),
    "level":       ("s.CP201.D122.level", LevelPoint),
})

batch = client.read_batch(F101, batch_id="B20261125-01", unit_id="F-101")
batch.ph.value, batch.temperature.value
batch.errors          # {"level": "..."} 越限/缺失的点位
batch.to_record()     # 扁平 dict，写时序库 / CSV / MQ
```

### 周期采集（断线自动恢复）

```python
for snapshot in client.poll(F101.tags, interval=5, on_error=lambda e: log.warning(e)):
    save(snapshot)
```

### 异步

```python
import asyncio
from ragany_dcs_gateway_sdk import AsyncDCSGatewayClient

async def main():
    async with AsyncDCSGatewayClient(base_url="http://192.168.1.88") as client:
        b1, b2 = await asyncio.gather(client.read_batch(F101_MAP), client.read_batch(F102_MAP))
        async for snapshot in client.poll(["s.CP201.D122.ph"], interval=5):
            ...

asyncio.run(main())
```

## 配置

| 参数 | 默认值 | 说明 |
|---|---|---|
| `base_url` | `http://192.168.1.88` | 网关地址 |
| `api_prefix` | `/soap` | 接口前缀 |
| `token` / `auth_header` / `auth_scheme` | 无 / `Authorization` / `Bearer` | 可选鉴权（协议未要求，网关前有代理鉴权时使用） |
| `connect_timeout` / `read_timeout` | 3s / 10s | 超时 |
| `max_retries` | 3 | 额外重试次数，0 关闭 |
| `backoff_factor` / `backoff_max` | 0.5s / 10s | 退避：`factor * 2^(n-1)`，±25% 抖动 |
| `retry_on_status` | 429, 502, 503, 504 | 可重试状态码 |
| `poll_interval` | 5s | `poll()` 默认周期 |
| `read_batch_size` | 100 | 单次 ReadTag 最大点位数 |

所有参数都可通过环境变量设置：`RAGANY_DCS_<参数名大写>`，例如：

```bash
export RAGANY_DCS_BASE_URL=http://192.168.1.88
export RAGANY_DCS_MAX_RETRIES=5
```

```python
client = DCSGatewayClient(GatewayConfig.from_env())
```

## 异常

```
DCSGatewayError
├── ConfigError                  配置非法
├── GatewayOfflineError          网关不可达（重试耗尽），.attempts
│   └── GatewayTimeoutError      请求超时
├── GatewayHTTPError             非 2xx，.status_code / .body
│   └── AuthenticationError      401 / 403（不重试）
├── ResponseValidationError      报文非 JSON 或结构不符，.payload
├── TagNotFoundError             点位不存在，.tags
└── PointValidationError         类型转换失败 / 量程越限，.tag / .value
```

`read_tags(..., strict=True)`（默认）在有点位未返回时抛 `TagNotFoundError`；`strict=False` 只记录告警并从结果中省略。

## 协议约定与假设

文档未明确的行为，SDK 处理方式如下，**现场联调时请确认**：

1. **点位不存在**：ReadTag 响应中缺少该点位即视为不存在。若网关改为返回错误码/空值，需调整 `_ClientCore._parse_read`。
2. **全路径拼接**：`驱动.通道.设备[.组].点位`。浏览全部（`node=""`）时根节点名（如 `VarDevice`）不计入路径；浏览子节点时以 `node` 作为前缀。
3. **结构节点名称字段**：使用 `name`（兼容 `n`），点位使用 `n`/`d`/`t`/`u`/`v`/`w`。
4. **单位编码**：协议只给出 `95=无单位`，其它编码保存在 `TagInfo.unit_code`，工艺单位由点位模型声明。

## 项目结构

```
ragany_dcs_gateway_sdk/
├── __init__.py      对外入口
├── client.py        同步/异步客户端：请求、重试、超时、鉴权、解析
├── models.py        TagInfo / BrowseNode / TagValue / PhPoint / TemperaturePoint / BatchData ...
├── exceptions.py    异常体系
├── config.py        GatewayConfig（不可变，支持环境变量）
├── utils.py         时间格式化、点位路径、类型转换、报文校验、TagMap
├── testing.py       FakeGateway 模拟网关
└── py.typed
examples/demo.py     调用示例
tests/               pytest 测试（146 个用例）
docs/                网关协议文档
```

## 示例与测试

```bash
python examples/demo.py --mock                 # 模拟网关
python examples/demo.py --mock --async
python examples/demo.py --base-url http://192.168.1.88   # 现场网关

pytest --cov                                   # 测试 + 覆盖率
mypy ragany_dcs_gateway_sdk                    # 类型检查（strict）
ruff check . && ruff format --check .          # 代码规范
```

在自己的项目中测试时可以复用模拟网关：

```python
from ragany_dcs_gateway_sdk.testing import FakeGateway

gw = FakeGateway()
gw.set_value("s.CP201.D122.ph", "7.2")
gw.fail_next("connect", 503)            # 注入故障
client = DCSGatewayClient(transport=gw.transport())
```
