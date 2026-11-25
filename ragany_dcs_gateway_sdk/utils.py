"""工具函数：时间格式化、点位路径、数据类型转换、报文解析与校验、点位映射。

本模块不依赖 :mod:`models`，以避免循环引用。
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import (
    TYPE_CHECKING,
    Any,
    TypeVar,
)

from .exceptions import PointValidationError, ResponseValidationError

if TYPE_CHECKING:  # pragma: no cover
    from .models import ProcessPoint

T = TypeVar("T")

TAG_SEPARATOR = "."

# 协议数据类型编码：1-Boolean 2-Word 3-Short 4-Dword 5-Long 6-Float 7-Double 8-String
_INT_RANGES: dict[int, tuple[int, int]] = {
    2: (0, 0xFFFF),  # Word
    3: (-0x8000, 0x7FFF),  # Short
    4: (0, 0xFFFFFFFF),  # Dword
    5: (-0x80000000, 0x7FFFFFFF),  # Long
}
_TRUE_STRINGS = frozenset({"1", "true", "on", "yes", "t", "y"})
_FALSE_STRINGS = frozenset({"0", "false", "off", "no", "f", "n", ""})


# --------------------------------------------------------------------------- #
# 时间
# --------------------------------------------------------------------------- #
def utc_now() -> datetime:
    """返回带时区的当前 UTC 时间。"""
    return datetime.now(timezone.utc)


def format_timestamp(
    dt: datetime,
    fmt: str = "iso",
    tz: tzinfo | None = None,
) -> str:
    """格式化时间戳。

    :param fmt: ``"iso"`` → ISO-8601（毫秒精度）；``"ms"`` → Unix 毫秒；
        其它字符串按 ``strftime`` 格式处理，例如 ``"%Y-%m-%d %H:%M:%S"``。
    :param tz: 输出时区，默认保持原时区；naive 时间按 UTC 处理。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if tz is not None:
        dt = dt.astimezone(tz)
    if fmt == "iso":
        return dt.isoformat(timespec="milliseconds")
    if fmt == "ms":
        return str(int(dt.timestamp() * 1000))
    return dt.strftime(fmt)


# --------------------------------------------------------------------------- #
# 点位路径
# --------------------------------------------------------------------------- #
def validate_tag_name(name: str) -> str:
    """校验点位全路径（驱动.通道.设备[.组...].点位），返回去除首尾空白后的名称。"""
    if not isinstance(name, str):
        raise ValueError(f"点位名称必须是字符串，实际为 {type(name).__name__}")
    stripped = name.strip()
    if not stripped:
        raise ValueError("点位名称不能为空")
    if any(not seg.strip() for seg in stripped.split(TAG_SEPARATOR)):
        raise ValueError(f"点位名称包含空路径段: {name!r}")
    return stripped


def build_tag_path(*segments: str) -> str:
    """拼接点位路径：``build_tag_path("s", "CP201", "D122", "qq1") -> "s.CP201.D122.qq1"``。"""
    if not segments:
        raise ValueError("至少需要一个路径段")
    cleaned: list[str] = []
    for seg in segments:
        if not isinstance(seg, str) or not seg.strip():
            raise ValueError(f"路径段不能为空: {segments!r}")
        cleaned.append(seg.strip())
    return validate_tag_name(TAG_SEPARATOR.join(cleaned))


def split_tag_path(name: str) -> list[str]:
    """拆分点位路径为各段。"""
    return validate_tag_name(name).split(TAG_SEPARATOR)


def join_path(prefix: str, name: str) -> str:
    return f"{prefix}{TAG_SEPARATOR}{name}" if prefix else name


def dedupe(items: Iterable[T]) -> list[T]:
    """保序去重。"""
    seen: set[T] = set()
    out: list[T] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def chunked(items: Sequence[T], size: int) -> Iterator[list[T]]:
    """按 ``size`` 切分序列，用于把大批量 ReadTag 拆成多次请求。"""
    if size <= 0:
        raise ValueError("size 必须大于 0")
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


# --------------------------------------------------------------------------- #
# 数据类型转换
# --------------------------------------------------------------------------- #
def parse_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return raw != 0
    text = str(raw).strip().lower()
    if text in _TRUE_STRINGS:
        return True
    if text in _FALSE_STRINGS:
        return False
    try:
        return float(text) != 0
    except ValueError:
        raise ValueError(f"无法解析为布尔值: {raw!r}") from None


def parse_int(raw: Any) -> int:
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    try:
        return int(text)
    except ValueError:
        pass
    f = float(text)  # 网关可能把整型返回成 "9.0"
    if not f.is_integer():
        raise ValueError(f"不是整数: {raw!r}")
    return int(f)


def parse_float(raw: Any) -> float:
    if isinstance(raw, (bool, int, float)):
        return float(raw)
    return float(str(raw).strip())


def convert_value(raw: Any, data_type: int | None, *, tag: str | None = None) -> Any:
    """按协议数据类型编码把网关返回的原始值（通常为字符串）转换为 Python 类型。

    未知类型或 ``data_type`` 为 ``None`` 时原样返回。
    转换失败抛出 :class:`PointValidationError`。
    """
    if raw is None:
        return None
    try:
        if data_type == 1:
            return parse_bool(raw)
        if data_type in _INT_RANGES:
            value = parse_int(raw)
            low, high = _INT_RANGES[data_type]
            if not low <= value <= high:
                raise ValueError(f"超出类型范围 [{low}, {high}]")
            return value
        if data_type in (6, 7):
            return parse_float(raw)
        if data_type == 8:
            return str(raw)
    except (TypeError, ValueError) as exc:
        raise PointValidationError(f"无法转换为数据类型 {data_type}: {exc}", tag=tag, value=raw) from exc
    return raw


