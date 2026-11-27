"""核心 HTTP 客户端：请求、重试、超时、鉴权、报文解析。

版权所有 © 2026 Ragany DCS Gateway SDK 权利人。保留一切权利。

提供同步 :class:`DCSGatewayClient` 与异步 :class:`AsyncDCSGatewayClient`，
两者共享 :class:`_ClientCore` 中的请求构造与响应解析逻辑，行为完全一致。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from types import TracebackType
from typing import (
    Any,
    TypeVar,
)

import httpx
from pydantic import ValidationError

from . import __version__
from .config import GatewayConfig
from .exceptions import (
    AuthenticationError,
    ConfigError,
    DCSGatewayError,
    GatewayHTTPError,
    GatewayOfflineError,
    GatewayTimeoutError,
    ResponseValidationError,
    TagNotFoundError,
)
from .models import BatchData, BrowseNode, ProcessPoint, TagInfo, TagValue
from .utils import (
    TagMap,
    backoff_delay,
    chunked,
    dedupe,
    parse_json_body,
    parse_retry_after,
    utc_now,
    validate_browse_payload,
    validate_read_payload,
    validate_tag_name,
)

logger = logging.getLogger("ragany_dcs_gateway_sdk")

BROWSE_TAG = "BrowseTag"
READ_TAG = "ReadTag"

P = TypeVar("P", bound=ProcessPoint)
ErrorHandler = Callable[[DCSGatewayError], Any]
TagNames = str | Iterable[str]


class _RetryableStatus(Exception):
    """内部信号：可重试的 HTTP 状态码。"""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response


class _ClientCore:
    """同步/异步客户端共享的无 I/O 逻辑。"""

    def __init__(self, config: GatewayConfig | None, overrides: dict[str, Any]) -> None:
        if config is not None and overrides:
            raise ConfigError("不能同时传入 config 与关键字配置参数")
        self.config: GatewayConfig = config if config is not None else GatewayConfig.create(**overrides)

    # ---- httpx 参数 ----
    def _client_kwargs(self) -> dict[str, Any]:
        cfg = self.config
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"{cfg.user_agent}/{__version__}",
            **cfg.auth_headers(),
        }
        return {
            "headers": headers,
            "timeout": httpx.Timeout(cfg.read_timeout, connect=cfg.connect_timeout),
            "limits": httpx.Limits(
                max_connections=cfg.max_connections,
                max_keepalive_connections=cfg.max_keepalive_connections,
            ),
            "verify": cfg.verify_ssl,
        }

    # ---- 请求结果判定 ----
    def _check_response(self, function: str, response: httpx.Response) -> Any:
        status = response.status_code
        if status in self.config.retry_on_status:
            raise _RetryableStatus(response)
        if status in (401, 403):
            raise AuthenticationError(
                f"鉴权失败 HTTP {status}，请检查 token",
                status_code=status,
                endpoint=function,
                body=response.text[:500],
            )
        if not 200 <= status < 300:
            raise GatewayHTTPError(
                f"网关返回 HTTP {status}",
                status_code=status,
                endpoint=function,
                body=response.text[:500],
            )
        return parse_json_body(response.content, endpoint=function)

    def _retry_delay(self, retry_number: int, exc: BaseException) -> float:
        retry_after = None
        if isinstance(exc, _RetryableStatus):
            retry_after = parse_retry_after(exc.response.headers.get("Retry-After"))
        cfg = self.config
        return backoff_delay(retry_number, cfg.backoff_factor, cfg.backoff_max, retry_after=retry_after)

    def _final_error(self, function: str, exc: BaseException, attempts: int) -> DCSGatewayError:
        if isinstance(exc, _RetryableStatus):
            resp = exc.response
            return GatewayHTTPError(
                f"网关返回 HTTP {resp.status_code}，已重试 {attempts - 1} 次",
                status_code=resp.status_code,
                endpoint=function,
                body=resp.text[:500],
            )
        if isinstance(exc, httpx.TimeoutException):
            return GatewayTimeoutError(
                f"请求超时（{type(exc).__name__}），共尝试 {attempts} 次",
                endpoint=function,
                attempts=attempts,
            )
        return GatewayOfflineError(
            f"网关不可达（{type(exc).__name__}: {exc}），共尝试 {attempts} 次",
            endpoint=function,
            attempts=attempts,
        )

    def _log_retry(self, function: str, attempt: int, exc: BaseException, delay: float) -> None:
        reason = f"HTTP {exc.response.status_code}" if isinstance(exc, _RetryableStatus) else type(exc).__name__
        logger.warning("%s 第 %d 次请求失败（%s），%.2fs 后重试", function, attempt, reason, delay)

    # ---- 请求体与响应解析 ----
    @staticmethod
    def _browse_body(node: str) -> dict[str, Any]:
        node = node.strip() if node else ""
        if node:
            validate_tag_name(node)
        return {"node": node}

    @staticmethod
    def _parse_browse(payload: Any) -> BrowseNode:
        data = validate_browse_payload(payload, endpoint=BROWSE_TAG)
        try:
            return BrowseNode.model_validate(data)
        except ValidationError as exc:
            raise ResponseValidationError(
                f"BrowseTag 响应结构非法: {exc}", endpoint=BROWSE_TAG, payload=payload
            ) from exc

    @staticmethod
    def _normalize_names(names: TagNames) -> list[str]:
        if isinstance(names, str):
            names = [names]
        result = dedupe(validate_tag_name(n) for n in names)
        if not result:
            raise ValueError("至少需要一个点位名称")
        return result

    @staticmethod
    def _read_body(names: list[str]) -> dict[str, Any]:
        return {"data": [{"name": n} for n in names]}

    @staticmethod
    def _parse_read(payload: Any, requested: list[str]) -> dict[str, TagValue]:
        items = validate_read_payload(payload, endpoint=READ_TAG)
        read_at = utc_now()
        by_name = {item["name"]: item for item in items}
        out: dict[str, TagValue] = {}
        for name in requested:
            item = by_name.get(name)
            if item is not None:
                out[name] = TagValue(name=name, raw=item.get("value"), read_at=read_at)
        return out

    @staticmethod
    def _finish_read(result: dict[str, TagValue], requested: list[str], strict: bool) -> dict[str, TagValue]:
        missing = [n for n in requested if n not in result]
        if missing:
            if strict:
                raise TagNotFoundError(missing, endpoint=READ_TAG)
            logger.warning("以下点位未返回（可能不存在）: %s", ", ".join(missing))
        return {n: result[n] for n in requested if n in result}

    @staticmethod
    def _build_batch(
        tag_map: TagMap,
        values: dict[str, TagValue],
        batch_id: str | None,
        unit_id: str | None,
    ) -> BatchData:
        points: dict[str, ProcessPoint] = {}
        errors: dict[str, str] = {}
        for key, spec in tag_map.items():
            tv = values.get(spec.tag)
            if tv is None:
                errors[key] = f"点位不存在: {spec.tag}"
                continue
            try:
                points[key] = spec.point_type.from_tag_value(tv)
            except DCSGatewayError as exc:
                errors[key] = str(exc)
        if errors:
            logger.warning("批次采集存在异常点位: %s", errors)
        return BatchData(batch_id=batch_id, unit_id=unit_id, points=points, errors=errors)


# =========================================================================== #
# 同步客户端
# =========================================================================== #
class DCSGatewayClient(_ClientCore):
    """同步网关客户端（线程安全，内部复用连接池）。

    示例::

        with DCSGatewayClient(base_url="http://192.168.1.88") as client:
            ph = client.read_point("s.CP201.D122.ph", PhPoint)
            print(ph.display())

    :param config: 完整配置；也可以直接用关键字参数（``base_url=...``），二者不能同时使用。
    :param transport: 自定义 httpx 传输层（测试 / 代理 / 本地 mock）。
    :param sleep: 自定义等待函数，主要用于测试。
    """

    def __init__(
        self,
        config: GatewayConfig | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        **overrides: Any,
    ) -> None:
        super().__init__(config, overrides)
        self._sleep = sleep
        self._http = httpx.Client(transport=transport, **self._client_kwargs())

    # ---- 生命周期 ----
    def close(self) -> None:
        self._http.close()

    @property
    def closed(self) -> bool:
        return self._http.is_closed

    def __enter__(self) -> DCSGatewayClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"DCSGatewayClient(base_url={self.config.base_url!r})"

    # ---- 底层调用 ----
    def call(self, function: str, body: dict[str, Any]) -> Any:
        """调用任意网关函数 ``POST {base_url}/soap/{function}``，返回解析后的 JSON。

        对连接错误、超时、``retry_on_status`` 中的状态码自动重试（指数退避）。
        """
        url = self.config.endpoint_url(function)
        attempts = self.config.max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                logger.debug("POST %s body=%s (attempt %d)", url, body, attempt)
                response = self._http.post(url, json=body)
                return self._check_response(function, response)
            except (httpx.TransportError, _RetryableStatus) as exc:
                if attempt >= attempts:
                    raise self._final_error(function, exc, attempt) from (
                        None if isinstance(exc, _RetryableStatus) else exc
                    )
                delay = self._retry_delay(attempt, exc)
                self._log_retry(function, attempt, exc, delay)
                self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    # ---- BrowseTag ----
    def browse(self, node: str = "") -> BrowseNode:
        """获取点位结构树。``node`` 为空获取全部，否则如 ``"Driver.Channel"``。"""
        return self._parse_browse(self.call(BROWSE_TAG, self._browse_body(node)))

    def list_tags(self, node: str = "") -> list[TagInfo]:
        """扁平化列出 ``node`` 下所有点位（``path`` 为可直接用于 ReadTag 的全路径）。"""
        node = node.strip() if node else ""
        return list(self.browse(node).iter_tags(prefix=node))

    # ---- ReadTag ----
    def read_tags(self, names: TagNames, *, strict: bool = True) -> dict[str, TagValue]:
        """批量读取点位值，返回 ``{点位名: TagValue}``（保持请求顺序）。

        超过 ``read_batch_size`` 会自动分多次请求。
        :param strict: ``True`` 时任一点位未返回即抛出 :class:`TagNotFoundError`；
            ``False`` 时仅记录告警，结果中不包含缺失点位。
        """
        requested = self._normalize_names(names)
        result: dict[str, TagValue] = {}
        for chunk in chunked(requested, self.config.read_batch_size):
            result.update(self._parse_read(self.call(READ_TAG, self._read_body(chunk)), chunk))
        return self._finish_read(result, requested, strict)

    def read_tag(self, name: str) -> TagValue:
        """读取单个点位。点位不存在抛出 :class:`TagNotFoundError`。"""
        name = validate_tag_name(name)
        return self.read_tags([name])[name]

    def read_point(self, name: str, point_type: type[P]) -> P:
        """读取单个点位并转换为工艺点位模型（含量程校验）。"""
        return point_type.from_tag_value(self.read_tag(name))

    def read_batch(
        self,
        tag_map: TagMap,
        *,
        batch_id: str | None = None,
        unit_id: str | None = None,
    ) -> BatchData:
        """按 :class:`TagMap` 一次请求读取一组工艺点位，生成 :class:`BatchData`。

        单个点位缺失或越限记录到 ``BatchData.errors``，不影响其它点位；
        网关离线等通信错误仍会抛出。
        """
        values = self.read_tags(tag_map.tags, strict=False)
        return self._build_batch(tag_map, values, batch_id, unit_id)

    # ---- 轮询采集 ----
    def poll(
        self,
        names: TagNames,
        *,
        interval: float | None = None,
        max_cycles: int | None = None,
        strict: bool = False,
        on_error: ErrorHandler | None = None,
    ) -> Iterator[dict[str, TagValue]]:
        """按固定周期循环采集，每周期 yield 一次结果。

        周期按起始时刻对齐（扣除请求耗时），不会因网络延迟逐渐漂移。
        :param on_error: 提供时，通信异常交给回调处理并继续下一周期（断线自动恢复）；
            不提供则异常直接抛出。
        """
        requested = self._normalize_names(names)
        period = interval if interval is not None else self.config.poll_interval
        cycle = 0
        while max_cycles is None or cycle < max_cycles:
            cycle += 1
            started = time.monotonic()
            try:
                yield self.read_tags(requested, strict=strict)
            except DCSGatewayError as exc:
                if on_error is None:
                    raise
                on_error(exc)
            if max_cycles is not None and cycle >= max_cycles:
                break
            self._sleep(max(0.0, period - (time.monotonic() - started)))

    # ---- 健康检查 ----
    def is_online(self, probe_tag: str | None = None) -> bool:
        """网关是否在线。提供 ``probe_tag`` 时读取该点位（轻量），否则调用 BrowseTag。"""
        try:
            if probe_tag:
                self.read_tags([probe_tag], strict=False)
            else:
                self.call(BROWSE_TAG, self._browse_body(""))
            return True
        except (GatewayOfflineError, GatewayHTTPError):
            return False


# =========================================================================== #
# 异步客户端
# =========================================================================== #
class AsyncDCSGatewayClient(_ClientCore):
    """异步网关客户端，API 与 :class:`DCSGatewayClient` 一致（方法均为 ``async``）。

    示例::

        async with AsyncDCSGatewayClient(base_url="http://192.168.1.88") as client:
            values = await client.read_tags(["s.CP201.D122.ph", "s.CP201.D122.temp"])
    """

    def __init__(
        self,
        config: GatewayConfig | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        **overrides: Any,
    ) -> None:
        super().__init__(config, overrides)
        self._sleep = sleep
        self._http = httpx.AsyncClient(transport=transport, **self._client_kwargs())

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def closed(self) -> bool:
        return self._http.is_closed

    async def __aenter__(self) -> AsyncDCSGatewayClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return f"AsyncDCSGatewayClient(base_url={self.config.base_url!r})"

    async def call(self, function: str, body: dict[str, Any]) -> Any:
        url = self.config.endpoint_url(function)
        attempts = self.config.max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                logger.debug("POST %s body=%s (attempt %d)", url, body, attempt)
                response = await self._http.post(url, json=body)
                return self._check_response(function, response)
            except (httpx.TransportError, _RetryableStatus) as exc:
                if attempt >= attempts:
                    raise self._final_error(function, exc, attempt) from (
                        None if isinstance(exc, _RetryableStatus) else exc
                    )
                delay = self._retry_delay(attempt, exc)
                self._log_retry(function, attempt, exc, delay)
                await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    async def browse(self, node: str = "") -> BrowseNode:
        return self._parse_browse(await self.call(BROWSE_TAG, self._browse_body(node)))

    async def list_tags(self, node: str = "") -> list[TagInfo]:
        node = node.strip() if node else ""
        return list((await self.browse(node)).iter_tags(prefix=node))

    async def read_tags(self, names: TagNames, *, strict: bool = True) -> dict[str, TagValue]:
        requested = self._normalize_names(names)
        chunks = list(chunked(requested, self.config.read_batch_size))
        payloads = await asyncio.gather(*(self.call(READ_TAG, self._read_body(c)) for c in chunks))
        result: dict[str, TagValue] = {}
        for chunk, payload in zip(chunks, payloads, strict=True):
            result.update(self._parse_read(payload, chunk))
        return self._finish_read(result, requested, strict)

    async def read_tag(self, name: str) -> TagValue:
        name = validate_tag_name(name)
        return (await self.read_tags([name]))[name]

    async def read_point(self, name: str, point_type: type[P]) -> P:
        return point_type.from_tag_value(await self.read_tag(name))

    async def read_batch(
        self,
        tag_map: TagMap,
        *,
        batch_id: str | None = None,
        unit_id: str | None = None,
    ) -> BatchData:
        values = await self.read_tags(tag_map.tags, strict=False)
        return self._build_batch(tag_map, values, batch_id, unit_id)

    async def poll(
        self,
        names: TagNames,
        *,
        interval: float | None = None,
        max_cycles: int | None = None,
        strict: bool = False,
        on_error: ErrorHandler | None = None,
    ) -> AsyncIterator[dict[str, TagValue]]:
        requested = self._normalize_names(names)
        period = interval if interval is not None else self.config.poll_interval
        cycle = 0
        while max_cycles is None or cycle < max_cycles:
            cycle += 1
            started = time.monotonic()
            try:
                yield await self.read_tags(requested, strict=strict)
            except DCSGatewayError as exc:
                if on_error is None:
                    raise
                on_error(exc)
            if max_cycles is not None and cycle >= max_cycles:
                break
            await self._sleep(max(0.0, period - (time.monotonic() - started)))

    async def is_online(self, probe_tag: str | None = None) -> bool:
        try:
            if probe_tag:
                await self.read_tags([probe_tag], strict=False)
            else:
                await self.call(BROWSE_TAG, self._browse_body(""))
            return True
        except (GatewayOfflineError, GatewayHTTPError):
            return False


__all__ = ["AsyncDCSGatewayClient", "BROWSE_TAG", "DCSGatewayClient", "READ_TAG"]
