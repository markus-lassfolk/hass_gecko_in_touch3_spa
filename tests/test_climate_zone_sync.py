"""Climate entity must track the live zone object after each MQTT snapshot."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from custom_components.gecko.climate import GeckoClimate
from gecko_iot_client.models.zone_types import ZoneType


@pytest.fixture
def mock_hass():
    return MagicMock()


def test_gecko_climate_rebinds_zone_when_coordinator_replaces_models(mock_hass):
    """Coordinator assigns a new zone dict; climate must not keep a stale reference."""
    old_zone = SimpleNamespace(
        id=1,
        min_temperature_set_point_c=10.0,
        max_temperature_set_point_c=42.0,
        temperature=29.0,
        target_temperature=30.0,
        status=None,
        mode=None,
    )
    new_zone = SimpleNamespace(
        id=1,
        min_temperature_set_point_c=10.0,
        max_temperature_set_point_c=42.0,
        temperature=29.0,
        target_temperature=38.0,
        status=None,
        mode=None,
        set_target_temperature=MagicMock(),
    )

    snapshot: dict[str, list] = {"zones": [old_zone]}

    coordinator = SimpleNamespace(
        hass=mock_hass,
        entry_id="ent1",
        vessel_id="v1",
        get_zones_by_type=lambda zt: (
            snapshot["zones"] if zt is ZoneType.TEMPERATURE_CONTROL_ZONE else []
        ),
    )

    ent = GeckoClimate(coordinator, old_zone)
    assert ent._attr_target_temperature == 30.0
    assert ent._zone is old_zone

    snapshot["zones"] = [new_zone]
    ent._update_from_zone()
    assert ent._zone is new_zone
    assert ent._attr_target_temperature == 38.0


@pytest.mark.asyncio
async def test_async_set_temperature_calls_zone_setter_on_event_loop() -> None:
    """Thermostat must call ``set_target_temperature`` on the HA loop (not executor)."""
    hass_stub = MagicMock()

    publish = MagicMock()
    transporter = MagicMock()
    transporter.publish_desired_state = publish
    gecko_client = MagicMock()
    gecko_client.transporter = transporter

    conn = MagicMock()
    conn.is_connected = True
    conn.gecko_client = gecko_client

    mgr = MagicMock()
    mgr.get_connection = MagicMock(return_value=conn)

    set_target_temperature = MagicMock()
    zone = SimpleNamespace(
        id=1,
        min_temperature_set_point_c=15.0,
        max_temperature_set_point_c=40.0,
        temperature=29.0,
        target_temperature=29.0,
        status=None,
        mode=None,
        set_target_temperature=set_target_temperature,
    )

    coordinator = SimpleNamespace(
        hass=hass_stub,
        entry_id="ent1",
        vessel_id="v1",
        vessel_name="Spa",
        monitor_id="m1",
        get_zones_by_type=lambda zt: (
            [zone] if zt is ZoneType.TEMPERATURE_CONTROL_ZONE else []
        ),
    )
    ent = GeckoClimate(coordinator, zone)
    ent.hass = hass_stub
    ent.entity_id = "climate.test"

    with patch(
        "custom_components.gecko.climate.async_get_connection_manager",
        new=AsyncMock(return_value=mgr),
    ):
        await ent.async_set_temperature(temperature=31.5)

    set_target_temperature.assert_called_once_with(31.5)
    publish.assert_not_called()
    assert ent._attr_target_temperature == 31.5
