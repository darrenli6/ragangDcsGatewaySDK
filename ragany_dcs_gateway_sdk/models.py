"""Pydantic 模型：网关点位结构、读值结果、发酵工艺点位与批次数据。"""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import datetime
from enum import IntEnum
from typing import Any, ClassVar, TypeVar

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator

from .exceptions import PointValidationError
from .utils import (
    convert_value,
    format_timestamp,
    join_path,
    parse_bool,
    parse_float,
    parse_int,
    split_tag_path,
    utc_now,
)

NO_UNIT = 95
"""协议约定：单位编码 95 表示无单位。"""


class DataType(IntEnum):
    """点位数据类型（协议字段 ``t``）。"""

    BOOLEAN = 1
    WORD = 2
    SHORT = 3
    DWORD = 4
    LONG = 5
    FLOAT = 6
    DOUBLE = 7
    STRING = 8


class AccessMode(IntEnum):
    """读写标志（协议字段 ``w``）。"""

    READ_ONLY = 0
    READ_WRITE = 1


# --------------------------------------------------------------------------- #
# BrowseTag 结构
# --------------------------------------------------------------------------- #
class TagInfo(BaseModel):
    """BrowseTag 返回的单个点位定义。协议短字段名通过 alias 映射为可读属性。"""

    model_config = ConfigDict(populate_by_name=True, extra="allow", frozen=True)

    name: str = Field(validation_alias=AliasChoices("n", "name"))
    description: str = Field(default="", validation_alias=AliasChoices("d", "description"))
    data_type_code: int = Field(default=DataType.STRING, validation_alias=AliasChoices("t", "data_type_code"))
    unit_code: int = Field(default=NO_UNIT, validation_alias=AliasChoices("u", "unit_code"))
    raw_value: Any = Field(default=None, validation_alias=AliasChoices("v", "raw_value"))
    access_code: int = Field(default=AccessMode.READ_ONLY, validation_alias=AliasChoices("w", "access_code"))
    path: str = Field(default="", description="点位全路径，由 BrowseNode.iter_tags 填充")

    @field_validator("description", mode="before")
    @classmethod
    def _none_to_empty(cls, v: Any) -> Any:
        return "" if v is None else v

    @property
    def data_type(self) -> DataType | None:
        """数据类型枚举；网关返回未知编码时为 ``None``。"""
        try:
            return DataType(self.data_type_code)
        except ValueError:
            return None

    @property
    def writable(self) -> bool:
        return self.access_code == AccessMode.READ_WRITE

    @property
    def has_unit(self) -> bool:
        return self.unit_code != NO_UNIT

    @property
    def value(self) -> Any:
        """按数据类型转换后的当前值。"""
        return convert_value(self.raw_value, self.data_type_code, tag=self.path or self.name)


class BrowseNode(BaseModel):
    """BrowseTag 返回的树节点（驱动 / 通道 / 设备 / 组 通用结构）。"""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    name: str = Field(default="", validation_alias=AliasChoices("name", "n"))
    description: str = Field(default="", validation_alias=AliasChoices("d", "description"))
    drivers: list[BrowseNode] = Field(default_factory=list)
    channels: list[BrowseNode] = Field(default_factory=list)
    devices: list[BrowseNode] = Field(default_factory=list)
    groups: list[BrowseNode] = Field(default_factory=list)
    tags: list[TagInfo] = Field(default_factory=list)

    @field_validator("drivers", "channels", "devices", "groups", "tags", mode="before")
    @classmethod
    def _none_to_list(cls, v: Any) -> Any:
        return [] if v is None else v

    @field_validator("description", mode="before")
    @classmethod
    def _none_to_empty(cls, v: Any) -> Any:
        return "" if v is None else v

    @property
    def children(self) -> list[BrowseNode]:
        return [*self.drivers, *self.channels, *self.devices, *self.groups]

    def iter_tags(self, prefix: str = "") -> Iterator[TagInfo]:
        """深度优先遍历所有点位，并填充全路径 ``path``。

        :param prefix: 当前节点的全路径。浏览全部（``node=""``）时为空，
            根节点名（如 ``VarDevice``）不计入路径；浏览 ``Driver.Channel`` 时传入该路径。
        """
        for tag in self.tags:
            yield tag.model_copy(update={"path": join_path(prefix, tag.name)})
        for child in self.children:
            yield from child.iter_tags(join_path(prefix, child.name))

    def find(self, path: str) -> BrowseNode | None:
        """按相对路径查找子节点，例如 ``root.find("s.CP201")``。"""
        node: BrowseNode = self
        for seg in split_tag_path(path):
            nxt = next((c for c in node.children if c.name == seg), None)
            if nxt is None:
                return None
            node = nxt
        return node


BrowseNode.model_rebuild()


