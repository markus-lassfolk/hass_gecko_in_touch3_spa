"""Diagnostics support for Gecko integration."""

from __future__ import annotations

import logging
from typing import Any

from gecko_iot_client import GeckoIotClient
from gecko_iot_client.models.connectivity import ConnectivityStatus
from gecko_iot_client.models.zone_types import ZoneType
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .connection_manager import async_get_connection_manager
from .energy_parse import (
    coerce_energy_consumption_kwh,
    coerce_energy_cost_amount,
    coerce_energy_score_value,
)
from .shadow_metrics import shadow_topology_summary

_LOGGER = logging.getLogger(__name__)


def _oauth_token_diagnostics(token: Any, *, label: str) -> dict[str, Any]:
    """Non-secret metadata so support can verify which token is linked and expiry."""
    if not isinstance(token, dict):
        return {"label": label, "stored": False}
    return {
        "label": label,
        "stored": True,
        "expires_at": token.get("expires_at"),
        "has_refresh_token": bool(token.get("refresh_token")),
        "has_access_token": bool(token.get("access_token")),
        "token_type": token.get("token_type"),
    }


def _temperature_control_zones_summary(coordinator: Any) -> list[dict[str, Any]]:
    """Per-zone current/target °C for diagnostics (same source as ``climate`` entities)."""
    getter = getattr(coordinator, "get_zones_by_type", None)
    if not callable(getter):
        return []
    try:
        zones = getter(ZoneType.TEMPERATURE_CONTROL_ZONE)
    except Exception:  # pragma: no cover - defensive
        return []
    rows: list[dict[str, Any]] = []
    for zone in zones:
        zid = getattr(zone, "id", None)
        if zid is None:
            continue
        row: dict[str, Any] = {"zone_id": zid}
        try:
            cur = getattr(zone, "temperature", None)
            tgt = getattr(zone, "target_temperature", None)
            if cur is not None:
                row["current_temperature_c"] = float(cur)
            if tgt is not None:
                row["target_temperature_c"] = float(tgt)
            tmin = getattr(zone, "min_temperature_set_point_c", None)
            tmax = getattr(zone, "max_temperature_set_point_c", None)
            if tmin is not None:
                row["min_setpoint_c"] = float(tmin)
            if tmax is not None:
                row["max_setpoint_c"] = float(tmax)
        except (TypeError, ValueError):
            continue
        rows.append(row)
    return rows


def _get_gecko_client_info(gecko_client: GeckoIotClient) -> dict[str, Any]:
    """Get gecko client diagnostics."""
    try:
        client_info: dict[str, Any] = {
            "client_id": gecko_client.id,
            "is_connected": gecko_client.is_connected,
            "has_configuration": gecko_client._configuration is not None,
            "has_state": gecko_client._state is not None,
        }

        if gecko_client.connectivity_status:
            connectivity = gecko_client.connectivity_status
            client_info["connectivity"] = {
                "transport_connected": connectivity.transport_connected,
                "gateway_status": connectivity.gateway_status,
                "vessel_status": connectivity.vessel_status,
                "is_fully_connected": connectivity.is_fully_connected,
            }

        if gecko_client.operation_mode_controller:
            omc = gecko_client.operation_mode_controller
            client_info["operation_mode"] = {
                "mode": omc.operation_mode.value if omc.operation_mode else None,
                "mode_name": omc.mode_name,
                "is_energy_saving": omc.is_energy_saving,
            }

        if gecko_client._zones:
            client_info["zones"] = {
                zone_type.value: len(zones)
                for zone_type, zones in gecko_client._zones.items()
            }

        if gecko_client.transporter:
            transporter = gecko_client.transporter
            transporter_info: dict[str, Any] = {
                "type": type(transporter).__name__,
            }
            monitor_id = getattr(transporter, "monitor_id", None)
            if monitor_id:
                transporter_info["monitor_id"] = monitor_id
            mqtt_client = getattr(transporter, "_mqtt_client", None)
            if mqtt_client and hasattr(mqtt_client, "is_connected"):
                transporter_info["mqtt_connected"] = mqtt_client.is_connected()
            client_info["transporter"] = transporter_info

        state = getattr(gecko_client, "_state", None)
        if isinstance(state, dict):
            client_info["shadow_topology"] = shadow_topology_summary(state)
            cfg = getattr(gecko_client, "_configuration", None)
            if isinstance(cfg, dict):
                zc = cfg.get("zones")
                if isinstance(zc, dict):
                    client_info["configuration_zones_keys"] = sorted(zc.keys())

        return client_info
    except Exception as err:
        _LOGGER.exception("Error getting gecko client info")
        msg = str(err).replace("\n", " ")[:200]
        return {"error": type(err).__name__, "message": msg}


