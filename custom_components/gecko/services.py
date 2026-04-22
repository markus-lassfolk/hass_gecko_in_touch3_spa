"""Home Assistant services mirroring Gecko MQTT desired-state shapes."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import json
from pathlib import Path

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .connection_manager import async_get_connection_manager
from .shadow_dump import (
    build_shadow_export_payload,
    safe_export_filename,
    write_json_file,
)

_LOGGER = logging.getLogger(__name__)

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_MONITOR_ID = "monitor_id"
ATTR_ZONE_TYPE = "zone_type"
ATTR_ZONE_ID = "zone_id"
ATTR_UPDATES = "updates"

SERVICE_PUBLISH_ZONE_DESIRED = "publish_zone_desired"
SERVICE_PUBLISH_FEATURE_DESIRED = "publish_feature_desired"
SERVICE_PUBLISH_DESIRED_STATE = "publish_desired_state"
SERVICE_DUMP_SHADOW_SNAPSHOT = "dump_shadow_snapshot"

ATTR_INCLUDE_CONFIGURATION = "include_configuration"
ATTR_INCLUDE_DERIVED = "include_derived"
ATTR_FILENAME = "filename"
ATTR_SANITIZE_FOR_PUBLIC_SHARE = "sanitize_for_public_share"

DESIRED_STATE_ALLOWED_ROOT_KEYS = frozenset({"zones", "features"})
_MAX_DESIRED_JSON_BYTES = 32000

ATTR_DESIRED_FRAGMENT = "desired_fragment"


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


def _as_desired_fragment(value: Any) -> dict[str, Any]:
    frag = _as_dict(ATTR_DESIRED_FRAGMENT, value)
    if not set(frag).issubset(DESIRED_STATE_ALLOWED_ROOT_KEYS):
        raise vol.Invalid(
            f"{ATTR_DESIRED_FRAGMENT} may only contain keys: "
            f"{sorted(DESIRED_STATE_ALLOWED_ROOT_KEYS)}"
        )
    try:
        raw = json.dumps(frag)
    except (TypeError, ValueError) as err:
        raise vol.Invalid(f"Fragment is not JSON-serializable: {err}") from err
    if len(raw.encode("utf-8")) > _MAX_DESIRED_JSON_BYTES:
        raise vol.Invalid("Fragment exceeds maximum size")
    return frag


PUBLISH_DESIRED_STATE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_MONITOR_ID): cv.string,
        vol.Required(ATTR_DESIRED_FRAGMENT): _as_desired_fragment,
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
    """Return real MQTT monitor ids only (``vesselId`` is accepted elsewhere for lookups)."""
    out: set[str] = set()
    for v in entry.data.get("vessels") or []:
        if not isinstance(v, dict):
            continue
        val = v.get("monitorId")
        if val is None:
            continue
        mid = str(val).strip()
        if mid:
            out.add(mid)
    return out


def _vessel_id_for_monitor(entry, monitor_id: str) -> str:
    for v in entry.data.get("vessels") or []:
        if not isinstance(v, dict):
            continue
        if str(v.get("monitorId")) == str(monitor_id) or str(v.get("vesselId")) == str(
            monitor_id
        ):
            vid = v.get("vesselId")
            if vid is not None:
                return str(vid)
    return str(monitor_id)


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


async def async_handle_publish_zone_desired(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Publish ``{"zones": {type: {id: updates}}}`` like gecko-iot-client zone callbacks."""
    _validate_config_entry(hass, call)
    client = await _async_client_for_monitor_from_call(hass, call)
    zone_type = str(call.data[ATTR_ZONE_TYPE])
    zone_id = str(call.data[ATTR_ZONE_ID])
    updates = call.data[ATTR_UPDATES]
    desired = {"zones": {zone_type: {zone_id: updates}}}
    await hass.async_add_executor_job(client.transporter.publish_desired_state, desired)


async def async_handle_publish_feature_desired(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Publish ``{"features": updates}`` like the library feature callback."""
    _validate_config_entry(hass, call)
    client = await _async_client_for_monitor_from_call(hass, call)
    updates = call.data[ATTR_UPDATES]
    desired = {"features": updates}
    await hass.async_add_executor_job(client.transporter.publish_desired_state, desired)


async def async_handle_publish_desired_state(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Publish a validated shadow ``desired`` fragment (``zones`` / ``features`` only)."""
    _validate_config_entry(hass, call)
    client = await _async_client_for_monitor_from_call(hass, call)
    fragment = call.data[ATTR_DESIRED_FRAGMENT]
    await hass.async_add_executor_job(client.transporter.publish_desired_state, fragment)


DUMP_SHADOW_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_MONITOR_ID): cv.string,
        vol.Optional(ATTR_INCLUDE_CONFIGURATION, default=True): cv.boolean,
        vol.Optional(ATTR_INCLUDE_DERIVED, default=True): cv.boolean,
        vol.Optional(ATTR_SANITIZE_FOR_PUBLIC_SHARE, default=True): cv.boolean,
        vol.Optional(ATTR_FILENAME): cv.string,
    }
)