def is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# --------------------------------------------------------------------------- #
# 报文解析与校验
# --------------------------------------------------------------------------- #
def decode_body(content: bytes) -> str:
    """解码响应体。兼容 UTF-8（含 BOM）以及国产网关常见的 GBK/GB18030 编码。"""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")  # pragma: no cover - gb18030 几乎不会失败


def parse_json_body(content: bytes, *, endpoint: str | None = None) -> Any:
    text = decode_body(content).strip()
    if not text:
        raise ResponseValidationError("响应体为空", endpoint=endpoint, payload=text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResponseValidationError(
            f"响应不是合法 JSON: {exc.msg} (line {exc.lineno} col {exc.colno})",
            endpoint=endpoint,
            payload=text[:500],
        ) from exc


def validate_browse_payload(payload: Any, *, endpoint: str | None = None) -> dict[str, Any]:
    """校验 BrowseTag 响应：必须是 JSON 对象，结构列表字段必须是数组。"""
    if not isinstance(payload, dict):
        raise ResponseValidationError("BrowseTag 响应必须是 JSON 对象", endpoint=endpoint, payload=payload)
    for key in ("drivers", "channels", "devices", "groups", "tags"):
        if key in payload and payload[key] is not None and not isinstance(payload[key], list):
            raise ResponseValidationError(f"BrowseTag 响应字段 {key!r} 必须是数组", endpoint=endpoint, payload=payload)
    return payload


def validate_read_payload(payload: Any, *, endpoint: str | None = None) -> list[dict[str, Any]]:
    """校验 ReadTag 响应：``{"data": [{"name": str, "value": ...}, ...]}``，返回 data 列表。"""
    if not isinstance(payload, dict):
        raise ResponseValidationError("ReadTag 响应必须是 JSON 对象", endpoint=endpoint, payload=payload)
    data = payload.get("data")
    if data is None:
        data = []
    if not isinstance(data, list):
        raise ResponseValidationError("ReadTag 响应字段 'data' 必须是数组", endpoint=endpoint, payload=payload)
    for i, item in enumerate(data):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ResponseValidationError(
                f"ReadTag 响应 data[{i}] 缺少字符串字段 'name'", endpoint=endpoint, payload=payload
            )
    return data


# --------------------------------------------------------------------------- #
# 重试退避
# --------------------------------------------------------------------------- #
def backoff_delay(
    retry_number: int,
    factor: float,
    maximum: float,
    *,
    jitter: bool = True,
    retry_after: float | None = None,
) -> float:
    """计算第 ``retry_number``（从 1 开始）次重试前的等待秒数。

    指数退避 ``factor * 2**(n-1)``，上限 ``maximum``；带 ±25% 抖动，避免多台采集机同时冲击网关。
    若服务端给出 ``Retry-After``，取二者较大值（仍受上限约束）。
    """
    delay: float = min(factor * 2.0 ** (retry_number - 1), maximum)
    if jitter and delay > 0:
        delay *= random.uniform(0.75, 1.25)
    if retry_after is not None:
        delay = max(delay, retry_after)
    return max(0.0, min(delay, maximum))


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date 格式在工业网关中极少见，忽略


# --------------------------------------------------------------------------- #
# 点位映射：逻辑名 → (网关点位全路径, 工艺点位模型)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PointSpec:
    tag: str
    point_type: type[ProcessPoint]


class TagMap:
    """工艺逻辑名到网关点位的映射，屏蔽现场点位命名差异。

    示例::

        tag_map = TagMap({
            "ph":          ("s.CP201.D122.ph",   PhPoint),
            "temperature": ("s.CP201.D122.temp", TemperaturePoint),
        })
    """

    def __init__(
        self,
        mapping: Mapping[str, PointSpec | tuple[str, type[ProcessPoint]]],
    ) -> None:
        if not mapping:
            raise ValueError("TagMap 不能为空")
        self._specs: dict[str, PointSpec] = {}
        for key, spec in mapping.items():
            if not isinstance(spec, PointSpec):
                tag, point_type = spec
                spec = PointSpec(tag=tag, point_type=point_type)
            self._specs[key] = PointSpec(tag=validate_tag_name(spec.tag), point_type=spec.point_type)

    def __getitem__(self, key: str) -> PointSpec:
        return self._specs[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def items(self) -> Iterator[tuple[str, PointSpec]]:
        return iter(self._specs.items())

    @property
    def tags(self) -> list[str]:
        return dedupe(spec.tag for spec in self._specs.values())

    def __repr__(self) -> str:
        inner = ", ".join(f"{k}={v.tag}" for k, v in self._specs.items())
        return f"TagMap({inner})"


__all__ = [
    "PointSpec",
    "TAG_SEPARATOR",
    "TagMap",
    "backoff_delay",
    "build_tag_path",
    "chunked",
    "convert_value",
    "decode_body",
    "dedupe",
    "format_timestamp",
    "parse_bool",
    "parse_float",
    "parse_int",
    "parse_json_body",
    "parse_retry_after",
    "split_tag_path",
    "utc_now",
    "validate_browse_payload",
    "validate_read_payload",
    "validate_tag_name",
]
