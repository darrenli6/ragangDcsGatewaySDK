"""ragany_dcs_gateway_sdk 调用示例：读取发酵罐 pH、温度等工艺参数。

运行方式::

    # 无现场设备，使用内置模拟网关
    python examples/demo.py --mock

    # 连接现场网关
    python examples/demo.py --base-url http://192.168.1.88
    RAGANY_DCS_BASE_URL=http://192.168.1.88 python examples/demo.py

    # 异步示例
    python examples/demo.py --mock --async
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import timedelta, timezone
from typing import Any

from ragany_dcs_gateway_sdk import (
    AsyncDCSGatewayClient,
    DCSGatewayClient,
    DCSGatewayError,
    GatewayConfig,
    GatewayOfflineError,
    LevelPoint,
    PhPoint,
    PointValidationError,
    TagMap,
    TagNotFoundError,
    TemperaturePoint,
)
from ragany_dcs_gateway_sdk.utils import format_timestamp

CST = timezone(timedelta(hours=8))

# 现场点位映射：工艺逻辑名 → (网关点位全路径, 点位模型)。换罐/换现场只需改这里。
F101 = TagMap(
    {
        "ph": ("s.CP201.D122.ph", PhPoint),
        "temperature": ("s.CP201.D122.temp", TemperaturePoint),
        "level": ("s.CP201.D122.level", LevelPoint),
    }
)


def build_client_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if args.mock:
        from ragany_dcs_gateway_sdk.testing import FakeGateway

        kwargs["transport"] = FakeGateway().transport()
    return kwargs


def run_sync(args: argparse.Namespace) -> None:
    config = GatewayConfig.from_env(**({"base_url": args.base_url} if args.base_url else {}))

    with DCSGatewayClient(config, **build_client_kwargs(args)) as client:
        # 1. 健康检查
        if not client.is_online():
            print(f"网关 {config.base_url} 不在线")
            return

        # 2. BrowseTag：浏览点位结构
        print("== 点位清单 ==")
        for tag in client.list_tags():
            dtype = tag.data_type.name if tag.data_type else tag.data_type_code
            rw = "读写" if tag.writable else "只读"
            print(f"  {tag.path:<32} {tag.description:<8} {dtype:<8} {rw}  当前值={tag.value!r}")

        # 3. ReadTag：读取单个工艺点位（带量程校验）
        print("\n== 单点读取 ==")
        ph = client.read_point("s.CP201.D122.ph", PhPoint)
        temp = client.read_point("s.CP201.D122.temp", TemperaturePoint)
        print(f"  pH   = {ph.display()}   @ {format_timestamp(ph.read_at, '%H:%M:%S', tz=CST)}")
        print(f"  温度 = {temp.display()}")

        # 4. ReadTag：批量读取原始值（与协议文档示例一致）
        print("\n== 批量读取 ==")
        values = client.read_tags(["s.CP201.D122.qq1", "s.CP201.D122.qq2"])
        print(f"  搅拌转速 qq1 = {values['s.CP201.D122.qq1'].as_int()}")
        print(f"  补料泵   qq2 = {'运行' if values['s.CP201.D122.qq2'].as_bool() else '停止'}")

        # 5. 批次快照：一次请求读取一个罐的全部关键参数
        print("\n== 批次快照 ==")
        batch = client.read_batch(F101, batch_id="B20261125-01", unit_id="F-101")
        print(f"  {batch.to_record()}")
        for key, err in batch.errors.items():
            print(f"  [异常点位] {key}: {err}")

        # 6. 异常处理
        print("\n== 异常处理 ==")
        try:
            client.read_tag("s.CP201.D122.not_exist")
        except TagNotFoundError as exc:
            print(f"  点位不存在 -> {exc}")

        class SeedTankTemp(TemperaturePoint):
            """种子罐温度：现场量程更窄，继承后覆盖上限即可。"""

            max_value = 35.0

        try:
            client.read_point("s.CP201.D122.temp", SeedTankTemp)  # 37.2℃ 超出 35℃ 上限
        except PointValidationError as exc:
            print(f"  量程校验失败 -> {exc}")

        # 7. 周期采集（断线自动恢复）
        print(f"\n== 周期采集 {args.cycles} 次，间隔 {args.interval}s ==")

        def on_error(exc: DCSGatewayError) -> None:
            level = "网关离线" if isinstance(exc, GatewayOfflineError) else "采集异常"
            print(f"  [{level}] {exc}，下周期重试")

        for snapshot in client.poll(F101.tags, interval=args.interval, max_cycles=args.cycles, on_error=on_error):
            row = {k: snapshot[spec.tag].as_float() for k, spec in F101.items() if spec.tag in snapshot}
            print(f"  {format_timestamp(next(iter(snapshot.values())).read_at, '%H:%M:%S', tz=CST)}  {row}")


async def run_async(args: argparse.Namespace) -> None:
    config = GatewayConfig.from_env(**({"base_url": args.base_url} if args.base_url else {}))
    async with AsyncDCSGatewayClient(config, **build_client_kwargs(args)) as client:
        # 并发采集多个罐（这里用同一个映射演示）
        batches = await asyncio.gather(
            client.read_batch(F101, unit_id="F-101"),
            client.read_batch(F101, unit_id="F-102"),
        )
        for b in batches:
            print(b.to_record())

        async for snapshot in client.poll(["s.CP201.D122.ph"], interval=args.interval, max_cycles=args.cycles):
            print("pH =", snapshot["s.CP201.D122.ph"].as_float())


def main() -> None:
    parser = argparse.ArgumentParser(description="DCS 网关采集示例")
    parser.add_argument("--base-url", help="网关地址，默认读取 RAGANY_DCS_BASE_URL 或 http://192.168.1.88")
    parser.add_argument("--mock", action="store_true", help="使用内置模拟网关")
    parser.add_argument("--async", dest="use_async", action="store_true", help="运行异步示例")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    try:
        if args.use_async:
            asyncio.run(run_async(args))
        else:
            run_sync(args)
    except DCSGatewayError as exc:
        print(f"采集失败: {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