# --------------------------------------------------------------------------- #
# ReadTag 结果
# --------------------------------------------------------------------------- #
class TagValue(BaseModel):
    """ReadTag 返回的单个点位值。网关值为字符串，提供类型安全的转换方法。"""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    name: str
    raw: Any = Field(default=None, validation_alias=AliasChoices("value", "raw"))
    read_at: datetime = Field(default_factory=utc_now, description="SDK 收到响应的 UTC 时间")

    @property
    def is_null(self) -> bool:
        return self.raw is None

    def _convert(self, fn: Any, kind: str) -> Any:
        if self.raw is None:
            raise PointValidationError(f"点位值为空，无法转换为 {kind}", tag=self.name, value=None)
        try:
            return fn(self.raw)
        except (TypeError, ValueError) as exc:
            raise PointValidationError(f"无法转换为 {kind}: {exc}", tag=self.name, value=self.raw) from exc

    def as_float(self) -> float:
        return float(self._convert(parse_float, "float"))

    def as_int(self) -> int:
        return int(self._convert(parse_int, "int"))

    def as_bool(self) -> bool:
        return bool(self._convert(parse_bool, "bool"))

    def as_str(self) -> str:
        return "" if self.raw is None else str(self.raw)

    def as_type(self, data_type: int) -> Any:
        """按协议数据类型编码转换（与 BrowseTag 中的 ``t`` 字段配合使用）。"""
        return convert_value(self.raw, data_type, tag=self.name)


# --------------------------------------------------------------------------- #
# 发酵工艺点位
# --------------------------------------------------------------------------- #
P = TypeVar("P", bound="ProcessPoint")


class ProcessPoint(BaseModel):
    """模拟量工艺点位基类：数值 + 单位 + 工艺量程校验。

    子类通过 ``unit`` / ``min_value`` / ``max_value`` 声明工艺属性；
    现场量程不同时继承并覆盖即可::

        class FermenterTemp(TemperaturePoint):
            min_value = 0.0
            max_value = 130.0
    """

    model_config = ConfigDict(frozen=True)

    unit: ClassVar[str] = ""
    min_value: ClassVar[float | None] = None
    max_value: ClassVar[float | None] = None

    tag: str
    value: float
    read_at: datetime = Field(default_factory=utc_now)

    @field_validator("value")
    @classmethod
    def _check_range(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("数值非有限（NaN/Inf），可能是仪表故障")
        if cls.min_value is not None and v < cls.min_value:
            raise ValueError(f"低于工艺下限 {cls.min_value}{cls.unit}")
        if cls.max_value is not None and v > cls.max_value:
            raise ValueError(f"高于工艺上限 {cls.max_value}{cls.unit}")
        return v

    @classmethod
    def from_tag_value(cls: type[P], tv: TagValue) -> P:
        """由 ReadTag 结果构造工艺点位；转换或量程校验失败抛出 :class:`PointValidationError`。"""
        value = tv.as_float()
        try:
            return cls(tag=tv.name, value=value, read_at=tv.read_at)
        except ValidationError as exc:
            msg = "; ".join(str(e.get("msg", "")).removeprefix("Value error, ") for e in exc.errors())
            raise PointValidationError(f"{cls.__name__} 校验失败: {msg}", tag=tv.name, value=tv.raw) from exc

    def display(self) -> str:
        return f"{self.value:g} {self.unit}".strip()


class AnalogPoint(ProcessPoint):
    """无量程约束的通用模拟量。"""


class PhPoint(ProcessPoint):
    """发酵液 pH，量程 0~14。"""

    unit = "pH"
    min_value = 0.0
    max_value = 14.0


class TemperaturePoint(ProcessPoint):
    """温度（℃）。默认覆盖发酵培养与 121℃ 在位灭菌（SIP）工况。"""

    unit = "℃"
    min_value = -50.0
    max_value = 200.0


class LevelPoint(ProcessPoint):
    """罐体液位（%）。"""

    unit = "%"
    min_value = 0.0
    max_value = 100.0


class DissolvedOxygenPoint(ProcessPoint):
    """溶氧 DO（%饱和度），允许过饱和。"""

    unit = "%DO"
    min_value = 0.0
    max_value = 200.0


class PressurePoint(ProcessPoint):
    """罐压（MPa，表压）。"""

    unit = "MPa"
    min_value = -0.1
    max_value = 1.6


# 兼容常用命名
TempPoint = TemperaturePoint


class BatchData(BaseModel):
    """一次批量采集的工艺数据快照（例如一个发酵罐在某一时刻的全部关键参数）。

    单个仪表故障不会让整批采集失败：校验失败的点位记录在 ``errors`` 中。
    """

    batch_id: str | None = None
    unit_id: str | None = Field(default=None, description="设备/罐号，如 F-101")
    collected_at: datetime = Field(default_factory=utc_now)
    points: dict[str, ProcessPoint] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def get(self, key: str) -> ProcessPoint | None:
        return self.points.get(key)

    def _first(self, kind: type[ProcessPoint]) -> ProcessPoint | None:
        return next((p for p in self.points.values() if isinstance(p, kind)), None)

    @property
    def ph(self) -> ProcessPoint | None:
        return self._first(PhPoint)

    @property
    def temperature(self) -> ProcessPoint | None:
        return self._first(TemperaturePoint)

    @property
    def level(self) -> ProcessPoint | None:
        return self._first(LevelPoint)

    def to_record(self) -> dict[str, Any]:
        """扁平化为一行记录，便于写入时序库 / CSV / 消息队列。"""
        rec: dict[str, Any] = {
            "batch_id": self.batch_id,
            "unit_id": self.unit_id,
            "collected_at": format_timestamp(self.collected_at),
        }
        for key, point in self.points.items():
            rec[key] = point.value
        return rec


__all__ = [
    "AccessMode",
    "AnalogPoint",
    "BatchData",
    "BrowseNode",
    "DataType",
    "DissolvedOxygenPoint",
    "LevelPoint",
    "NO_UNIT",
    "PhPoint",
    "PressurePoint",
    "ProcessPoint",
    "TagInfo",
    "TagValue",
    "TempPoint",
    "TemperaturePoint",
]
