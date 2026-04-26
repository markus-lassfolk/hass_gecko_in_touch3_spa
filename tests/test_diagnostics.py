"""Tests for ``custom_components.gecko.diagnostics`` helpers."""

from __future__ import annotations

from enum import Enum
from types import SimpleNamespace

from custom_components.gecko import diagnostics as gecko_diag
from gecko_iot_client.models.zone_types import ZoneType


class _ZoneType(Enum):
    FLOW = "flow"


def test_get_gecko_client_info_minimal() -> None:
    client = SimpleNamespace(
        id="c1",
        is_connected=True,
        _configuration=None,
        _state=None,
        connectivity_status=None,
        operation_mode_controller=None,
        _zones={},
        transporter=None,
    )
    info = gecko_diag._get_gecko_client_info(client)
    assert info["client_id"] == "c1"
    assert info["is_connected"] is True


def test_get_gecko_client_info_shadow_topology() -> None:
    client = SimpleNamespace(
        id="c1",
        is_connected=False,
        _configuration={"zones": {"flow": {}}},
        _state={"state": {"reported": {"zones": {}}}},
        connectivity_status=None,
        operation_mode_controller=None,
        _zones={},
        transporter=None,
    )
    info = gecko_diag._get_gecko_client_info(client)
    assert "shadow_topology" in info
    assert info["configuration_zones_keys"] == ["flow"]


async def test_get_connection_diagnostics_empty_manager() -> None:
    assert await gecko_diag._get_connection_diagnostics(None) == {}


async def test_get_connection_diagnostics_with_connection() -> None:
    class _Conn:
        vessel_name = "Spa"
        is_connected = True
        update_callbacks = []
        connectivity_status = None
        gecko_client = SimpleNamespace(
            id="gc",
            is_connected=True,
            _configuration=None,
            _state=None,
            connectivity_status=None,
            operation_mode_controller=None,
            _zones={},
            transporter=None,
        )

    connections = {"m1": _Conn()}
    mgr = SimpleNamespace(
        get_connections_snapshot=lambda: dict(connections),
    )
    out = await gecko_diag._get_connection_diagnostics(mgr)
    assert "m1" in out
    assert out["m1"]["gecko_client"]["client_id"] == "gc"


def test_get_vessel_coordinators_diagnostics_no_runtime() -> None:
    entry = SimpleNamespace(runtime_data=None)
    assert gecko_diag._get_vessel_coordinators_diagnostics(entry) == []


def test_temperature_control_zones_summary() -> None:
    zone = SimpleNamespace(
        id=1,
        temperature=36.5,
        target_temperature=38.0,
        min_temperature_set_point_c=10.0,
        max_temperature_set_point_c=40.0,
    )
    coord = SimpleNamespace(
        get_zones_by_type=lambda zt: (
            [zone] if zt is ZoneType.TEMPERATURE_CONTROL_ZONE else []
        ),
    )
    rows = gecko_diag._temperature_control_zones_summary(coord)
    assert rows == [
        {
            "zone_id": 1,
            "current_temperature_c": 36.5,
            "target_temperature_c": 38.0,
            "min_setpoint_c": 10.0,
            "max_setpoint_c": 40.0,
        }
    ]


def test_get_vessel_coordinators_diagnostics_with_coordinator() -> None:
    zone = SimpleNamespace(
        id=2,
        temperature=35.0,
        target_temperature=37.5,
        min_temperature_set_point_c=15.0,
        max_temperature_set_point_c=40.0,
    )
    coord = SimpleNamespace(
        vessel_id="v1",
        vessel_name="Test",
        monitor_id="m1",
        _has_initial_zones=True,
        _shadow_metric_values={"zones.waterlab.z1.ph": 7.0},
        _cloud_tile_metrics={"cloud.rest.readings.ph": 7.85},
        _cloud_string_metrics={"cloud.rest.readings.ph.status": "high"},
        _cloud_bool_metrics={},
        _last_cloud_poll_monotonic=12345.0,
        get_zones_by_type=lambda zt: (
            [zone] if zt is ZoneType.TEMPERATURE_CONTROL_ZONE else []
        ),
    )

    def get_all_zones():
        return {_ZoneType.FLOW: {}}

    coord.get_all_zones = get_all_zones  # type: ignore[method-assign]

    rd = SimpleNamespace(coordinators=[coord])
    entry = SimpleNamespace(
        runtime_data=rd,
        data={
            "vessels": [
                {
                    "vesselId": "v1",
                    "monitorId": "m1",
                    "spa_configuration": {
                        "accessories": {
                            "pumps": {"9": {}},
                            "lights": {},
                            "waterfalls": {},
                            "blowers": {},
                        },
                        "zones": {"flow": {"7": {"pumps": [9]}}},
                    },
                },
            ],
        },
    )
    rows = gecko_diag._get_vessel_coordinators_diagnostics(entry)
    assert len(rows) == 1
    assert rows[0]["monitor_id"] == "m1"
    assert "zones.waterlab.z1.ph" in rows[0]["shadow_extension_metric_paths"]
    assert rows[0]["temperature_control_zones"] == [
        {
            "zone_id": 2,
            "current_temperature_c": 35.0,
            "target_temperature_c": 37.5,
            "min_setpoint_c": 15.0,
            "max_setpoint_c": 40.0,
        }
    ]
    summ = rows[0]["spa_configuration_summary"]
    assert summ["present"] is True
    assert summ["pump_id_to_flow_zone_id"] == {"9": "7"}


def test_spa_configuration_summary_disambiguates_monitor_id() -> None:
    """When two vessels share vesselId, diagnostics pick spa_configuration by monitorId."""
    coord = SimpleNamespace(
        vessel_id="vx",
        vessel_name="Dup",
        monitor_id="mon-b",
        _has_initial_zones=True,
        _shadow_metric_values={},
        _cloud_tile_metrics={},
        _cloud_string_metrics={},
        _cloud_bool_metrics={},
        _last_cloud_poll_monotonic=None,
        get_zones_by_type=lambda zt: [],
        get_all_zones=lambda: {_ZoneType.FLOW: {}},
    )
    rd = SimpleNamespace(coordinators=[coord])
    entry = SimpleNamespace(
        runtime_data=rd,
        data={
            "vessels": [
                {
                    "vesselId": "vx",
                    "monitorId": "mon-a",
                    "spa_configuration": {
                        "accessories": {
                            "pumps": {"1": {}},
                            "lights": {},
                            "waterfalls": {},
                            "blowers": {},
                        },
                        "zones": {"flow": {"1": {"pumps": [1]}}},
                    },
                },
                {
                    "vesselId": "vx",
                    "monitorId": "mon-b",
                    "spa_configuration": {
                        "accessories": {
                            "pumps": {"2": {}},
                            "lights": {},
                            "waterfalls": {},
                            "blowers": {},
                        },
                        "zones": {"flow": {"2": {"pumps": [2]}}},
                    },
                },
            ],
        },
    )
    rows = gecko_diag._get_vessel_coordinators_diagnostics(entry)
    assert len(rows) == 1
    assert rows[0]["spa_configuration_summary"]["pump_id_to_flow_zone_id"] == {"2": "2"}


def test_get_gecko_client_info_handles_exception() -> None:
    class _BadClient:
        @property
        def id(self):
            raise RuntimeError("boom")

    info = gecko_diag._get_gecko_client_info(_BadClient())
    assert "error" in info
    assert info["error"] == "RuntimeError"
