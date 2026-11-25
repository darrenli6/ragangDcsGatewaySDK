import math
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from ragany_dcs_gateway_sdk import (
    AnalogPoint,
    BatchData,
    BrowseNode,
    DataType,
    DissolvedOxygenPoint,
    LevelPoint,
    PhPoint,
    PointValidationError,
    PressurePoint,
    TagInfo,
    TagValue,
    TemperaturePoint,
    TempPoint,
)
from ragany_dcs_gateway_sdk.testing import DEMO_TREE


class TestTagInfo:
    def test_protocol_short_fields(self):
        tag = TagInfo.model_validate({"n": "qq1", "d": "搅拌", "t": 3, "u": 95, "v": "9", "w": 1})
        assert tag.name == "qq1"
        assert tag.description == "搅拌"
        assert tag.data_type is DataType.SHORT
        assert tag.writable is True
        assert tag.has_unit is False
        assert tag.value == 9

    def test_defaults_and_unknown_type(self):
        tag = TagInfo.model_validate({"n": "x", "t": 42, "d": None})
        assert tag.data_type is None
        assert tag.description == ""
        assert tag.writable is False
        assert tag.value is None

    def test_extra_fields_preserved(self):
        tag = TagInfo.model_validate({"n": "x", "vendor_field": 1})
        assert tag.model_extra == {"vendor_field": 1}


class TestBrowseNode:
    def test_iter_tags_builds_full_paths(self):
        root = BrowseNode.model_validate(DEMO_TREE)
        paths = [t.path for t in root.iter_tags()]
        assert "s.CP201.D122.ph" in paths
        assert "s.CP201.D122.Alarm.high_temp" in paths  # 设备下有分组时路径包含组名
        assert not any(p.startswith("VarDevice") for p in paths)  # 根节点名不计入路径
        assert len(paths) == 8

    def test_iter_tags_with_prefix(self):
        root = BrowseNode.model_validate(DEMO_TREE)
        channel = root.find("s.CP201")
        assert channel is not None
        paths = [t.path for t in channel.iter_tags(prefix="s.CP201")]
        assert paths[0] == "s.CP201.D122.ph"

    def test_find_missing(self):
        root = BrowseNode.model_validate(DEMO_TREE)
        assert root.find("s.NOPE") is None

    def test_empty_doc_example(self):
        root = BrowseNode.model_validate({"drivers": [], "name": "VarDevice", "tags": []})
        assert root.name == "VarDevice"
        assert list(root.iter_tags()) == []

    def test_null_lists(self):
        root = BrowseNode.model_validate({"name": "r", "drivers": None, "tags": None, "d": None})
        assert root.children == [] and root.tags == []


class TestTagValue:
    def test_conversions(self):
        tv = TagValue.model_validate({"name": "s.a", "value": "9"})
        assert tv.raw == "9"
        assert tv.as_int() == 9
        assert tv.as_float() == 9.0
        assert tv.as_bool() is True
        assert tv.as_str() == "9"
        assert tv.as_type(DataType.SHORT) == 9
        assert tv.read_at.tzinfo is not None

    def test_null_value(self):
        tv = TagValue(name="s.a", raw=None)
        assert tv.is_null and tv.as_str() == ""
        with pytest.raises(PointValidationError, match="为空"):
            tv.as_float()

    def test_bad_value(self):
        with pytest.raises(PointValidationError) as ei:
            TagValue(name="s.a", raw="#ERR").as_float()
        assert ei.value.tag == "s.a" and ei.value.value == "#ERR"
        assert "s.a" in str(ei.value)


class TestProcessPoints:
    @pytest.mark.parametrize(
        "cls, ok, low, high",
        [
            (PhPoint, 6.8, -0.1, 14.1),
            (TemperaturePoint, 121.0, -51, 201),
            (LevelPoint, 50, -1, 100.1),
            (DissolvedOxygenPoint, 120, -1, 201),
            (PressurePoint, 0.25, -0.2, 2.0),
        ],
    )
    def test_ranges(self, cls, ok, low, high):
        assert cls.from_tag_value(TagValue(name="t", raw=str(ok))).value == ok
        for bad in (low, high):
            with pytest.raises(PointValidationError, match=cls.__name__):
                cls.from_tag_value(TagValue(name="t", raw=str(bad)))

    @pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
    def test_non_finite_rejected(self, raw):
        with pytest.raises(PointValidationError, match="仪表故障"):
            AnalogPoint.from_tag_value(TagValue(name="t", raw=raw))

    def test_metadata_and_display(self):
        p = PhPoint.from_tag_value(TagValue(name="s.ph", raw="6.85"))
        assert p.tag == "s.ph" and p.unit == "pH"
        assert p.display() == "6.85 pH"
        assert TempPoint is TemperaturePoint
        assert AnalogPoint(tag="x", value=1e9).display() == "1e+09"

    def test_custom_range_subclass(self):
        class SeedTankTemp(TemperaturePoint):
            max_value = 45.0

        with pytest.raises(PointValidationError):
            SeedTankTemp.from_tag_value(TagValue(name="t", raw="50"))
        assert TemperaturePoint.from_tag_value(TagValue(name="t", raw="50")).value == 50

    def test_frozen(self):
        p = PhPoint(tag="x", value=7)
        with pytest.raises(ValidationError):
            p.value = 8  # type: ignore[misc]


class TestBatchData:
    def test_accessors_and_record(self):
        ts = datetime(2026, 11, 25, tzinfo=timezone.utc)
        batch = BatchData(
            batch_id="B20261125-01",
            unit_id="F-101",
            collected_at=ts,
            points={
                "ph": PhPoint(tag="a", value=6.9),
                "temp": TemperaturePoint(tag="b", value=37.0),
            },
        )
        assert batch.ok
        assert batch.ph is not None and batch.ph.value == 6.9
        assert batch.temperature is not None and batch.temperature.value == 37.0
        assert batch.level is None
        assert batch.get("ph") is batch.points["ph"]
        assert batch.to_record() == {
            "batch_id": "B20261125-01",
            "unit_id": "F-101",
            "collected_at": "2026-11-25T00:00:00.000+00:00",
            "ph": 6.9,
            "temp": 37.0,
        }

    def test_errors_flag(self):
        assert not BatchData(errors={"ph": "bad"}).ok
        assert not math.isnan(BatchData().collected_at.timestamp())
