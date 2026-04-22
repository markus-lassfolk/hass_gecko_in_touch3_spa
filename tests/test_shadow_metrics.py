"""Tests for ``custom_components.gecko.shadow_metrics`` heuristics."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from custom_components.gecko import shadow_metrics
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import UnitOfTemperature


def test_path_reserved_for_number_control_unknown_zone_setpoint() -> None:
    assert shadow_metrics.path_reserved_for_number_control(
        "zones.watercare.z1.setpoint"
    )
    assert not shadow_metrics.path_reserved_for_number_control("zones.flow.z1.setpoint")
    assert not shadow_metrics.path_reserved_for_number_control("zones.other.z1.temp")


def test_path_looks_sensitive() -> None:
    assert shadow_metrics._path_looks_sensitive("features.waterlab.password_field")
    assert not shadow_metrics._path_looks_sensitive("features.waterlab.ph")


def test_string_value_ok() -> None:
    assert shadow_metrics._string_value_ok("ok")
    assert not shadow_metrics._string_value_ok("")
    assert not shadow_metrics._string_value_ok("eyJxxx")
    assert not shadow_metrics._string_value_ok("x" * 300)


@pytest.mark.parametrize(
    ("seg", "is_ph"),
    [
        ("ph", True),
        ("phValue", True),
        ("phosphate", False),
        ("phone", False),
        ("phase", False),
    ],
)
def test_segment_is_ph(seg: str, is_ph: bool) -> None:
    assert shadow_metrics._segment_is_ph(seg) is is_ph


@pytest.mark.parametrize(
    ("seg", "is_orp"),
    [
        ("orp", True),
        ("orpMv", True),
        ("orphan", False),
        ("orphaned", False),
    ],
)
def test_segment_is_orp(seg: str, is_orp: bool) -> None:
    assert shadow_metrics._segment_is_orp(seg) is is_orp


@pytest.mark.parametrize(
    "path",
    [
        "features.waterlab.sensor.ph.offsetMv",
        "features.waterlab.sensor.ph.slopeMvPerPh",
        "features.waterlab.sensor.therm.R0",
    ],
)
def test_is_calibration_or_model_param_path(path: str) -> None:
    assert shadow_metrics._is_calibration_or_model_param_path(path)


def test_get_reported_variants() -> None:
    assert shadow_metrics._get_reported(None) == {}
    assert shadow_metrics._get_reported({}) == {}
    inner = {"state": {"reported": {"a": 1}}}
    assert shadow_metrics._get_reported(inner) == {"a": 1}
    assert shadow_metrics._get_reported({"reported": {"b": 2}}) == {"b": 2}


def test_flatten_numeric_skips_bool_and_nan() -> None:
    out: dict[str, float | int] = {}
    shadow_metrics._flatten_numeric(
        {"n": 1, "b": True, "f": float("nan"), "nested": {"x": 2}},
        "root",
        out,
        0,
    )
    assert out == {"root.n": 1, "root.nested.x": 2}


def test_extract_extension_metrics_zones_and_features() -> None:
    state = {
        "state": {
            "reported": {
                "zones": {
                    "waterlab": {"z1": {"reading": 7.2}},
                    "flow": {"f1": {"ignored": 1}},
                },
                "features": {"extra": {"n": 3}},
                "connectivity": {"rssi": -50},
            }
        }
    }
    m = shadow_metrics.extract_extension_metrics(state)
    assert m["zones.waterlab.z1.reading"] == 7.2
    assert not any(k.startswith("zones.flow") for k in m)
    assert m["features.extra.n"] == 3
    assert m["connectivity.rssi"] == -50


def test_extract_extension_strings_skips_operation_mode() -> None:
    state = {
        "state": {
            "reported": {
                "zones": {"custom": {"z": {"label": "x"}}},
                "features": {"operationMode": {"name": "Away"}},
            }
        }
    }
    s = shadow_metrics.extract_extension_strings(state)
    assert any("custom" in k for k in s)
    assert not any(k.lower().startswith("features.operationmode") for k in s)


def test_shadow_topology_summary() -> None:
    summary = shadow_metrics.shadow_topology_summary(
        {"state": {"reported": {"zones": {"a": {"z": 1}}}}}
    )
    assert "reported_top_level_keys" in summary
    assert "zones" in summary["reported_top_level_keys"]


def test_metric_path_to_entity_slug_truncation_collision_avoidance() -> None:
    long_path = "zones." + "x" * 100 + ".value"
    slug = shadow_metrics.metric_path_to_entity_slug(long_path, max_len=20)
    assert len(slug) <= 20
    slug_b = shadow_metrics.metric_path_to_entity_slug(long_path + "2", max_len=20)
    assert slug != slug_b


def test_infer_sensor_metadata_ph_orp_temp() -> None:
    dc, unit = shadow_metrics.infer_sensor_metadata("zones.x.phValue")
    assert dc == SensorDeviceClass.PH
    assert unit is None
    dc2, u2 = shadow_metrics.infer_sensor_metadata("something.orp_mv.reading")
    assert dc2 == SensorDeviceClass.VOLTAGE
    assert u2 == "mV"
    dc3, u3 = shadow_metrics.infer_sensor_metadata("zones.flow.temp_sensor")
    assert dc3 == SensorDeviceClass.TEMPERATURE
    assert u3 == UnitOfTemperature.CELSIUS


def test_infer_sensor_metadata_calibration_returns_none() -> None:
    dc, unit = shadow_metrics.infer_sensor_metadata(
        "features.waterlab.sensor.ph.offsetMv"
    )
    assert dc is None and unit is None


def test_shadow_extension_diagnostic_disables_registry_default() -> None:
    assert shadow_metrics.shadow_extension_diagnostic_disables_registry_default(
        "features.waterlab.sensor.ph.slopeMvPerPh"
    )
    assert shadow_metrics.shadow_extension_diagnostic_disables_registry_default(
        "connectivity.wifi.rssi"
    )


def test_classify_gecko_shadow_metric_buckets() -> None:
    assert (
        shadow_metrics.classify_gecko_shadow_metric(
            "features.waterlab.sensor.ph.offsetMv"
        )
        == "calibration_model"
    )
    assert shadow_metrics.classify_gecko_shadow_metric("x.rssi.live") == "rf"
    assert (
        shadow_metrics.classify_gecko_shadow_metric("connectivity.uptime")
        == "connectivity"
    )


def test_shadow_metric_icon_matches_bucket() -> None:
    assert shadow_metrics.shadow_metric_icon("any.thing") == "mdi:gauge"
    assert shadow_metrics.shadow_metric_icon("zones.w.ph") == "mdi:water-opacity"


def test_apply_numeric_shadow_sensor_hints_sets_attrs() -> None:
    ent = SimpleNamespace()
    shadow_metrics.apply_numeric_shadow_sensor_hints(ent, "zones.z.phReading")
    assert ent._attr_device_class == SensorDeviceClass.PH
    assert ent._attr_suggested_display_precision == 2


def test_chemistry_metric_enabled_by_default() -> None:
    assert shadow_metrics.chemistry_metric_enabled_by_default("zones.x.phValue")
    assert not shadow_metrics.chemistry_metric_enabled_by_default(
        "features.waterlab.sensor.ph.offsetMv"
    )
    assert shadow_metrics.chemistry_metric_enabled_by_default("cloud.rest.summary.ph")


def test_parse_unknown_zone_setpoint_path() -> None:
    assert shadow_metrics.parse_unknown_zone_setpoint_path(
        "zones.spa.z1.targetTemp"
    ) == ("spa", "z1", "targetTemp")
    assert shadow_metrics.parse_unknown_zone_setpoint_path("zones.flow.z1.sp") is None


def test_infer_number_setpoint_limits() -> None:
    assert shadow_metrics.infer_number_setpoint_limits("zones.x.ph", "reading") == (
        0.0,
        14.0,
        0.1,
    )
    assert shadow_metrics.infer_number_setpoint_limits("zones.x.orp", "mv") == (
        0.0,
        1000.0,
        1.0,
    )
    assert shadow_metrics.infer_number_setpoint_limits("zones.x.foo", "targetTemp") == (
        4.0,
        42.0,
        0.5,
    )


def test_infer_binary_sensor_device_class() -> None:
    assert (
        shadow_metrics.infer_binary_sensor_device_class("zones.pump_fault")
        == BinarySensorDeviceClass.PROBLEM
    )
    assert (
        shadow_metrics.infer_binary_sensor_device_class("device.online")
        == BinarySensorDeviceClass.CONNECTIVITY
    )


def test_binary_extension_enabled_by_default_snake_case() -> None:
    assert shadow_metrics.binary_extension_enabled_by_default("tiles.leak_alarm")
    assert not shadow_metrics.binary_extension_enabled_by_default("x.rssi.signal")


def test_string_extension_enabled_by_default() -> None:
    assert shadow_metrics.string_extension_enabled_by_default(
        "cloud.rest.status.waterStatus"
    )
    assert shadow_metrics.string_extension_enabled_by_default("zone.status_text")


@patch.object(shadow_metrics, "_MAX_DEPTH", 1)
def test_flatten_respects_max_depth() -> None:
    out: dict[str, float | int] = {}
    shadow_metrics._flatten_numeric({"a": {"b": {"c": 1}}}, "r", out, 0)
    assert "r.a.b.c" not in out
