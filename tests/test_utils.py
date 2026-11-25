from datetime import datetime, timedelta, timezone

import pytest

from ragany_dcs_gateway_sdk import PhPoint, PointValidationError, ResponseValidationError, TagMap, TemperaturePoint
from ragany_dcs_gateway_sdk.utils import (
    PointSpec,
    backoff_delay,
    build_tag_path,
    chunked,
    convert_value,
    decode_body,
    dedupe,
    format_timestamp,
    parse_bool,
    parse_json_body,
    parse_retry_after,
    split_tag_path,
    validate_browse_payload,
    validate_read_payload,
    validate_tag_name,
)


class TestTimestamps:
    dt = datetime(2026, 11, 25, 8, 30, 15, 123456, tzinfo=timezone.utc)

    def test_iso(self):
        assert format_timestamp(self.dt) == "2026-11-25T08:30:15.123+00:00"

    def test_ms(self):
        assert format_timestamp(self.dt, "ms") == "1795595415123"

    def test_strftime_with_timezone(self):
        cst = timezone(timedelta(hours=8))
        assert format_timestamp(self.dt, "%Y-%m-%d %H:%M:%S", tz=cst) == "2026-11-25 16:30:15"

    def test_naive_treated_as_utc(self):
        assert format_timestamp(datetime(2026, 1, 1)).endswith("+00:00")


class TestTagPath:
    def test_build_and_split(self):
        path = build_tag_path("s", "CP201", "D122", "Alarm", "high_temp")
        assert path == "s.CP201.D122.Alarm.high_temp"
        assert split_tag_path(path) == ["s", "CP201", "D122", "Alarm", "high_temp"]

    @pytest.mark.parametrize("bad", ["", "   ", "s..D122", ".s", "s.", 123])
    def test_invalid_names(self, bad):
        with pytest.raises(ValueError):
            validate_tag_name(bad)

    def test_build_rejects_empty_segment(self):
        with pytest.raises(ValueError):
            build_tag_path("s", "", "x")
        with pytest.raises(ValueError):
            build_tag_path()

    def test_strips_whitespace(self):
        assert validate_tag_name("  s.CP201.qq1 ") == "s.CP201.qq1"


def test_chunked_and_dedupe():
    assert list(chunked([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(chunked([], 3)) == []
    with pytest.raises(ValueError):
        list(chunked([1], 0))
    assert dedupe(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]


class TestConvertValue:
    @pytest.mark.parametrize(
        "raw, expected",
        [("1", True), ("0", False), ("true", True), ("OFF", False), (1, True), ("2.0", True), ("", False)],
    )
    def test_bool(self, raw, expected):
        assert convert_value(raw, 1) is expected
        assert parse_bool(raw) is expected

    @pytest.mark.parametrize(
        "raw, dtype, expected",
        [
            ("9", 3, 9),
            ("9.0", 3, 9),
            ("65535", 2, 65535),
            ("-32768", 3, -32768),
            ("4294967295", 4, 4294967295),
            ("-5", 5, -5),
            ("6.85", 6, 6.85),
            ("1e3", 7, 1000.0),
            (12, 8, "12"),
        ],
    )
    def test_numeric_and_string(self, raw, dtype, expected):
        assert convert_value(raw, dtype) == expected

    @pytest.mark.parametrize(
        "raw, dtype",
        [("65536", 2), ("-1", 2), ("32768", 3), ("9.5", 3), ("abc", 6), ("maybe", 1)],
    )
    def test_invalid_raises(self, raw, dtype):
        with pytest.raises(PointValidationError) as ei:
            convert_value(raw, dtype, tag="s.x")
        assert ei.value.tag == "s.x"

    def test_none_and_unknown_type_passthrough(self):
        assert convert_value(None, 6) is None
        assert convert_value("raw", 99) == "raw"


class TestPayload:
    def test_decode_utf8_bom_and_gbk(self):
        assert decode_body("﻿{}".encode()) == "{}"
        assert decode_body("发酵罐".encode("gbk")) == "发酵罐"

    def test_parse_json_errors(self):
        with pytest.raises(ResponseValidationError, match="为空"):
            parse_json_body(b"  ")
        with pytest.raises(ResponseValidationError, match="合法 JSON"):
            parse_json_body(b"<html>error</html>", endpoint="ReadTag")

    def test_parse_gbk_json(self):
        assert parse_json_body('{"d": "罐温"}'.encode("gbk")) == {"d": "罐温"}

    def test_validate_read(self):
        assert validate_read_payload({"data": [{"name": "a", "value": "1"}]}) == [{"name": "a", "value": "1"}]
        assert validate_read_payload({"data": None}) == []
        assert validate_read_payload({}) == []
        for bad in ([], {"data": {}}, {"data": [1]}, {"data": [{"value": 1}]}):
            with pytest.raises(ResponseValidationError):
                validate_read_payload(bad)

    def test_validate_browse(self):
        assert validate_browse_payload({"name": "x", "tags": None}) == {"name": "x", "tags": None}
        with pytest.raises(ResponseValidationError):
            validate_browse_payload([])
        with pytest.raises(ResponseValidationError):
            validate_browse_payload({"tags": "oops"})


class TestBackoff:
    def test_exponential_with_cap(self):
        delays = [backoff_delay(n, 0.5, 3.0, jitter=False) for n in range(1, 6)]
        assert delays == [0.5, 1.0, 2.0, 3.0, 3.0]

    def test_jitter_bounds(self):
        for _ in range(100):
            assert 0.75 <= backoff_delay(1, 1.0, 10.0) <= 1.25

    def test_retry_after(self):
        assert backoff_delay(1, 0.1, 10.0, jitter=False, retry_after=4) == 4
        assert backoff_delay(1, 0.1, 2.0, jitter=False, retry_after=60) == 2.0
        assert parse_retry_after("3") == 3.0
        assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") is None
        assert parse_retry_after(None) is None


class TestTagMap:
    def test_tuple_and_spec(self):
        tm = TagMap(
            {
                "ph": ("s.CP201.D122.ph", PhPoint),
                "temp": PointSpec(" s.CP201.D122.temp ", TemperaturePoint),
                "ph_dup": ("s.CP201.D122.ph", PhPoint),
            }
        )
        assert len(tm) == 3
        assert list(tm) == ["ph", "temp", "ph_dup"]
        assert tm["temp"].tag == "s.CP201.D122.temp"
        assert tm.tags == ["s.CP201.D122.ph", "s.CP201.D122.temp"]
        assert "ph=s.CP201.D122.ph" in repr(tm)

    def test_invalid(self):
        with pytest.raises(ValueError):
            TagMap({})
        with pytest.raises(ValueError):
            TagMap({"x": ("bad..path", PhPoint)})