async def async_handle_dump_shadow_snapshot(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Write device shadow + configuration JSON under ``<config>/gecko_shadow_dumps/``."""
    _validate_config_entry(hass, call)
    entry_id = str(call.data[ATTR_CONFIG_ENTRY_ID])
    monitor_id = str(call.data[ATTR_MONITOR_ID])
    entry = hass.config_entries.async_get_entry(entry_id)
    if not entry:
        raise HomeAssistantError("Config entry not found")

    mgr = await async_get_connection_manager(hass)
    conn = mgr._connections.get(monitor_id)
    if not conn or not conn.gecko_client:
        raise HomeAssistantError(
            "No Gecko client for this monitor. Open the Gecko integration so MQTT "
            "connects at least once, then run the export again."
        )

    vessel_id = _vessel_id_for_monitor(entry, monitor_id)
    sanitize = bool(call.data.get(ATTR_SANITIZE_FOR_PUBLIC_SHARE, True))
    fname = safe_export_filename(
        call.data.get(ATTR_FILENAME),
        monitor_id,
        anonymous=sanitize and not call.data.get(ATTR_FILENAME),
    )
    dump_dir = Path(hass.config.config_dir) / "gecko_shadow_dumps"
    out_path = (dump_dir / fname).resolve()
    if not str(out_path).startswith(str(dump_dir.resolve())):
        raise HomeAssistantError("Invalid export path")

    payload = build_shadow_export_payload(
        monitor_id=monitor_id,
        vessel_id=vessel_id,
        gecko_client=conn.gecko_client,
        include_configuration=bool(call.data.get(ATTR_INCLUDE_CONFIGURATION, True)),
        include_derived=bool(call.data.get(ATTR_INCLUDE_DERIVED, True)),
        mqtt_connected=getattr(conn, "is_connected", None),
        sanitize_for_public_share=sanitize,
    )

    await hass.async_add_executor_job(write_json_file, out_path, payload)

    if sanitize:
        msg = (
            "A **sanitized** JSON export of the Gecko device shadow was written "
            "(fingerprints instead of raw monitor/vessel ids; credential-like keys "
            "and common secret patterns redacted). Skim the file once more before "
            "posting—unknown vendor keys are not guaranteed clean.\n\n"
            f"**Path:** `{out_path}`\n\n"
            "For a full raw dump for private debugging only, run the service again with "
            "**Sanitize for public share** turned off."
        )
    else:
        msg = (
            "A **full** JSON export of the Gecko device shadow was written (not redacted). "
            "**Do not share publicly** — treat like credentials.\n\n"
            f"**Path:** `{out_path}`\n\n"
            "Copy it from your Home Assistant configuration directory (Samba / SSH / backup)."
        )
    persistent_notification.async_create(
        hass,
        msg,
        title="Gecko shadow export ready",
        notification_id=f"gecko_shadow_dump_{monitor_id}",
    )
    _LOGGER.info("Gecko shadow dump written to %s", out_path)


async def async_remove_services(hass: HomeAssistant) -> None:
    """Unregister Gecko services when the integration is fully unloaded."""
    for name in (
        SERVICE_PUBLISH_ZONE_DESIRED,
        SERVICE_PUBLISH_FEATURE_DESIRED,
        SERVICE_PUBLISH_DESIRED_STATE,
        SERVICE_DUMP_SHADOW_SNAPSHOT,
    ):
        if hass.services.has_service(DOMAIN, name):
            hass.services.async_remove(DOMAIN, name)


def _bind_service_handler(
    hass: HomeAssistant,
    handler: Callable[[HomeAssistant, ServiceCall], Awaitable[None]],
) -> Callable[[ServiceCall], Awaitable[None]]:
    """Wrap ``(hass, call)`` handlers so HA only passes ``ServiceCall`` (no ``call.hass``)."""

    async def _wrapped(call: ServiceCall) -> None:
        await handler(hass, call)

    return _wrapped


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register Gecko services (idempotent across component reload)."""
    registrations: list[
        tuple[
            str,
            Callable[[HomeAssistant, ServiceCall], Awaitable[None]],
            vol.Schema,
        ]
    ] = [
        (SERVICE_PUBLISH_ZONE_DESIRED, async_handle_publish_zone_desired, PUBLISH_ZONE_SCHEMA),
        (
            SERVICE_PUBLISH_FEATURE_DESIRED,
            async_handle_publish_feature_desired,
            PUBLISH_FEATURE_SCHEMA,
        ),
        (
            SERVICE_PUBLISH_DESIRED_STATE,
            async_handle_publish_desired_state,
            PUBLISH_DESIRED_STATE_SCHEMA,
        ),
        (
            SERVICE_DUMP_SHADOW_SNAPSHOT,
            async_handle_dump_shadow_snapshot,
            DUMP_SHADOW_SCHEMA,
        ),
    ]
    for name, handler, schema in registrations:
        if not hass.services.has_service(DOMAIN, name):
            hass.services.async_register(
                DOMAIN, name, _bind_service_handler(hass, handler), schema=schema
            )
    _LOGGER.debug("Registered %s services", DOMAIN)
