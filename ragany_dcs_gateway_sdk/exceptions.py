"""SDK 异常体系。

所有异常都继承自 :class:`DCSGatewayError`，调用方可以只捕获这一个基类，
也可以按需精细处理::

    DCSGatewayError
    ├── ConfigError                  配置非法
    ├── GatewayOfflineError          网关不可达（连接失败/超时，重试耗尽）
    │   └── GatewayTimeoutError      请求超时
    ├── GatewayHTTPError             网关返回非 2xx 状态码
    │   └── AuthenticationError      401 / 403 鉴权失败
    ├── ResponseValidationError      报文格式/结构校验失败
    ├── TagNotFoundError             点位不存在
    └── PointValidationError         点位值转换或工艺范围校验失败
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any


class DCSGatewayError(Exception):
    """SDK 所有异常的基类。"""

    def __init__(self, message: str, *, endpoint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.endpoint = endpoint

    def __str__(self) -> str:
        if self.endpoint:
            return f"[{self.endpoint}] {self.message}"
        return self.message


class ConfigError(DCSGatewayError):
    """SDK 配置非法（如网关地址为空、超时为负数）。"""


class GatewayOfflineError(DCSGatewayError):
    """网关离线：连接被拒绝、网络不可达、DNS 失败等，且重试次数已耗尽。"""

    def __init__(
        self,
        message: str,
        *,
        endpoint: str | None = None,
        attempts: int = 1,
    ) -> None:
        super().__init__(message, endpoint=endpoint)
        self.attempts = attempts


class GatewayTimeoutError(GatewayOfflineError):
    """网关请求超时（连接超时或读取超时），且重试次数已耗尽。"""


class GatewayHTTPError(DCSGatewayError):
    """网关返回了非 2xx 的 HTTP 状态码。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        endpoint: str | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message, endpoint=endpoint)
        self.status_code = status_code
        self.body = body


class AuthenticationError(GatewayHTTPError):
    """鉴权失败（HTTP 401 / 403），通常是 token 错误或过期。"""


class ResponseValidationError(DCSGatewayError):
    """网关响应报文不是合法 JSON，或结构与协议不符。"""

    def __init__(
        self,
        message: str,
        *,
        endpoint: str | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message, endpoint=endpoint)
        self.payload = payload


class TagNotFoundError(DCSGatewayError):
    """请求的点位在网关上不存在。"""

    def __init__(
        self,
        tags: Iterable[str],
        *,
        endpoint: str | None = None,
    ) -> None:
        self.tags: Sequence[str] = tuple(tags)
        super().__init__(f"点位不存在: {', '.join(self.tags)}", endpoint=endpoint)


class PointValidationError(DCSGatewayError):
    """点位值无法转换为目标类型，或超出工艺允许范围（如 pH > 14）。"""

    def __init__(self, message: str, *, tag: str | None = None, value: Any = None) -> None:
        super().__init__(message)
        self.tag = tag
        self.value = value

    def __str__(self) -> str:
        if self.tag:
            return f"[{self.tag}] {self.message} (value={self.value!r})"
        return self.message


__all__ = [
    "AuthenticationError",
    "ConfigError",
    "DCSGatewayError",
    "GatewayHTTPError",
    "GatewayOfflineError",
    "GatewayTimeoutError",
    "PointValidationError",
    "ResponseValidationError",
    "TagNotFoundError",
]
