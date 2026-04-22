"""Tests for spa thermostat mirror sensors (``sensor`` domain for dashboard cards)."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.gecko.sensor import GeckoSpaTemperatureSensor
from gecko_iot_client.models.zone_types import ZoneType


def test_gecko_spa_temperature_sensor_reads_zone_target_and_current() -> None:
    zone = SimpleNamespace(id=1, temperature=36.0, target_temperature=38.5)
    coordinator = SimpleNamespace(
        entry_id="e1",
        vessel_id="v1",
        get_zones_by_type=lambda zt: (
            [zone] if zt is ZoneType.TEMPERATURE_CONTROL_ZONE else []
        ),
    )
    entry = SimpleNamespace(entry_id="ent1")

    target_ent = GeckoSpaTemperatureSensor(coordinator, entry, 1, "target")
    assert target_ent._attr_native_value == 38.5
    assert target_ent._attr_unique_id.endswith("spa_target_temperature_1")

    current_ent = GeckoSpaTemperatureSensor(coordinator, entry, 1, "current")
    assert current_ent._attr_native_value == 36.0
    assert current_ent._attr_unique_id.endswith("spa_current_temperature_1")


def test_gecko_spa_temperature_sensor_unknown_zone() -> None:
    coordinator = SimpleNamespace(
        entry_id="e1",
        vessel_id="v1",
        get_zones_by_type=lambda _zt: [],
    )
    entry = SimpleNamespace(entry_id="ent1")
    ent = GeckoSpaTemperatureSensor(coordinator, entry, 99, "target")
    assert ent._attr_native_value is None
