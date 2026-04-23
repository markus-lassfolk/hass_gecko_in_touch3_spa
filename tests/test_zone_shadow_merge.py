"""Tests for merging AWS IoT shadow reported + desired zone trees."""

from custom_components.gecko.zone_shadow_merge import (
    install_zone_parser_merge_patch,
    merge_shadow_zone_trees,
)
from gecko_iot_client.models.zone_parser import ZoneConfigurationParser
from gecko_iot_client.models.zone_types import ZoneType

install_zone_parser_merge_patch()


def test_merge_empty_reported_uses_desired() -> None:
    assert merge_shadow_zone_trees(
        {},
        {"temperatureControl": {"1": {"setPoint": 38.0}}},
    ) == {"temperatureControl": {"1": {"setPoint": 38.0}}}


def test_merge_desired_setpoint_over_stale_reported() -> None:
    merged = merge_shadow_zone_trees(
        {"temperatureControl": {"1": {"setPoint": 36.0, "temperature_": 35.0}}},
        {"temperatureControl": {"1": {"setPoint": 38.0}}},
    )
    assert merged["temperatureControl"]["1"]["setPoint"] == 38.0
    assert merged["temperatureControl"]["1"]["temperature_"] == 35.0


def test_merge_adds_zone_from_desired_only() -> None:
    merged = merge_shadow_zone_trees(
        {"temperatureControl": {"1": {"setPoint": 36.0}}},
        {"temperatureControl": {"2": {"setPoint": 40.0}}},
    )
    assert merged["temperatureControl"]["1"]["setPoint"] == 36.0
    assert merged["temperatureControl"]["2"]["setPoint"] == 40.0


def test_apply_state_to_zones_sees_merged_setpoint(monkeypatch) -> None:
    """Regression: parser must not drop desired setpoint when reported exists."""
    from gecko_iot_client.models.temperature_control_zone import TemperatureControlZone

    zone = TemperatureControlZone(
        "1",
        {
            "minTemperatureSetPointC": 10.0,
            "maxTemperatureSetPointC": 42.0,
        },
    )
    zones = {ZoneType.TEMPERATURE_CONTROL_ZONE: [zone]}
    state_data = {
        "state": {
            "reported": {
                "zones": {"temperatureControl": {"1": {"setPoint": 36.0}}},
            },
            "desired": {
                "zones": {"temperatureControl": {"1": {"setPoint": 38.5}}},
            },
        }
    }
    parser = ZoneConfigurationParser()
    parser.apply_state_to_zones(zones, state_data)
    assert zone.set_point == 38.5
