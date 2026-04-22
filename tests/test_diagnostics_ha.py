"""Home Assistant–scoped tests for config entry diagnostics."""

from __future__ import annotations

from enum import Enum
from types import SimpleNamespace

from custom_components.gecko.const import DOMAIN
from custom_components.gecko.diagnostics import async_get_config_entry_diagnostics
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_async_get_config_entry_diagnostics_minimal(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Spa",
        data={},
        state=ConfigEntryState.LOADED,
    )
    entry.add_to_hass(hass)

    data = await async_get_config_entry_diagnostics(hass, entry)

    assert data["config_entry"]["domain"] == DOMAIN
    assert data["config_entry"]["entry_id"] == entry.entry_id
    assert data["vessels"] == []
    assert data["connections"] == {}


class _ZoneType(Enum):
    FLOW = "flow"


async def test_async_get_config_entry_diagnostics_runtime_data(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Spa",
        data={},
        state=ConfigEntryState.LOADED,
    )
    entry.add_to_hass(hass)
    coord = SimpleNamespace(
        vessel_id="v1",
        vessel_name="S",
        monitor_id="m1",
        _has_initial_zones=True,
        _shadow_metric_values={"zones.a": 1.0},
        get_all_zones=lambda: {_ZoneType.FLOW: {}},
    )
    entry.runtime_data = SimpleNamespace(api_client=None, coordinators=[coord])

    data = await async_get_config_entry_diagnostics(hass, entry)

    assert data["runtime_data"]["api_client_type"] == "NoneType"
    assert data["runtime_data"]["coordinator_count"] == 1
    assert len(data["vessels"]) == 1
    assert data["vessels"][0]["monitor_id"] == "m1"
