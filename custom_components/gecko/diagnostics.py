"""Diagnostics support for Gecko integration."""

from __future__ import annotations

import logging
import time
from typing import Any

from gecko_iot_client import GeckoIotClient
from gecko_iot_client.models.connectivity import ConnectivityStatus
from gecko_iot_client.models.zone_types import ZoneType
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import async_get_platforms

from .connection_manager import async_get_connection_manager
from .const import (
    CONF_ALERTS_POLL_INTERVAL,
    CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
    CONF_CLOUD_REST_POLL_INTERVAL,
    CONF_ENERGY_POLL_INTERVAL,
    DEFAULT_ALERTS_POLL_INTERVAL,
    DEFAULT_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
    DEFAULT_CLOUD_REST_POLL_INTERVAL,
    DEFAULT_ENERGY_POLL_INTERVAL,
)
from .energy_parse import (
    coerce_energy_consumption_kwh,
    coerce_energy_cost_amount,
    coerce_energy_score_value,
    extract_electricity_rate,
)
from .shadow_metrics import infer_sensor_metadata, shadow_topology_summary

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


async def _get_connection_diagnostics(connection_manager: Any) -> dict[str, Any]:
    """Get connection manager diagnostics."""
    if not connection_manager:
        return {}

    async with connection_manager._connection_lock:
        connections_snapshot = dict(connection_manager._connections)

    connections: dict[str, Any] = {}
    for monitor_id, connection in connections_snapshot.items():
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


def _coordinator_health(coord: Any) -> dict[str, Any]:
    """Internal coordinator timers, error counters, and cached alerts snapshot."""
    now = time.monotonic()
    health: dict[str, Any] = {
        "consecutive_failures": getattr(coord, "_consecutive_failures", None),
        "account_id_resolved": getattr(coord, "_account_id_resolve_attempted", None),
        "has_initial_zones": getattr(coord, "_has_initial_zones", None),
    }
    for attr, label in (
        ("_last_cloud_poll_monotonic", "last_cloud_poll_age_s"),
        ("_last_alerts_poll_monotonic", "last_alerts_poll_age_s"),
        ("_last_energy_poll_monotonic", "last_energy_poll_age_s"),
        ("_last_zone_shadow_refresh_mono", "last_shadow_refresh_age_s"),
    ):
        mono = getattr(coord, attr, None)
        health[label] = round(now - mono, 1) if mono is not None else None

    health["energy_api_forbidden"] = getattr(
        coord, "_logged_energy_api_forbidden", False
    )
    health["energy_unparsed_shapes"] = getattr(
        coord, "_logged_energy_unparsed_shapes", False
    )

    snap_fn = getattr(coord, "get_rest_alerts_snapshot", None)
    if callable(snap_fn):
        snap = snap_fn()
        health["alerts_snapshot"] = {
            "total": snap.get("total"),
            "messages_count": len(snap.get("messages") or []),
            "actions_count": len(snap.get("actions") or []),
            "updated_at": snap.get("updated_at"),
            "error": snap.get("error"),
        }
    return health


def _config_entry_options(config_entry: ConfigEntry) -> dict[str, Any]:
    """Effective option values (with defaults applied) for troubleshooting."""
    opts = config_entry.options or {}
    return {
        "cloud_rest_poll_interval": int(
            opts.get(CONF_CLOUD_REST_POLL_INTERVAL, DEFAULT_CLOUD_REST_POLL_INTERVAL)
        ),
        "cloud_rest_only_when_mqtt_down": bool(
            opts.get(
                CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
                DEFAULT_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
            )
        ),
        "alerts_poll_interval": int(
            opts.get(CONF_ALERTS_POLL_INTERVAL, DEFAULT_ALERTS_POLL_INTERVAL)
        ),
        "energy_poll_interval": int(
            opts.get(CONF_ENERGY_POLL_INTERVAL, DEFAULT_ENERGY_POLL_INTERVAL)
        ),
    }


def _pending_grace_remaining(entity: Any) -> float | None:
    """Seconds left on a climate entity's pending-setpoint grace window."""
    deadline = getattr(entity, "_pending_target_deadline_mono", None)
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    return round(max(remaining, 0.0), 1)


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
        entry["health"] = _coordinator_health(coord)
        out.append(entry)
    return out


def _sensor_values_snapshot(coord: Any) -> dict[str, Any]:
    """Current numeric and string sensor values with their inferred HA type hints.

    Groups values into ``numeric`` and ``string`` sub-dicts so it's easy to see
    at a glance what every sensor is reading right now and whether the device_class
    and unit are set correctly.
    """
    numeric: dict[str, Any] = {}
    for path, val in sorted(getattr(coord, "_shadow_metric_values", {}).items()):
        dc, unit = infer_sensor_metadata(path)
        numeric[path] = {
            "value": val,
            "device_class": dc.value if dc else None,
            "unit": unit,
        }

    string: dict[str, str] = dict(
        sorted(getattr(coord, "_shadow_string_values", {}).items())
    )

    return {"numeric": numeric, "string": string}


