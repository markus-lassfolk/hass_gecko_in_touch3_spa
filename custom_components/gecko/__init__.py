"""The Gecko integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .api import OAuthGeckoApi
from .connection_manager import async_get_connection_manager
from .const import (
    CONF_ALERTS_POLL_INTERVAL,
    DEFAULT_ALERTS_POLL_INTERVAL,
    DOMAIN,
    OAUTH2_AUTHORIZE,
    OAUTH2_CLIENT_ID,
    OAUTH2_TOKEN,
)
from .coordinator import GeckoVesselCoordinator
from .oauth_implementation import GeckoPKCEOAuth2Implementation
from .services import async_remove_services, async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_LOGGER = logging.getLogger(__name__)

_TARGET_ENTRY_VERSION = 2


def _rest_alerts_entities_enabled(entry: ConfigEntry) -> bool:
    """REST alert entities are only useful while alerts polling is enabled."""
    return (
        int(entry.options.get(CONF_ALERTS_POLL_INTERVAL, DEFAULT_ALERTS_POLL_INTERVAL))
        > 0
    )


def _rest_alerts_toggle_state_key(entry_id: str) -> str:
    """Stable hass.data key for alerts-toggle reload bookkeeping (one string, no tuple collisions)."""
    return f"{DOMAIN}.rest_alerts_entities_enabled.{entry_id}"


async def _async_reload_if_rest_alerts_toggle(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Reload when alerts poll interval crosses zero so alert platforms add/remove."""
    key = _rest_alerts_toggle_state_key(entry.entry_id)
    prev = hass.data.get(key)
    now = _rest_alerts_entities_enabled(entry)
    hass.data[key] = now
    if prev is not None and prev != now:
        await hass.config_entries.async_reload(entry.entry_id)


