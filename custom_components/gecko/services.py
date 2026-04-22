"""Home Assistant services mirroring Gecko MQTT desired-state shapes."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .connection_manager import async_get_connection_manager

_LOGGER = logging.getLogger(__name__)

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_MONITOR_ID = "monitor_id"
ATTR_ZONE_TYPE = "zone_type"
ATTR_ZONE_ID = "zone_id"
ATTR_UPDATES = "updates"

SERVICE_PUBLISH_ZONE_DESIRED = "publish_zone_desired"
SERVICE_PUBLISH_FEATURE_DESIRED = "publish_feature_desired"


def _as_dict(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise vol.Invalid(f"{name} must be a dictionary")
    return value


PUBLISH_ZONE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_MONITOR_ID): cv.string,
        vol.Required(ATTR_ZONE_TYPE): cv.string,
        vol.Required(ATTR_ZONE_ID): cv.string,
        vol.Required(ATTR_UPDATES): lambda v: _as_dict(ATTR_UPDATES, v),
    }
)

PUBLISH_FEATURE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_MONITOR_ID): cv.string,
        vol.Required(ATTR_UPDATES): lambda v: _as_dict(ATTR_UPDATES, v),
    }
)


async def _async_client_for_monitor(hass: HomeAssistant, monitor_id: str) -> Any:
    mgr = await async_get_connection_manager(hass)
    conn = mgr._connections.get(str(monitor_id))
    if not conn or not conn.is_connected:
        raise HomeAssistantError(
            f"No active Gecko MQTT connection for monitor_id={monitor_id}"
        )
    return conn.gecko_client


async def _async_client_for_monitor_from_call(
    hass: HomeAssistant, call: ServiceCall
) -> Any:
    return await _async_client_for_monitor(hass, str(call.data[ATTR_MONITOR_ID]))


def _allowed_monitor_ids(entry) -> set[str]:
    out: set[str] = set()
    for v in entry.data.get("vessels") or []:
        if not isinstance(v, dict):
            continue
        val = v.get("monitorId")
        if val is not None and str(val):
            out.add(str(val))
    return out


def _validate_config_entry(hass: HomeAssistant, call: ServiceCall) -> None:
    entry_id = str(call.data[ATTR_CONFIG_ENTRY_ID])
    entry = hass.config_entries.async_get_entry(entry_id)
    if not entry or entry.domain != DOMAIN:
        raise HomeAssistantError(f"Invalid or unknown Gecko config entry: {entry_id}")
    mid = str(call.data[ATTR_MONITOR_ID])
    if mid not in _allowed_monitor_ids(entry):
        raise HomeAssistantError(
            f"monitor_id {mid} is not part of config entry {entry_id}"
        )


async def async_handle_publish_zone_desired(hass: HomeAssistant, call: ServiceCall) -> None:
    """Publish ``{"zones": {type: {id: updates}}}`` like gecko-iot-client zone callbacks."""
    _validate_config_entry(hass, call)
    client = await _async_client_for_monitor_from_call(hass, call)
    zone_type = str(call.data[ATTR_ZONE_TYPE])
    zone_id = str(call.data[ATTR_ZONE_ID])
    updates = call.data[ATTR_UPDATES]
    desired = {"zones": {zone_type: {zone_id: updates}}}
    client.transporter.publish_desired_state(desired)


async def async_handle_publish_feature_desired(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Publish ``{"features": updates}`` like the library feature callback."""
    _validate_config_entry(hass, call)
    client = await _async_client_for_monitor_from_call(hass, call)
    updates = call.data[ATTR_UPDATES]
    desired = {"features": updates}
    client.transporter.publish_desired_state(desired)


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register Gecko services (idempotent across component reload)."""
    if hass.services.has_service(DOMAIN, SERVICE_PUBLISH_ZONE_DESIRED):
        return

    hass.services.async_register(
        DOMAIN,
        SERVICE_PUBLISH_ZONE_DESIRED,
        async_handle_publish_zone_desired,
        schema=PUBLISH_ZONE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PUBLISH_FEATURE_DESIRED,
        async_handle_publish_feature_desired,
        schema=PUBLISH_FEATURE_SCHEMA,
    )
    _LOGGER.debug("Registered %s services", DOMAIN)
