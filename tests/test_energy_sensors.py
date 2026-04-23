"""Tests for premium energy sensor payload parsing."""

from unittest.mock import MagicMock

from custom_components.gecko.energy_parse import (
    _coerce_energy_consumption_kwh,
    _coerce_energy_cost_amount,
    _coerce_energy_score_value,
    _first_valid_float,
    _safe_float,
    premium_energy_poll_has_usable_values,
)
from custom_components.gecko.sensor import (
    GeckoEnergyConsumptionSensor,
    GeckoEnergyCostSensor,
    GeckoEnergyScoreSensor,
)
from homeassistant.components.sensor.const import DEVICE_CLASS_STATE_CLASSES


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


def test_coerce_energy_consumption_nested_and_kwh_key_scan() -> None:
    assert (
        _coerce_energy_consumption_kwh({"data": {"values": {"cumulativeKwh": 7.0}}})
        == 7.0
    )
    assert (
        _coerce_energy_consumption_kwh({"spaMetrics": {"lifetimeEnergyKwh": 99.1}})
        == 99.1
    )
    assert _coerce_energy_consumption_kwh("12.25") == 12.25
    assert _coerce_energy_consumption_kwh({"aggregates": {"totalKwh": 3.5}}) == 3.5
    assert _coerce_energy_consumption_kwh({"data": {"consumption": 2.0}}) == 2.0
    assert _coerce_energy_consumption_kwh({"energy": {"totalKwh": 9.0}}) == 9.0


def test_premium_energy_poll_requires_parseable_values() -> None:
    """Coordinator must not treat opaque JSON blobs as a successful energy poll."""
    assert not premium_energy_poll_has_usable_values(
        {"consumption": {"foo": 1}, "score": None, "cost": None}
    )
    assert premium_energy_poll_has_usable_values(
        {"consumption": {"totalKwh": 1.0}, "score": {}, "cost": None}
    )


def test_coerce_gecko_app_energy_wh_and_wrapped_cost() -> None:
    """Live Gecko API uses Wh fields and nests monetary amount under ``energyCost``."""
    assert _coerce_energy_consumption_kwh({"energyConsumptionWh": 12500}) == 12.5
    assert _coerce_energy_consumption_kwh({"worstCaseConsumptionWh": 1000}) == 1.0
    assert (
        _coerce_energy_cost_amount({"energyCost": {"amount": 42.5, "currency": "SEK"}})
        == 42.5
    )
    assert _coerce_energy_cost_amount({"energyCost": 9.99}) == 9.99


def test_coerce_energy_prefers_total_wh_over_period_wh() -> None:
    """Period ``energyConsumptionWh`` is often 0; cumulative total must win."""
    raw = {
        "energyConsumptionWh": 0,
        "totalEnergyConsumptionWh": 500_000,
        "period": "month",
    }
    assert _coerce_energy_consumption_kwh(raw) == 500.0


def test_coerce_energy_cost_total_and_formatted_variants() -> None:
    """Gecko often omits ``amount``; use ``total`` / formatted strings / deep scan."""
    assert _coerce_energy_cost_amount(
        {"energyCost": {"currency": "SEK", "total": 19.95}}
    ) == 19.95
    assert _coerce_energy_cost_amount(
        {"energyCost": {"formattedEnergyCost": "12,50 kr"}}
    ) == 12.5
    assert _coerce_energy_cost_amount({"formattedEnergyCost": "3.25 SEK"}) == 3.25
    assert (
        _coerce_energy_cost_amount(
            {"energyCost": {"currency": "SEK", "lineItems": [{"netPrice": 7.5}]}}
        )
        == 7.5
    )


def test_coerce_gecko_app_score_scalar_and_object() -> None:
    assert _coerce_energy_score_value({"score": 77, "period": "month"}) == 77.0
    assert _coerce_energy_score_value({"score": {"value": 81.0}}) == 81.0


def test_energy_sensors_device_class_state_class_allowed_by_ha_matrix() -> None:
    """HA core warns (``sensor`` platform) when ``state_class`` ∉ ``DEVICE_CLASS_STATE_CLASSES[dc]``.

    Unit tests that only assert ``native_value`` never hit that path; this locks
    metadata against Home Assistant's published allowlist.
    """
    entry = MagicMock()
    entry.entry_id = "e1"

    consumption_coord = MagicMock()
    consumption_coord.get_energy_data = MagicMock(return_value={"consumption": 1.0})
    consumption_coord.vessel_id = "v1"
    consumption = GeckoEnergyConsumptionSensor(consumption_coord, entry)
    dc_c, sc_c = consumption._attr_device_class, consumption._attr_state_class
    assert dc_c is not None and sc_c is not None
    assert sc_c in DEVICE_CLASS_STATE_CLASSES[dc_c], (dc_c, sc_c)

    cost_coord = MagicMock()
    cost_coord.get_energy_data = MagicMock(return_value={"cost": 2.5})
    cost_coord.vessel_id = "v1"
    cost = GeckoEnergyCostSensor(cost_coord, entry)
    dc_m, sc_m = cost._attr_device_class, cost._attr_state_class
    assert dc_m is not None and sc_m is not None
    assert sc_m in DEVICE_CLASS_STATE_CLASSES[dc_m], (dc_m, sc_m)


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
