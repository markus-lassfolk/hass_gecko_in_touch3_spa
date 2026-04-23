"""Pump fan on/off vs multi-speed behaviour."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from custom_components.gecko.fan import GeckoFan
from gecko_iot_client.models.flow_zone import FlowZoneType
from homeassistant.components.fan import FanEntityFeature


@pytest.mark.asyncio
async def test_fan_turn_on_binary_pump_activates_not_set_speed() -> None:
    """On/off pumps must not receive generic set_speed values from async_turn_on."""
    activate = MagicMock()
    zone = SimpleNamespace(
        name="Pump",
        type=FlowZoneType.FLOW_ZONE,
        id=1,
        active=False,
        speed=0.0,
        speed_config=None,
        initiators=[],
        activate=activate,
        deactivate=MagicMock(),
    )
    coordinator = MagicMock()
    coordinator.vessel_id = "v1"
    coordinator.get_zones_by_type = MagicMock(return_value=[zone])
    coordinator.get_gecko_client = AsyncMock(return_value=MagicMock())
    entry = MagicMock()
    entry.entry_id = "e1"

    fan = GeckoFan(coordinator, entry, zone)
    assert not (fan.supported_features & FanEntityFeature.SET_SPEED)

    await fan.async_turn_on()
    activate.assert_called_once()
    coordinator.get_gecko_client.assert_awaited_once()


@pytest.mark.asyncio
async def test_fan_turn_on_speed_pump_uses_async_set_speed() -> None:
    """Multi-speed pumps continue to map percentage through async_set_speed."""
    zone = SimpleNamespace(
        name="Pump",
        type=FlowZoneType.FLOW_ZONE,
        id=2,
        active=False,
        speed=1.0,
        speed_config={"minimum": 1.0, "maximum": 3.0, "stepIncrement": 1.0},
        initiators=[],
        activate=MagicMock(),
        deactivate=MagicMock(),
    )
    coordinator = MagicMock()
    coordinator.vessel_id = "v1"
    coordinator.get_zones_by_type = MagicMock(return_value=[zone])
    coordinator.get_gecko_client = AsyncMock(return_value=MagicMock())
    entry = MagicMock()
    entry.entry_id = "e1"

    fan = GeckoFan(coordinator, entry, zone)
    assert fan.supported_features & FanEntityFeature.SET_SPEED
    fan.async_set_speed = AsyncMock()
    await fan.async_turn_on(percentage=50)
    fan.async_set_speed.assert_awaited_once()
