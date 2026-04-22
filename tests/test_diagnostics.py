"""Tests for ``custom_components.gecko.diagnostics`` helpers."""

from __future__ import annotations

from enum import Enum
from types import SimpleNamespace

from custom_components.gecko import diagnostics as gecko_diag


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


def test_get_connection_diagnostics_empty_manager() -> None:
    assert gecko_diag._get_connection_diagnostics(None) == {}


def test_get_connection_diagnostics_with_connection() -> None:
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

    mgr = SimpleNamespace(_connections={"m1": _Conn()})
    out = gecko_diag._get_connection_diagnostics(mgr)
    assert "m1" in out
    assert out["m1"]["gecko_client"]["client_id"] == "gc"


def test_get_vessel_coordinators_diagnostics_no_runtime() -> None:
    entry = SimpleNamespace(runtime_data=None)
    assert gecko_diag._get_vessel_coordinators_diagnostics(entry) == []


def test_get_vessel_coordinators_diagnostics_with_coordinator() -> None:
    coord = SimpleNamespace(
        vessel_id="v1",
        vessel_name="Test",
        monitor_id="m1",
        _has_initial_zones=True,
        _shadow_metric_values={"zones.waterlab.z1.ph": 7.0},
    )

    def get_all_zones():
        return {_ZoneType.FLOW: {}}

    coord.get_all_zones = get_all_zones  # type: ignore[method-assign]

    rd = SimpleNamespace(coordinators=[coord])
    entry = SimpleNamespace(runtime_data=rd)
    rows = gecko_diag._get_vessel_coordinators_diagnostics(entry)
    assert len(rows) == 1
    assert rows[0]["monitor_id"] == "m1"
    assert "zones.waterlab.z1.ph" in rows[0]["shadow_extension_metric_paths"]
