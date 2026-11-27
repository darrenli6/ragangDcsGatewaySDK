"""SDK 配置：网关地址、超时、重试、采集周期、鉴权 token。

版权所有 © 2026 Ragany DCS Gateway SDK 权利人。保留一切权利。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from .exceptions import ConfigError

ENV_PREFIX = "RAGANY_DCS_"


class GatewayConfig(BaseModel):
    """网关连接配置（不可变）。

    示例::

        cfg = GatewayConfig(base_url="http://192.168.1.88", token="xxx")
        cfg = GatewayConfig.from_env()   # 读取 RAGANY_DCS_* 环境变量
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- 连接 ---
    base_url: str = Field(default="http://192.168.1.88", description="网关根地址，不含 /soap")
    api_prefix: str = Field(default="/soap", description="接口路径前缀")
    verify_ssl: bool = Field(default=True, description="HTTPS 时是否校验证书")
    user_agent: str = Field(default="ragany-dcs-gateway-sdk")

    # --- 鉴权（协议文档未要求，网关前若有反向代理/网关鉴权时使用） ---
    token: SecretStr | None = Field(default=None, description="鉴权 token")
    auth_header: str = Field(default="Authorization")
    auth_scheme: str = Field(default="Bearer", description="为空字符串时直接发送 token")

    # --- 超时（秒） ---
    connect_timeout: float = Field(default=3.0, gt=0)
    read_timeout: float = Field(default=10.0, gt=0)

    # --- 重试：指数退避 + 抖动 ---
    max_retries: int = Field(default=3, ge=0, description="失败后额外重试次数")
    backoff_factor: float = Field(default=0.5, ge=0, description="第 n 次重试等待 factor * 2**(n-1) 秒")
    backoff_max: float = Field(default=10.0, ge=0)
    retry_on_status: frozenset[int] = Field(default=frozenset({429, 502, 503, 504}))

    # --- 连接池 ---
    max_connections: int = Field(default=10, gt=0)
    max_keepalive_connections: int = Field(default=5, ge=0)

    # --- 采集 ---
    poll_interval: float = Field(default=5.0, gt=0, description="轮询采集周期（秒）")
    read_batch_size: int = Field(default=100, gt=0, description="单次 ReadTag 最多请求的点位数")

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not v:
            raise ValueError("base_url 不能为空")
        if not v.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        return v

    @field_validator("api_prefix")
    @classmethod
    def _check_prefix(cls, v: str) -> str:
        v = v.strip().strip("/")
        return f"/{v}" if v else ""

    @field_validator("token", mode="before")
    @classmethod
    def _empty_token_is_none(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @classmethod
    def create(cls, **kwargs: Any) -> GatewayConfig:
        """与构造函数相同，但把 pydantic 校验错误统一转为 :class:`ConfigError`。"""
        try:
            return cls(**kwargs)
        except ValidationError as exc:
            raise ConfigError(f"网关配置非法: {exc}") from exc

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        **overrides: Any,
    ) -> GatewayConfig:
        """从环境变量读取配置，例如 ``RAGANY_DCS_BASE_URL``、``RAGANY_DCS_TOKEN``。

        ``overrides`` 中的参数优先级最高。
        """
        env = os.environ if environ is None else environ
        values: dict[str, Any] = {}
        for name in cls.model_fields:
            key = f"{ENV_PREFIX}{name.upper()}"
            if key in env and name != "retry_on_status":
                values[name] = env[key]
        if f"{ENV_PREFIX}RETRY_ON_STATUS" in env:
            raw = env[f"{ENV_PREFIX}RETRY_ON_STATUS"]
            try:
                values["retry_on_status"] = frozenset(int(x) for x in raw.split(",") if x.strip())
            except ValueError as exc:
                raise ConfigError(f"{ENV_PREFIX}RETRY_ON_STATUS 非法: {raw!r}") from exc
        values.update(overrides)
        return cls.create(**values)

    # --- 便捷属性 ---
    def endpoint_url(self, function: str) -> str:
        """返回接口完整 URL，例如 ``http://192.168.1.88/soap/ReadTag``。"""
        return f"{self.base_url}{self.api_prefix}/{function}"

    def auth_headers(self) -> dict[str, str]:
        if self.token is None:
            return {}
        secret = self.token.get_secret_value()
        value = f"{self.auth_scheme} {secret}" if self.auth_scheme else secret
        return {self.auth_header: value}


__all__ = ["ENV_PREFIX", "GatewayConfig"]
