"""Tests for ``GeckoConnectionManager`` using the HA test ``hass`` fixture."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from custom_components.gecko.connection_manager import (
    GeckoConnectionManager,
    GeckoMonitorConnection,
    async_get_connection_manager,
)
from homeassistant.core import HomeAssistant


@pytest.fixture
async def gecko_manager(hass: HomeAssistant) -> GeckoConnectionManager:
    """Fresh manager bound to the test ``hass`` (no MQTT)."""
    return GeckoConnectionManager(hass)


async def test_canonical_monitor_id_strips(
    hass: HomeAssistant, gecko_manager: GeckoConnectionManager
) -> None:
    assert gecko_manager._canonical_monitor_id("  mid  ") == "mid"


async def test_resolved_connection_key_string_and_int(
    hass: HomeAssistant, gecko_manager: GeckoConnectionManager
) -> None:
    conn = GeckoMonitorConnection(
        monitor_id="42",
        gecko_client=None,
        websocket_url="wss://x",
        vessel_name="Spa",
    )
    gecko_manager._connections[42] = conn
    assert gecko_manager._resolved_connection_key("42") == 42
    assert gecko_manager.get_connection("42") is conn


async def test_get_connection_status_missing_and_present(
    hass: HomeAssistant, gecko_manager: GeckoConnectionManager
) -> None:
    assert gecko_manager.get_connection_status("nope")["exists"] is False
    client = SimpleNamespace(connectivity_status=None)
    gecko_manager._connections["m1"] = GeckoMonitorConnection(
        monitor_id="m1",
        gecko_client=client,
        websocket_url="wss://b",
        vessel_name="Tub",
        is_connected=True,
    )
    st = gecko_manager.get_connection_status("m1")
    assert st["exists"] is True
    assert st["connected"] is True
    assert st["vessel_name"] == "Tub"


async def test_async_remove_callback(
    hass: HomeAssistant, gecko_manager: GeckoConnectionManager
) -> None:

    def cb(_zones):
        return None

    conn = GeckoMonitorConnection(
        monitor_id="m1",
        gecko_client=None,
        websocket_url="wss://b",
        vessel_name="Tub",
        update_callbacks=[cb],
    )
    gecko_manager._connections["m1"] = conn
    await gecko_manager.async_remove_callback("m1", cb)
    assert cb not in conn.update_callbacks


async def test_async_disconnect_monitor(
    hass: HomeAssistant, gecko_manager: GeckoConnectionManager
) -> None:
    disconnect_mock = MagicMock()
    client = SimpleNamespace(disconnect=disconnect_mock)
    gecko_manager._connections["m1"] = GeckoMonitorConnection(
        monitor_id="m1",
        gecko_client=client,
        websocket_url="wss://b",
        vessel_name="Tub",
        is_connected=True,
    )
    await gecko_manager.async_disconnect_monitor("m1")
    disconnect_mock.assert_called_once()
    assert "m1" not in gecko_manager._connections


async def test_async_get_connection_manager_singleton(
    hass: HomeAssistant,
) -> None:
    m1 = await async_get_connection_manager(hass)
    m2 = await async_get_connection_manager(hass)
    assert m1 is m2


async def test_get_connection_status_gecko_client_raises(
    hass: HomeAssistant, gecko_manager: GeckoConnectionManager
) -> None:
    class _Bad:
        @property
        def connectivity_status(self):
            raise RuntimeError("unavailable")

    gecko_manager._connections["m1"] = GeckoMonitorConnection(
        monitor_id="m1",
        gecko_client=_Bad(),
        websocket_url="wss://b",
        vessel_name="Tub",
        is_connected=True,
    )
    st = gecko_manager.get_connection_status("m1")
    assert st["exists"] is True
    assert st["connected"] is False
    assert "error" in st


async def test_setup_client_handlers_invokes_zone_callbacks(
    hass: HomeAssistant, gecko_manager: GeckoConnectionManager
) -> None:
    received: list[object] = []

    def cb(zones):
        received.append(zones)

    conn = GeckoMonitorConnection(
        monitor_id="m1",
        gecko_client=None,
        websocket_url="wss://b",
        vessel_name="Tub",
        update_callbacks=[cb],
    )
    zone_handlers: list = []
    connectivity_handlers: list = []

    class _Client:
        def on_zone_update(self, fn):
            zone_handlers.append(fn)

        def on(self, _channel, fn):
            connectivity_handlers.append(fn)

    client = _Client()
    gecko_manager._setup_client_handlers(client, conn, "m1")
    assert len(zone_handlers) == 1
    zone_handlers[0]({"z": 1})
    assert received == [{"z": 1}]