async def _async_resolve_missing_account_id(
    hass: HomeAssistant, entry: ConfigEntry
) -> str | None:
    """Resolve Gecko cloud account_id using stored OAuth tokens (same path as config flow)."""
    try:
        implementation = (
            await config_entry_oauth2_flow.async_get_config_entry_implementation(
                hass, entry
            )
        )
    except Exception as err:
        _LOGGER.warning(
            "Gecko migration: cannot load OAuth implementation for entry %s: %s",
            entry.entry_id,
            err,
        )
        return None

    session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
    try:
        await session.async_ensure_token_valid()
    except Exception as err:
        _LOGGER.warning(
            "Gecko migration: token refresh failed for entry %s: %s",
            entry.entry_id,
            err,
        )
        return None

    token = session.token or {}
    access = token.get("access_token")
    if not access:
        _LOGGER.warning(
            "Gecko migration: no access token for entry %s after ensure_token_valid",
            entry.entry_id,
        )
        return None

    from .api import ConfigFlowGeckoApi

    api_client = ConfigFlowGeckoApi(hass, access)
    try:
        user_id = await api_client.async_get_user_id()
        user_data = await api_client.async_get_user_info(user_id)
    except Exception as err:
        _LOGGER.warning(
            "Gecko migration: user/account API failed for entry %s: %s",
            entry.entry_id,
            err,
        )
        return None

    account_data = user_data.get("account") or {}
    account_id = str(account_data.get("accountId", "")).strip()
    if not account_id:
        _LOGGER.warning(
            "Gecko migration: user endpoint returned no accountId for entry %s",
            entry.entry_id,
        )
        return None

    return account_id


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entry to latest version."""
    if entry.version > _TARGET_ENTRY_VERSION:
        return False

    need_version_bump = entry.version < _TARGET_ENTRY_VERSION
    existing_account = str(entry.data.get("account_id", "")).strip()

    resolved_account = existing_account
    if not resolved_account:
        for attempt in range(3):
            _LOGGER.info(
                "Gecko migration: resolving account_id for entry %s "
                "(stored version %s, attempt %s/3)",
                entry.entry_id,
                entry.version,
                attempt + 1,
            )
            resolved_account = (
                await _async_resolve_missing_account_id(hass, entry) or ""
            ).strip()
            if resolved_account:
                break
            if attempt < 2:
                await asyncio.sleep(0.75)

    if not resolved_account and not existing_account:
        _LOGGER.error(
            "Gecko migration: could not resolve account_id for entry %s; "
            "cloud REST tile metrics and alerts stay disabled until API access works "
            "(check network or re-authenticate).",
            entry.entry_id,
        )

    current = hass.config_entries.async_get_entry(entry.entry_id)
    if current is None:
        return False

    if need_version_bump:
        data = dict(current.data)
        if resolved_account:
            data["account_id"] = resolved_account
        hass.config_entries.async_update_entry(
            current,
            data=data,
            version=_TARGET_ENTRY_VERSION,
        )
    elif (
        resolved_account
        and str(current.data.get("account_id", "")).strip() != resolved_account
    ):
        data = dict(current.data)
        data["account_id"] = resolved_account
        hass.config_entries.async_update_entry(current, data=data)

    return True


async def async_setup(hass: HomeAssistant, _config: dict) -> bool:
    """Set up the Gecko component."""
    # Register hardcoded OAuth implementation with PKCE (no user credentials needed)
    config_entry_oauth2_flow.async_register_implementation(
        hass,
        DOMAIN,
        GeckoPKCEOAuth2Implementation(
            hass,
            DOMAIN,
            client_id=OAUTH2_CLIENT_ID,
            authorize_url=OAUTH2_AUTHORIZE,
            token_url=OAUTH2_TOKEN,
        ),
    )
    return True


@dataclass
class GeckoRuntimeData:
    """Runtime data for Gecko integration."""

    api_client: OAuthGeckoApi
    coordinators: list[GeckoVesselCoordinator]
    rest_vessels_response_cache: list[Any] | None = field(
        default=None, repr=False, compare=False
    )
    rest_vessels_response_cache_mono: float | None = field(
        default=None, repr=False, compare=False
    )
    rest_vessels_cache_account_id: str | None = field(
        default=None, repr=False, compare=False
    )
    rest_alerts_messages_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, repr=False, compare=False
    )
    rest_alerts_messages_payload: Any | None = field(
        default=None, repr=False, compare=False
    )
    rest_alerts_messages_mono: float | None = field(
        default=None, repr=False, compare=False
    )
    rest_alerts_messages_account_id: str | None = field(
        default=None, repr=False, compare=False
    )
    rest_alerts_actions_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, repr=False, compare=False
    )
    rest_alerts_actions_cache: dict[tuple[str, str], dict[str, Any]] = field(
        default_factory=dict, repr=False, compare=False
    )


# List the platforms that this integration supports.
_PLATFORMS: list[Platform] = [
    Platform.LIGHT,
    Platform.FAN,
    Platform.CLIMATE,
    Platform.SELECT,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.NUMBER,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Gecko from a config entry."""
    # Fallback: resolve missing account_id for version-2 entries (recovery path)
    if (
        entry.version == _TARGET_ENTRY_VERSION
        and not str(entry.data.get("account_id", "")).strip()
    ):
        _LOGGER.info(
            "Gecko setup: version-2 entry %s missing account_id, attempting resolution",
            entry.entry_id,
        )
        resolved = await _async_resolve_missing_account_id(hass, entry)
        if resolved:
            data = dict(entry.data)
            data["account_id"] = resolved
            hass.config_entries.async_update_entry(entry, data=data)
            _LOGGER.info(
                "Gecko setup: resolved account_id for entry %s",
                entry.entry_id,
            )
        else:
            _LOGGER.warning(
                "Gecko setup: could not resolve account_id for entry %s; "
                "cloud REST features (tiles, alerts) will be limited",
                entry.entry_id,
            )

    implementation = (
        await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, entry
        )
    )

    session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)

    # Create OAuth-based Gecko API client
    api_client = OAuthGeckoApi(hass, session)

    # Create one coordinator per vessel following Home Assistant best practices
    vessels = entry.data.get("vessels", [])
    vessels_count = len(vessels)
    if vessels_count == 0:
        _LOGGER.warning("No vessels found in config entry")

    coordinators = []
    for vessel in vessels:
        vessel_id = vessel.get("vesselId")
        monitor_id = vessel.get("monitorId")
        vessel_name = vessel.get("name", f"Vessel {vessel_id}")

        coordinator = GeckoVesselCoordinator(
            hass=hass,
            entry_id=entry.entry_id,
            vessel_id=vessel_id,
            monitor_id=monitor_id,
            vessel_name=vessel_name,
        )
        coordinators.append(coordinator)

    # Store in runtime data
    entry.runtime_data = GeckoRuntimeData(
        api_client=api_client,
        coordinators=coordinators,
    )

    # Create devices for each vessel/spa and set up geckoIotClient
    # Use specific exceptions to trigger Home Assistant's retry mechanism only for
    # connection-related issues, not programming errors
    try:
        await _setup_vessels_and_gecko_clients(hass, entry)
    except (ConnectionError, TimeoutError, OSError) as ex:
        # These indicate temporary connection issues that should trigger retry
        raise ConfigEntryNotReady(f"Failed to connect to Gecko device: {ex}") from ex
    except KeyError as ex:
        # Missing required data (e.g., 'refresh_token') indicates auth issues
        raise ConfigEntryNotReady(f"Failed to connect to Gecko device: {ex}") from ex

    # One refresh + zone wait before platforms so each platform does not repeat the wait.
    for coordinator in entry.runtime_data.coordinators:
        await coordinator.async_ensure_initial_setup()

    # Set up platforms immediately - entities will be created when zone data becomes available
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)

    # Domain services (async_setup runs only once per HA restart; unload may remove them).
    await async_setup_services(hass)

    hass.data[_rest_alerts_toggle_state_key(entry.entry_id)] = (
        _rest_alerts_entities_enabled(entry)
    )
    entry.async_on_unload(
        entry.async_add_update_listener(_async_reload_if_rest_alerts_toggle)
    )

    _LOGGER.info("Gecko integration setup completed for %d vessels", vessels_count)

    return True