def _get_connection_diagnostics(connection_manager: Any) -> dict[str, Any]:
    """Get connection manager diagnostics."""
    if not connection_manager:
        return {}

    connections: dict[str, Any] = {}
    for monitor_id, connection in connection_manager._connections.items():
        conn_data: dict[str, Any] = {
            "monitor_id": monitor_id,
            "vessel_name": connection.vessel_name,
            "is_connected": connection.is_connected,
            "callback_count": len(connection.update_callbacks),
            # Broker URL embeds JWTs; keep an explicit placeholder so diagnostics
            # never regresses to dumping raw credentials via future serialization.
            "websocket_url": "<REDACTED>",
        }

        if connection.connectivity_status:
            connectivity: ConnectivityStatus = connection.connectivity_status
            conn_data["connectivity_status"] = {
                "transport_connected": connectivity.transport_connected,
                "gateway_status": str(connectivity.gateway_status),
                "vessel_status": str(connectivity.vessel_status),
                "is_fully_connected": connectivity.is_fully_connected,
            }

        if connection.gecko_client:
            conn_data["gecko_client"] = _get_gecko_client_info(connection.gecko_client)

        connections[monitor_id] = conn_data

    return connections


def _get_vessel_coordinators_diagnostics(
    config_entry: ConfigEntry,
) -> list[dict[str, Any]]:
    """Summarize per-vessel coordinators from config entry runtime data."""
    out: list[dict[str, Any]] = []
    if not hasattr(config_entry, "runtime_data") or not config_entry.runtime_data:
        return out
    coordinators = getattr(config_entry.runtime_data, "coordinators", None)
    if not coordinators:
        return out

    for coord in coordinators:
        entry: dict[str, Any] = {
            "vessel_id": coord.vessel_id,
            "vessel_name": coord.vessel_name,
            "monitor_id": coord.monitor_id,
            "has_initial_zones": coord._has_initial_zones,
            "zone_types": [zt.value for zt in coord.get_all_zones().keys()],
            "shadow_extension_metric_count": len(coord._shadow_metric_values),
        }
        if coord._shadow_metric_values:
            entry["shadow_extension_metric_paths"] = sorted(
                coord._shadow_metric_values.keys()
            )[:48]
        entry["cloud_tile_metric_count"] = len(coord._cloud_tile_metrics)
        entry["cloud_string_metric_count"] = len(coord._cloud_string_metrics)
        entry["cloud_bool_metric_count"] = len(coord._cloud_bool_metrics)
        if coord._cloud_tile_metrics:
            entry["cloud_tile_metric_paths"] = sorted(coord._cloud_tile_metrics.keys())[
                :48
            ]
        entry["last_cloud_poll_monotonic"] = coord._last_cloud_poll_monotonic
        tcz = _temperature_control_zones_summary(coord)
        if tcz:
            entry["temperature_control_zones"] = tcz
        out.append(entry)
    return out


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    connection_manager = await async_get_connection_manager(hass)

    diagnostics_data: dict[str, Any] = {
        "config_entry": {
            "entry_id": config_entry.entry_id,
            "title": config_entry.title,
            "domain": config_entry.domain,
            "state": config_entry.state.value,
            "version": config_entry.version,
            "has_account_id": bool(
                str(config_entry.data.get("account_id", "")).strip()
            ),
        },
        "oauth_tokens": {
            "community": _oauth_token_diagnostics(
                config_entry.data.get("token"), label="community (HA OAuth)"
            ),
            "app_premium": _oauth_token_diagnostics(
                config_entry.data.get("app_token"), label="mobile app (premium REST)"
            ),
        },
        "vessels": _get_vessel_coordinators_diagnostics(config_entry),
        "connections": _get_connection_diagnostics(connection_manager),
    }

    if hasattr(config_entry, "runtime_data") and config_entry.runtime_data:
        rd = config_entry.runtime_data
        coordinators = getattr(rd, "coordinators", []) or []
        energy_summary: list[dict[str, Any]] = []
        for coord in coordinators:
            getter = getattr(coord, "get_energy_data", None)
            if not callable(getter):
                continue
            ed = getter()
            parseable: list[str] = []
            if coerce_energy_consumption_kwh(ed.get("consumption")) is not None:
                parseable.append("consumption")
            if coerce_energy_cost_amount(ed.get("cost")) is not None:
                parseable.append("cost")
            if coerce_energy_score_value(ed.get("score")) is not None:
                parseable.append("score")
            vessel_energy: dict[str, Any] = {
                "vessel_name": getattr(coord, "vessel_name", None),
                "vessel_id": getattr(coord, "vessel_id", None),
                "monitor_id": getattr(coord, "monitor_id", None),
                "energy_keys_with_data": [
                    k for k, v in ed.items() if v is not None
                ],
                "energy_keys_parseable_for_sensors": parseable,
            }
            for ek in ("consumption", "cost", "score"):
                raw = ed.get(ek)
                if raw is not None:
                    vessel_energy[f"raw_{ek}"] = raw
            energy_summary.append(vessel_energy)
        diagnostics_data["runtime_data"] = {
            "api_client_type": type(getattr(rd, "api_client", None)).__name__,
            "coordinator_count": len(coordinators),
            "premium_energy_client": getattr(rd, "app_api_client", None) is not None,
            "energy_data_per_vessel": energy_summary,
        }

    # Full MQTT shadow state + flow zone runtime for debugging pump/thermostat issues.
    shadow_dumps: list[dict[str, Any]] = []
    for monitor_id, connection in (connection_manager._connections or {}).items():
        gc = getattr(connection, "gecko_client", None)
        if not gc:
            continue
        dump: dict[str, Any] = {"monitor_id": monitor_id}
        state = getattr(gc, "_state", None)
        if isinstance(state, dict):
            st = state.get("state", {})
            reported = st.get("reported", {}) if isinstance(st, dict) else {}
            desired = st.get("desired", {}) if isinstance(st, dict) else {}
            delta = st.get("delta", {}) if isinstance(st, dict) else {}
            dump["reported_zones"] = (
                reported.get("zones") if isinstance(reported, dict) else None
            )
            dump["desired_zones"] = (
                desired.get("zones") if isinstance(desired, dict) else None
            )
            dump["delta_zones"] = (
                delta.get("zones") if isinstance(delta, dict) else None
            )
            dump["reported_features"] = (
                reported.get("features") if isinstance(reported, dict) else None
            )
        zones = getattr(gc, "_zones", None)
        if isinstance(zones, dict):
            flow_runtime: list[dict[str, Any]] = []
            for zt, zlist in zones.items():
                for z in zlist:
                    flow_runtime.append(
                        {
                            "zone_type": zt.value if hasattr(zt, "value") else str(zt),
                            "id": getattr(z, "id", None),
                            "name": getattr(z, "name", None),
                            "active": getattr(z, "active", None),
                            "speed": getattr(z, "speed", None),
                            "target_temperature": getattr(
                                z, "target_temperature", None
                            ),
                            "temperature": getattr(z, "temperature", None),
                            "set_point": getattr(z, "set_point", None),
                            "status": str(getattr(z, "status", None)),
                        }
                    )
            dump["zone_objects"] = flow_runtime
        shadow_dumps.append(dump)
    if shadow_dumps:
        diagnostics_data["mqtt_shadow_state"] = shadow_dumps

    return diagnostics_data