def _energy_parse_trace(ed: dict[str, Any]) -> dict[str, Any]:
    """Run all energy coerce functions and show results alongside raw payloads."""
    consumption_raw = ed.get("consumption")
    cost_raw = ed.get("cost")
    score_raw = ed.get("score")
    rate, rate_currency = extract_electricity_rate(cost_raw)
    return {
        "consumption": {
            "raw_status": (
                consumption_raw.get("status")
                if isinstance(consumption_raw, dict)
                else None
            ),
            "coerced_kwh": coerce_energy_consumption_kwh(consumption_raw),
        },
        "cost": {
            "coerced_amount": coerce_energy_cost_amount(cost_raw),
            "electricity_rate_per_kwh": rate,
            "electricity_rate_currency": rate_currency,
        },
        "score": {
            "coerced_value": coerce_energy_score_value(score_raw),
            "maximum": (
                score_raw.get("score", {}).get("maximum")
                if isinstance(score_raw, dict)
                else None
            ),
            "period_remaining_days": (
                score_raw.get("period", {}).get("remainingDays")
                if isinstance(score_raw, dict)
                else None
            ),
        },
    }


def _platform_entities_snapshot(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> list[dict[str, Any]]:
    """Snapshot of live HA entity state, availability, and enabled status.

    Covers all sensor and binary_sensor platforms for this config entry so it's
    easy to see which sensors are showing Unknown/unavailable and why.
    """
    rows: list[dict[str, Any]] = []
    for platform in async_get_platforms(hass, "gecko"):
        if platform.domain not in ("sensor", "binary_sensor"):
            continue
        for ent in platform.entities.values():
            re = getattr(ent, "registry_entry", None)
            if re and re.config_entry_id != config_entry.entry_id:
                continue
            state_obj = hass.states.get(ent.entity_id) if ent.entity_id else None
            rows.append(
                {
                    "entity_id": ent.entity_id,
                    "platform": platform.domain,
                    "name": getattr(ent, "_attr_name", None)
                    or getattr(ent, "name", None),
                    "translation_key": getattr(ent, "_attr_translation_key", None),
                    "state": state_obj.state if state_obj else None,
                    "available": getattr(ent, "available", None),
                    "enabled": re.disabled_by is None if re else True,
                    "device_class": (
                        dc.value
                        if (dc := getattr(ent, "_attr_device_class", None))
                        else None
                    ),
                    "unit": getattr(ent, "_attr_native_unit_of_measurement", None),
                    "state_class": (
                        sc.value
                        if (sc := getattr(ent, "_attr_state_class", None))
                        else None
                    ),
                }
            )
    rows.sort(key=lambda r: (r["platform"], r["entity_id"] or ""))
    return rows


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
        "config_options": _config_entry_options(config_entry),
        "oauth_tokens": {
            "community": _oauth_token_diagnostics(
                config_entry.data.get("token"), label="community (HA OAuth)"
            ),
            "app_premium": _oauth_token_diagnostics(
                config_entry.data.get("app_token"), label="mobile app (premium REST)"
            ),
        },
        "vessels": _get_vessel_coordinators_diagnostics(config_entry),
        "connections": await _get_connection_diagnostics(connection_manager),
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
                "energy_keys_with_data": [k for k, v in ed.items() if v is not None],
                "energy_keys_parseable_for_sensors": parseable,
            }
            for ek in ("consumption", "cost", "score"):
                raw = ed.get(ek)
                if raw is not None:
                    vessel_energy[f"raw_{ek}"] = raw
            rate, rate_currency = extract_electricity_rate(ed.get("cost"))
            if rate is not None:
                vessel_energy["electricity_rate_per_kwh"] = rate
                vessel_energy["electricity_rate_currency"] = rate_currency
            vessel_energy["energy_parse_trace"] = _energy_parse_trace(ed)
            vessel_energy["sensor_values"] = _sensor_values_snapshot(coord)
            energy_summary.append(vessel_energy)
        diagnostics_data["runtime_data"] = {
            "api_client_type": type(getattr(rd, "api_client", None)).__name__,
            "coordinator_count": len(coordinators),
            "premium_energy_client": getattr(rd, "app_api_client", None) is not None,
            "energy_data_per_vessel": energy_summary,
        }

    # Live premium API probe — call all premium endpoints right now so the
    # diagnostics JSON always contains the freshest possible responses.
    if hasattr(config_entry, "runtime_data") and config_entry.runtime_data:
        rd = config_entry.runtime_data
        premium_api = getattr(rd, "app_api_client", None)
        community_api = getattr(rd, "api_client", None)
        account_id = str(config_entry.data.get("account_id", "")).strip()
        coordinators = getattr(rd, "coordinators", []) or []
        live_probes: list[dict[str, Any]] = []

        for coord in coordinators:
            vid = str(getattr(coord, "vessel_id", ""))
            probe: dict[str, Any] = {
                "vessel_id": vid,
                "monitor_id": getattr(coord, "monitor_id", None),
                "vessel_name": getattr(coord, "vessel_name", None),
            }

            # Premium (app-token) endpoints
            if premium_api and account_id:
                for label, method in (
                    ("energy_consumption", "async_get_energy_consumption"),
                    ("energy_score", "async_get_energy_score"),
                    ("energy_cost", "async_get_energy_cost"),
                ):
                    fn = getattr(premium_api, method, None)
                    if not callable(fn):
                        probe[label] = {"error": "method_not_found"}
                        continue
                    try:
                        probe[label] = await fn(account_id, vid)
                    except Exception as err:
                        probe[label] = {
                            "error": type(err).__name__,
                            "status": getattr(err, "status", None),
                            "message": str(err)[:300],
                        }
            else:
                probe["premium_api_available"] = False

            # Community-token endpoints
            if community_api and account_id:
                for label, method, args in (
                    ("vessel_detail_v6", "async_get_vessel_detail", (account_id, vid)),
                    (
                        "vessel_actions_v2",
                        "async_get_vessel_actions_v2",
                        (account_id, vid),
                    ),
                    (
                        "unread_messages",
                        "async_get_messages_unread",
                        (account_id,),
                    ),
                ):
                    fn = getattr(community_api, method, None)
                    if not callable(fn):
                        probe[label] = {"error": "method_not_found"}
                        continue
                    try:
                        probe[label] = await fn(*args)
                    except Exception as err:
                        probe[label] = {
                            "error": type(err).__name__,
                            "status": getattr(err, "status", None),
                            "message": str(err)[:300],
                        }

            live_probes.append(probe)

        if live_probes:
            diagnostics_data["live_api_probe"] = live_probes

    # Full MQTT shadow state + flow zone runtime for debugging pump/thermostat issues.
    shadow_dumps: list[dict[str, Any]] = []
    async with connection_manager._connection_lock:
        connections_snapshot = dict(connection_manager._connections)
    for monitor_id, connection in connections_snapshot.items():
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
            dump["desired_features"] = (
                desired.get("features") if isinstance(desired, dict) else None
            )
        zones = getattr(gc, "_zones", None)
        if isinstance(zones, dict):
            flow_runtime: list[dict[str, Any]] = []
            for zt, zlist in zones.items():
                for z in zlist:
                    zone_info: dict[str, Any] = {
                        "zone_type": zt.value if hasattr(zt, "value") else str(zt),
                        "id": getattr(z, "id", None),
                        "name": getattr(z, "name", None),
                        "active": getattr(z, "active", None),
                        "speed": getattr(z, "speed", None),
                        "target_temperature": getattr(z, "target_temperature", None),
                        "temperature": getattr(z, "temperature", None),
                        "set_point": getattr(z, "set_point", None),
                        "status": str(getattr(z, "status", None)),
                    }
                    initiators = getattr(z, "initiators", None)
                    if initiators is not None:
                        zone_info["initiators"] = [str(i) for i in initiators]
                    flow_runtime.append(zone_info)
            dump["zone_objects"] = flow_runtime
        shadow_dumps.append(dump)
    if shadow_dumps:
        diagnostics_data["mqtt_shadow_state"] = shadow_dumps

    # Climate entity internal state (pending setpoints, eco mode).
    climate_state: list[dict[str, Any]] = []
    for platform in async_get_platforms(hass, "gecko"):
        if platform.domain != "climate":
            continue
        for ent in platform.entities.values():
            if getattr(ent, "registry_entry", None) and (
                ent.registry_entry.config_entry_id != config_entry.entry_id
            ):
                continue
            climate_state.append(
                {
                    "entity_id": getattr(ent, "entity_id", None),
                    "pending_target_temperature": getattr(
                        ent, "_pending_target_temperature", None
                    ),
                    "pending_target_grace_remaining_s": _pending_grace_remaining(ent),
                    "target_temperature": getattr(
                        ent, "_attr_target_temperature", None
                    ),
                    "current_temperature": getattr(
                        ent, "_attr_current_temperature", None
                    ),
                }
            )
    if climate_state:
        diagnostics_data["climate_entities"] = climate_state

    entity_snapshot = _platform_entities_snapshot(hass, config_entry)
    if entity_snapshot:
        diagnostics_data["platform_entities_snapshot"] = entity_snapshot

    return diagnostics_data
