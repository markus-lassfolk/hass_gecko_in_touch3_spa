"""Tests for ``custom_components.gecko.shadow_metrics`` heuristics."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from custom_components.gecko import shadow_metrics
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
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
    assert shadow_metrics._string_value_ok("x" * 255)
    assert not shadow_metrics._string_value_ok("")
    assert not shadow_metrics._string_value_ok("eyJxxx")
    assert not shadow_metrics._string_value_ok("x" * 256)
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
    assert ent._attr_state_class == SensorStateClass.MEASUREMENT
    assert ent._attr_suggested_display_precision == 2


def test_apply_numeric_shadow_sensor_hints_lsi_gets_measurement_state_class() -> None:
    """LSI has no device_class but must still be a statistical numeric (line graph)."""
    ent = SimpleNamespace()
    shadow_metrics.apply_numeric_shadow_sensor_hints(ent, "cloud.rest.readings.lsi")
    assert ent._attr_device_class is None
    assert ent._attr_state_class == SensorStateClass.MEASUREMENT
    assert ent._attr_suggested_display_precision == 2


def test_chemistry_metric_enabled_by_default() -> None:
    assert shadow_metrics.chemistry_metric_enabled_by_default("zones.x.phValue")
    assert not shadow_metrics.chemistry_metric_enabled_by_default(
        "features.waterlab.sensor.ph.offsetMv"
    )
    assert not shadow_metrics.chemistry_metric_enabled_by_default(
        "cloud.rest.summary.ph"
    )


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
    assert not shadow_metrics.string_extension_enabled_by_default(
        "cloud.rest.status.waterStatus"
    )
    assert shadow_metrics.string_extension_enabled_by_default("zone.status_text")


@patch.object(shadow_metrics, "_MAX_DEPTH", 1)
def test_flatten_respects_max_depth() -> None:
    out: dict[str, float | int] = {}
    shadow_metrics._flatten_numeric({"a": {"b": {"c": 1}}}, "r", out, 0)
    assert "r.a.b.c" not in out


def test_infer_sensor_metadata_humidity_pressure_energy_power_flow() -> None:
    assert shadow_metrics.infer_sensor_metadata("zones.spa.humidity.value") == (
        SensorDeviceClass.HUMIDITY,
        "%",
    )
    assert shadow_metrics.infer_sensor_metadata("zones.line.pressure.psi") == (
        SensorDeviceClass.PRESSURE,
        "psi",
    )
    assert shadow_metrics.infer_sensor_metadata("zones.meter.kwh") == (
        SensorDeviceClass.ENERGY,
        "kWh",
    )
    assert shadow_metrics.infer_sensor_metadata("zones.pump.power.reading") == (
        SensorDeviceClass.POWER,
        "W",
    )
    dc, unit = shadow_metrics.infer_sensor_metadata("zones.flow.reading")
    vfr = getattr(SensorDeviceClass, "VOLUME_FLOW_RATE", None)
    assert dc == vfr
    assert unit == "L/min"


def test_is_connectivity_shadow_metric_nested() -> None:
    assert shadow_metrics._is_connectivity_shadow_metric_path(
        "features.connectivity.rssi"
    )


def test_is_rf_diagnostic_waterlab_rf() -> None:
    assert shadow_metrics._is_rf_diagnostic_path("features.waterlab.rf.link")


def test_extract_extension_booleans_skips_sensitive_feature_base() -> None:
    state = {
        "state": {
            "reported": {
                "features": {
                    "password_vault": {"leak": True},
                    "okfeat": {"flag": False},
                }
            }
        }
    }
    b = shadow_metrics.extract_extension_booleans(state)
    assert b == {"features.okfeat.flag": False}


def test_iter_extension_bases_order() -> None:
    state = {"state": {"reported": {"zones": {"x": {"z": {}}}, "features": {"f": {}}}}}
    bases = shadow_metrics._iter_extension_bases(state)
    prefixes = {p for p, _ in bases}
    assert any(p.startswith("zones.") for p in prefixes)
    assert any(p.startswith("features.") for p in prefixes)


def test_infer_binary_sensor_heat_cold_lock() -> None:
    assert (
        shadow_metrics.infer_binary_sensor_device_class("zones.heat.mode_on")
        == BinarySensorDeviceClass.HEAT
    )
    assert (
        shadow_metrics.infer_binary_sensor_device_class("cooling_valve_open")
        == BinarySensorDeviceClass.COLD
    )
    assert (
        shadow_metrics.infer_binary_sensor_device_class("door.lock_state")
        == BinarySensorDeviceClass.LOCK
    )


def test_string_extension_cloud_rest_mode_token() -> None:
    assert not shadow_metrics.string_extension_enabled_by_default(
        "cloud.rest.status.mode_tile"
    )


def test_metric_path_to_entity_slug_empty_path() -> None:
    assert shadow_metrics.metric_path_to_entity_slug("...") == "metric"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("features.waterlab.sensor.ph.offsetMv", "Waterlab pH Offset mV"),
        ("features.waterlab.sensor.ph.slopeMvPerPh", "Waterlab pH Slope mV/pH"),
        ("features.waterlab.sensor.ph.offsetMvAtPh7", "Waterlab pH Offset mV at pH 7"),
        ("features.waterlab.sensor.therm.R0", "Waterlab Thermistor R₀"),
        ("features.waterlab.sensor.therm.T0", "Waterlab Thermistor T₀"),
        ("features.waterlab.sensor.therm.beta", "Waterlab Thermistor Beta"),
        ("connectivity.vesselStatus", "Vessel Status"),
        ("connectivity.gatewayStatus", "Gateway Status"),
        ("connectivity.channel", "Conn. Channel"),
        ("connectivity.id", "Conn. ID"),
        ("connectivity.strength", "Conn. Signal Strength"),
        ("features.operationMode", "Operation Mode"),
        ("cloud.rest.temperature", "Temperature"),
        ("zones.waterlab.z1.reading", "Waterlab Z1 Reading"),
        ("features.extra.n", "Extra N"),
        ("cloud.rest.readings.ph", "pH"),
        ("cloud.rest.readings.waterTemp", "Water Temp"),
        ("cloud.rest.summary.ph", "Tile copy pH"),
        ("cloud.rest.readings.totalAlkalinity", "Total Alkalinity"),
        ("cloud.rest.readings.lsi", "LSI"),
        ("cloud.rest.readings.wifiRssi", "WiFi RSSI"),
        ("cloud.rest.actions.count", "Action Count"),
        ("cloud.rest.actions.lower_ph", "Action Lower pH"),
        ("cloud.rest.actions.lower_ph.instructions", "Action Lower pH Instructions"),
        ("cloud.rest.actions.raise_orp_chlorine", "Action Raise ORP Chlorine"),
        ("cloud.rest.disc.text", "Status Text"),
        ("cloud.rest.disc.waterStatusColor", "Status Water Status Color"),
        ("cloud.rest.disc.lastUpdatedText", "Status Last Updated Text"),
        ("cloud.rest.disc_elements.temp_c", "Status Temp C"),
    ],
)
def test_humanize_shadow_path(path: str, expected: str) -> None:
    assert shadow_metrics.humanize_shadow_path(path) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("cloud.rest.readings.ph", True),
        ("cloud.rest.readings.orp", True),
        ("cloud.rest.readings.waterTemp", True),
        ("cloud.rest.readings.totalAlkalinity", True),
        ("cloud.rest.readings.freeChlorine", True),
        ("cloud.rest.readings.lsi", True),
        ("cloud.rest.readings.calciumHardness", True),
        ("cloud.rest.readings.cyanuricAcid", True),
        ("cloud.rest.readings.totalChlorine", True),
        ("cloud.rest.readings.adjustedTotalAlkalinity", True),
        ("cloud.rest.readings.totalHardness", True),
        ("cloud.rest.readings.phStc20", True),
        ("cloud.rest.readings.tds", True),
        ("cloud.rest.readings.salinity", True),
        ("cloud.rest.readings.wifiRssi", False),
        ("cloud.rest.summary.ph", False),
        ("cloud.rest.summary.orp_mv", False),
        ("cloud.rest.actions.count", False),
        ("cloud.rest.disc_elements.temp_c", False),
    ],
)
def test_chemistry_metric_enabled_readings(path: str, expected: bool) -> None:
    assert shadow_metrics.chemistry_metric_enabled_by_default(path) is expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("cloud.rest.actions.lower_ph", False),
        ("cloud.rest.actions.raise_orp_chlorine", False),
        ("cloud.rest.actions.lower_ph.instructions", False),
        ("cloud.rest.actions.raise_orp_chlorine.instructions", False),
        ("cloud.rest.disc.text", False),
        ("cloud.rest.disc.waterStatusColor", False),
        ("cloud.rest.disc.lastUpdatedText", False),
        ("cloud.rest.readings.waterTemp.status", True),
        ("cloud.rest.readings.waterTemp.title", False),
        ("cloud.rest.readings.orp.status", True),
        ("cloud.rest.readings.freechlorine.status", True),
        ("cloud.rest.readings.wifiRssi.status", False),
        ("cloud.rest.readings.totalChlorine.status", True),
        ("cloud.rest.readings.adjustedTotalAlkalinity.status", True),
        ("cloud.rest.readings.calciumHardness.status", True),
        ("cloud.rest.readings.lsi.status", True),
        ("cloud.rest.readings.phStc20.status", True),
        ("cloud.rest.readings.ph.status", True),
    ],
)
def test_string_enabled_by_default_actions(path: str, expected: bool) -> None:
    assert shadow_metrics.string_extension_enabled_by_default(path) is expected


@pytest.mark.parametrize(
    ("path", "dc", "unit"),
    [
        ("cloud.rest.readings.ph", "ph", None),
        ("cloud.rest.readings.orp", "voltage", "mV"),
        ("cloud.rest.readings.waterTemp", "temperature", "°C"),
        ("cloud.rest.readings.totalAlkalinity", None, "ppm"),
        ("cloud.rest.readings.freeChlorine", None, "ppm"),
        ("cloud.rest.readings.lsi", None, None),
        ("cloud.rest.readings.wifiRssi", "signal_strength", "dB"),
        ("cloud.rest.readings.phSTC20", "ph", None),
        ("cloud.rest.readings.calciumHardness", None, "ppm"),
        ("cloud.rest.readings.adjustedTotalAlkalinity", None, "ppm"),
        ("cloud.rest.readings.totalChlorine", None, "ppm"),
        ("cloud.rest.readings.totalHardness", None, "ppm"),
        ("cloud.rest.readings.cyanuricAcid", None, "ppm"),
        ("cloud.rest.disc_elements.temp_c", "temperature", "°C"),
    ],
)
def test_infer_sensor_metadata_readings(path, dc, unit) -> None:
    result_dc, result_unit = shadow_metrics.infer_sensor_metadata(path)
    if dc is None:
        assert result_dc is None
    else:
        assert result_dc is not None and result_dc.value == dc
    assert result_unit == unit


def test_infer_sensor_metadata_rssi_and_rf_strength() -> None:
    """WiFi RSSI and RF signal strength paths get SIGNAL_STRENGTH device class."""
    dc_rssi, u_rssi = shadow_metrics.infer_sensor_metadata(
        "cloud.rest.readings.wifiRssi"
    )
    assert dc_rssi is not None and dc_rssi.value == "signal_strength"
    assert u_rssi == "dB"

    dc_rf, u_rf = shadow_metrics.infer_sensor_metadata("features.rf.strength_")
    assert dc_rf is not None and dc_rf.value == "signal_strength"
    assert u_rf == "dB"