async def _setup_vessels_and_gecko_clients(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Set up devices for each vessel/spa and geckoIotClient connections."""
    runtime_data: GeckoRuntimeData = entry.runtime_data
    vessels = entry.data.get("vessels", [])

    if not vessels:
        _LOGGER.warning("No vessels found in config entry data!")
        return

    device_registry = dr.async_get(hass)
    api_client = runtime_data.api_client

    # Match each vessel with its coordinator
    for i, (vessel, coordinator) in enumerate(zip(vessels, runtime_data.coordinators)):
        vessel_name = vessel.get("name", f"Vessel {i}")

        try:
            _setup_vessel_device(entry, vessel, device_registry)
            await _setup_vessel_gecko_client(vessel, api_client, coordinator)
        except Exception as e:
            _LOGGER.error(
                "Failed to setup vessel %s: %s", vessel_name, e, exc_info=True
            )
            # Re-raise to allow async_setup_entry to handle with ConfigEntryNotReady
            raise


def _setup_vessel_device(
    entry: ConfigEntry, vessel: dict, device_registry: dr.DeviceRegistry
) -> None:
    """Set up device registry entry for a vessel."""
    vessel_id = vessel.get("vesselId")
    vessel_name = vessel.get("name", f"Vessel {vessel_id}")
    vessel_type = vessel.get("type", "Unknown")
    protocol_name = vessel.get("protocolName", "Unknown")
    monitor_id = vessel.get("monitorId")

    # Create a more descriptive device name
    device_name = vessel_name

    # Create device entry for this spa/vessel
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(entry.domain, str(vessel_id))},
        name=device_name,
        manufacturer="Gecko",
        model=f"{vessel_type} ({protocol_name})",
        sw_version=None,
        serial_number=monitor_id,
    )


async def _setup_vessel_gecko_client(
    vessel: dict, api_client: OAuthGeckoApi, coordinator: GeckoVesselCoordinator
) -> None:
    """Set up geckoIotClient connection for a vessel using the singleton connection manager."""
    vessel_id = vessel.get("vesselId")
    vessel_name = vessel.get("name", f"Vessel {vessel_id}")
    monitor_id = vessel.get("monitorId")

    if not monitor_id:
        _LOGGER.error(
            "No monitor ID found for vessel %s. Available keys: %s",
            vessel_name,
            list(vessel.keys()),
        )
        return

    try:
        livestream_data = await api_client.async_get_monitor_livestream(monitor_id)
        websocket_url = livestream_data.get("brokerUrl")

        if not websocket_url:
            _LOGGER.error(
                "No WebSocket URL found in livestream response for monitor %s",
                monitor_id,
            )
            return

        # Don't create zones from spa configuration in coordinator - let GeckoIotClient handle this
        # The coordinator will get zones from the GeckoIotClient once it's connected and configured

        # Use the singleton connection manager through the coordinator
        success = await coordinator.async_setup_monitor_connection(
            websocket_url=websocket_url
        )

        if not success:
            raise ConnectionError(
                f"Failed to setup connection for monitor {monitor_id}"
            )

    except Exception as ex:
        _LOGGER.error(
            "Failed to set up connection for monitor %s: %s",
            monitor_id,
            ex,
            exc_info=True,
        )
        raise


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.data.pop(_rest_alerts_toggle_state_key(entry.entry_id), None)
    # Clean up all vessel coordinators
    runtime_data: GeckoRuntimeData = entry.runtime_data
    for coordinator in runtime_data.coordinators:
        await coordinator.async_shutdown()

    # Disconnect all monitors from the connection manager
    # This ensures fresh connections on reload with updated config/tokens
    try:
        connection_manager = await async_get_connection_manager(hass)
        vessels = entry.data.get("vessels", [])
        for vessel in vessels:
            monitor_id = vessel.get("monitorId")
            if monitor_id:
                await connection_manager.async_disconnect_monitor(monitor_id)
    except Exception as ex:
        _LOGGER.error("Error disconnecting monitors during unload: %s", ex)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
    if unload_ok and not any(
        e.state is ConfigEntryState.LOADED
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id
    ):
        await async_remove_services(hass)
    return unload_ok
