"""Tests for premium energy sensor payload parsing."""

from unittest.mock import MagicMock

from custom_components.gecko.sensor import (
    GeckoEnergyScoreSensor,
    _coerce_energy_consumption_kwh,
    _first_valid_float,
    _safe_float,
)


def test_first_valid_float_preserves_zero() -> None:
    """Or-chaining would skip 0.0; first-valid must not."""
    raw = {"totalKwh": 0.0}
    assert _first_valid_float(raw, ("totalKwh",), ("value",)) == 0.0
    assert _first_valid_float({"value": 0.0}, ("missing",), ("value",)) == 0.0
    assert _first_valid_float({}, ("a",)) is None


def test_first_valid_float_prefers_first_non_none() -> None:
    raw = {"a": 1.0, "b": 2.5}
    assert _first_valid_float(raw, ("a",), ("b",)) == 1.0


def test_safe_float_nested() -> None:
    data = {"a": {"b": 3.14}}
    assert _safe_float(data, "a", "b") == 3.14
    assert _safe_float(data, "a", "missing") is None


def test_energy_score_scalar_payload_has_no_default_percent_unit() -> None:
    """Plain numeric score payloads must not assume a percentage unit."""
    coordinator = MagicMock()
    coordinator.get_energy_data = MagicMock(return_value={"score": 7.5})
    coordinator.vessel_id = "v1"
    entry = MagicMock()
    entry.entry_id = "e1"
    sensor = GeckoEnergyScoreSensor(coordinator, entry)
    assert sensor.native_value == 7.5
    assert sensor.native_unit_of_measurement is None


def test_coerce_energy_consumption_unwraps_data_and_extra_keys() -> None:
    assert _coerce_energy_consumption_kwh({"data": {"totalKwh": 42.25}}) == 42.25
    assert _coerce_energy_consumption_kwh({"totalEnergyKWh": 10.0}) == 10.0
    assert _coerce_energy_consumption_kwh(3.5) == 3.5


def test_energy_score_dict_unit_is_preserved() -> None:
    coordinator = MagicMock()
    coordinator.get_energy_data = MagicMock(
        return_value={"score": {"value": 82.0, "unit": "%"}}
    )
    coordinator.vessel_id = "v1"
    entry = MagicMock()
    entry.entry_id = "e1"
    sensor = GeckoEnergyScoreSensor(coordinator, entry)
    assert sensor.native_value == 82.0
    assert sensor.native_unit_of_measurement == "%"
