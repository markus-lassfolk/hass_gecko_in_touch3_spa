"""Tests for merging AWS IoT shadow reported + desired zone trees."""

from custom_components.gecko.zone_shadow_merge import (
    _normalize_merged_zone_runtime_state,
    enrich_document_current_state_with_previous_desired,
    install_mqtt_shadow_document_patch,
    install_zone_parser_merge_patch,
    merge_shadow_zone_trees,
)
from gecko_iot_client.models.flow_zone import FlowZone
from gecko_iot_client.models.zone_parser import ZoneConfigurationParser
from gecko_iot_client.models.zone_types import ZoneType

install_zone_parser_merge_patch()
install_mqtt_shadow_document_patch()


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


def test_apply_state_delta_overrides_desired_and_reported() -> None:
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
                "zones": {"temperatureControl": {"1": {"setPoint": 37.0}}},
            },
            "delta": {
                "zones": {"temperatureControl": {"1": {"setPoint": 38.0}}},
            },
        }
    }
    parser = ZoneConfigurationParser()
    parser.apply_state_to_zones(zones, state_data)
    assert zone.set_point == 38.0


def test_enrich_document_carries_previous_desired_when_current_omits_it() -> None:
    current = {
        "reported": {
            "zones": {"temperatureControl": {"1": {"setPoint": 33.0}}},
        },
    }
    previous = {
        "desired": {
            "zones": {"temperatureControl": {"1": {"setPoint": 34.0}}},
        },
    }
    out = enrich_document_current_state_with_previous_desired(current, previous)
    assert out["desired"]["zones"]["temperatureControl"]["1"]["setPoint"] == 34.0


def test_enrich_document_empty_current_desired_dict_still_merges_zones() -> None:
    current = {
        "reported": {
            "zones": {"temperatureControl": {"1": {"setPoint": 33.0}}},
        },
        "desired": {},
    }
    previous = {
        "desired": {
            "zones": {"temperatureControl": {"1": {"setPoint": 34.5}}},
        },
    }
    out = enrich_document_current_state_with_previous_desired(current, previous)
    assert out["desired"]["zones"]["temperatureControl"]["1"]["setPoint"] == 34.5


def test_normalize_flow_runtime_aligns_active_and_isactive() -> None:
    """Merged shadows may carry both keys; last writer must not flip ``active`` off."""
    merged = _normalize_merged_zone_runtime_state(
        "flow", {"active": True, "isActive": False}
    )
    assert merged["active"] is True and merged["isActive"] is True
    merged_off = _normalize_merged_zone_runtime_state(
        "flow", {"active": False, "isActive": True}
    )
    assert merged_off["active"] is False and merged_off["isActive"] is False


def test_apply_state_flow_zone_string_id_matches_int_zone_id() -> None:
    """Shadow zone ids are often JSON strings while library zones may use int ids."""
    zone = FlowZone(1, {"name": "Pump 1"})
    zone._publish_desired_state = lambda *_a, **_k: None  # type: ignore[assignment]
    zones = {ZoneType.FLOW_ZONE: [zone]}
    state_data = {
        "state": {
            "reported": {
                "zones": {
                    "flow": {"1": {"active": True, "isActive": False}},
                },
            },
        }
    }
    parser = ZoneConfigurationParser()
    parser.apply_state_to_zones(zones, state_data)
    assert zone.active is True


def test_apply_state_flow_keeps_pump_on_when_reported_keys_conflict() -> None:
    """``isActive`` maps onto ``active``; conflicting booleans must be reconciled first."""
    zone = FlowZone("1", {"name": "Pump 1"})
    zone._publish_desired_state = lambda *_a, **_k: None  # type: ignore[assignment]
    zones = {ZoneType.FLOW_ZONE: [zone]}
    state_data = {
        "state": {
            "reported": {
                "zones": {"flow": {"1": {"active": True, "isActive": False}}},
            },
        }
    }
    parser = ZoneConfigurationParser()
    parser.apply_state_to_zones(zones, state_data)
    assert zone.active is True
