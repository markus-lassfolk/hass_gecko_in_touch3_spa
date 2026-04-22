"""Tests for API wiring, package setup, migration, and entity availability."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import custom_components.gecko as gecko_pkg
from custom_components.gecko.api import ConfigFlowGeckoApi, OAuthGeckoApi
from custom_components.gecko.connection_manager import (
    GECKO_CONNECTION_MANAGER_KEY,
    GeckoConnectionManager,
    GeckoMonitorConnection,
)
from custom_components.gecko.const import DOMAIN
from custom_components.gecko.entity import GeckoEntityAvailabilityMixin
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_oauth_gecko_api_async_get_access_token_without_gecko_client_init() -> (
    None
):
    """Avoid constructing ``GeckoApiClient`` (background threads) in the HA test harness."""
    session = MagicMock()
    session.async_ensure_token_valid = AsyncMock()
    session.token = {"access_token": "tok123"}
    api = object.__new__(OAuthGeckoApi)
    api._oauth_session = session
    assert await OAuthGeckoApi.async_get_access_token(api) == "tok123"
    session.async_ensure_token_valid.assert_awaited_once()


async def test_configflow_gecko_api_returns_static_token_without_client_init() -> None:
    api = object.__new__(ConfigFlowGeckoApi)
    api._token = "pre-auth-token"
    assert await ConfigFlowGeckoApi.async_get_access_token(api) == "pre-auth-token"


async def test_async_setup_registers_oauth(hass: HomeAssistant) -> None:
    assert await gecko_pkg.async_setup(hass, {}) is True


async def test_migrate_options_defaults_skips_empty_options(
    hass: HomeAssistant,
) -> None:
    """Fresh entries with no persisted options should not trigger a config write."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"vessels": [], "account_id": "a1"},
        options={},
    )
    entry.add_to_hass(hass)
    with patch.object(hass.config_entries, "async_update_entry") as mock_upd:
        gecko_pkg._migrate_options_defaults(hass, entry)
    mock_upd.assert_not_called()


async def test_lazy_resolve_account_id_retries_after_transient_error(
    hass: HomeAssistant,
) -> None:
    """Failed lazy resolve must not set the one-shot flag; a later call may succeed."""
    from custom_components.gecko.coordinator import GeckoVesselCoordinator

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"vessels": [], "account_id": ""},
        options={},
    )
    rd = SimpleNamespace(api_client=MagicMock())
    entry.runtime_data = rd
    entry.add_to_hass(hass)

    rd.api_client.async_get_user_id = AsyncMock(side_effect=[OSError("net"), "u1"])
    rd.api_client.async_get_user_info = AsyncMock(
        return_value={"account": {"accountId": "acct-99"}}
    )

    coord = GeckoVesselCoordinator(hass, entry.entry_id, "v1", "m1", "Spa")

    assert await coord._async_lazy_resolve_account_id() == ""
    assert coord._account_id_resolve_attempted is False

    assert await coord._async_lazy_resolve_account_id() == "acct-99"
    assert coord._account_id_resolve_attempted is True


async def test_async_migrate_entry_bumps_version_and_account(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"vessels": []},
        version=1,
    )
    entry.add_to_hass(hass)
    with patch.object(
        gecko_pkg,
        "_async_resolve_missing_account_id",
        new_callable=AsyncMock,
        return_value="acct-9",
    ):
        ok = await gecko_pkg.async_migrate_entry(hass, entry)
    assert ok is True
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated is not None
    assert updated.version == 2
    assert updated.data.get("account_id") == "acct-9"


async def test_async_migrate_entry_future_version_no_op(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={}, version=99)
    entry.add_to_hass(hass)
    assert await gecko_pkg.async_migrate_entry(hass, entry) is False


async def test_entity_mixin_check_is_connected_uses_connection_manager(
    hass: HomeAssistant,
) -> None:
    class _Probe(GeckoEntityAvailabilityMixin):
        def __init__(self) -> None:
            self.hass = hass
            self.coordinator = SimpleNamespace(monitor_id="mx")
            self._attr_available = False

    mgr = GeckoConnectionManager(hass)
    hass.data[GECKO_CONNECTION_MANAGER_KEY] = mgr
    gc = SimpleNamespace(is_connected=True)
    mgr._connections["mx"] = GeckoMonitorConnection(
        monitor_id="mx",
        gecko_client=gc,
        websocket_url="wss://x",
        vessel_name="Spa",
        is_connected=True,
    )
    probe = _Probe()
    assert probe._check_is_connected() is True
    del mgr._connections["mx"]
    assert probe._check_is_connected() is False
