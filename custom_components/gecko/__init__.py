"""The Gecko integration."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from gecko_iot_client.transporters.exceptions import (
    ConfigurationError as GeckoConfigurationError,
)
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
    rest_vessels_cache_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, repr=False, compare=False
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
    rest_vessel_detail_cache: dict[str, dict[str, Any]] = field(
        default_factory=dict, repr=False, compare=False
    )
    rest_vessel_detail_mono: dict[str, float] = field(
        default_factory=dict, repr=False, compare=False
    )
    rest_vessel_detail_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, repr=False, compare=False
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
    _t0 = time.monotonic()
    _LOGGER.debug(
        "Gecko setup starting for entry %s (%d vessels configured)",
        entry.entry_id,
        len(entry.data.get("vessels", [])),
    )

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

    try:
        _t1 = time.monotonic()
        await _setup_vessels_and_gecko_clients(hass, entry)
        _LOGGER.debug(
            "Gecko vessel connections established in %.1fs", time.monotonic() - _t1
        )
    except (ConnectionError, TimeoutError, OSError, GeckoConfigurationError) as ex:
        _LOGGER.debug(
            "Gecko setup triggering retry (ConfigEntryNotReady): %s", ex
        )
        raise ConfigEntryNotReady(f"Failed to connect to Gecko device: {ex}") from ex
    except KeyError as ex:
        _LOGGER.debug(
            "Gecko setup triggering retry (missing key): %s", ex
        )
        raise ConfigEntryNotReady(f"Failed to connect to Gecko device: {ex}") from ex

    for coordinator in entry.runtime_data.coordinators:
        _t2 = time.monotonic()
        await coordinator.async_ensure_initial_setup()
        _LOGGER.debug(
            "Initial setup for %s completed in %.1fs",
            coordinator.vessel_name,
            time.monotonic() - _t2,
        )

    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    await async_setup_services(hass)

    _LOGGER.info(
        "Gecko integration setup completed for %d vessels in %.1fs",
        vessels_count,
        time.monotonic() - _t0,
    )

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
            _LOGGER.debug(
                "Failed to setup vessel %s: %s", vessel_name, e, exc_info=True
            )
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
        _LOGGER.debug(
            "Fetching livestream URL for vessel %s (monitor %s)",
            vessel_name,
            monitor_id,
        )
        _t0 = time.monotonic()
        livestream_data = await api_client.async_get_monitor_livestream(monitor_id)
        websocket_url = livestream_data.get("brokerUrl")
        _LOGGER.debug(
            "Livestream URL obtained for monitor %s in %.1fs (url_present=%s)",
            monitor_id,
            time.monotonic() - _t0,
            bool(websocket_url),
        )

        if not websocket_url:
            _LOGGER.error(
                "No WebSocket URL found in livestream response for monitor %s",
                monitor_id,
            )
            return

        _t1 = time.monotonic()
        await coordinator.async_setup_monitor_connection(
            websocket_url=websocket_url
        )
        _LOGGER.debug(
            "MQTT connection for vessel %s (monitor %s) established in %.1fs",
            vessel_name,
            monitor_id,
            time.monotonic() - _t1,
        )

    except Exception as ex:
        _LOGGER.debug(
            "Failed to set up connection for monitor %s: %s",
            monitor_id,
            ex,
        )
        raise


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Gecko unloading entry %s", entry.entry_id)
    runtime_data: GeckoRuntimeData = entry.runtime_data
    for coordinator in runtime_data.coordinators:
        await coordinator.async_shutdown()

    try:
        connection_manager = await async_get_connection_manager(hass)
        vessels = entry.data.get("vessels", [])
        for vessel in vessels:
            monitor_id = vessel.get("monitorId")
            if monitor_id:
                _LOGGER.debug("Disconnecting monitor %s during unload", monitor_id)
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
    _LOGGER.debug("Gecko unload complete for entry %s (ok=%s)", entry.entry_id, unload_ok)
    return unload_ok
