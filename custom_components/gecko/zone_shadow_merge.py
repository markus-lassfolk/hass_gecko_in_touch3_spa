"""Patch gecko_iot_client zone shadow application so desired values are not dropped.

``ZoneConfigurationParser.apply_state_to_zones`` (gecko-iot-client 0.2.5) uses only
``state.reported.zones`` whenever it is non-empty. After a thermostat writes
``desired``, the next MQTT snapshot still carries the *old* setpoint under
``reported`` until the spa applies the change. Re-applying reported alone then
overwrites the in-memory setpoint and Home Assistant rubberbands back.

We shallow-merge ``desired.zones`` over ``reported.zones`` per zone id so fields
present in ``desired`` (for example ``setPoint``) win until reported catches up.

MQTT ``shadow/.../update/documents`` only forwards ``current.state`` to the client.
After a publish, AWS often clears ``desired`` in ``current`` while ``reported`` is
still stale, so we also merge ``delta.zones`` (when present) and patch the MQTT
transporter to fold ``previous.state.desired`` into ``current`` when ``current``
drops ``desired`` but the spa has not updated ``reported`` yet.
"""

from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

_PATCH_ATTR = "_gecko_ha_zone_shadow_merge_patch"
_DOC_PATCH_ATTR = "_gecko_ha_shadow_document_merge_patch"


def _shadow_zone_ids_equal(zone_id: object, other: object) -> bool:
    """Compare zone ids (int/str) without importing ``entity`` (avoids import cycles)."""
    if zone_id is None or other is None:
        return False
    try:
        return int(zone_id) == int(other)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return zone_id == other


def _normalize_merged_zone_runtime_state(
    zone_type_key: str, runtime: dict[str, Any]
) -> dict[str, Any]:
    """Align shadow keys Gecko reports vs what ``gecko_iot_client`` publishes.

    Flow zones publish ``active`` but many shadows still carry ``isActive`` under
    ``reported``. A shallow merge leaves both keys; if ``isActive`` is processed
    last, it can wipe a user ``active`` true and the pump rubberbands off.
    """
    out = dict(runtime)
    if zone_type_key == "flow":
        if "active" in out:
            out["isActive"] = out["active"]
        if "isActive" in out:
            out["active"] = out["isActive"]
    if zone_type_key == "temperatureControl":
        if "setpoint" in out and "setPoint" not in out:
            out["setPoint"] = out["setpoint"]
    return out


def merge_shadow_zone_trees(
    reported_zones: Any,
    desired_zones: Any,
) -> dict[str, dict[str, Any]]:
    """Build a ``zones`` tree: reported base with desired fields overlaid per zone."""
    rz = reported_zones if isinstance(reported_zones, dict) else {}
    dz = desired_zones if isinstance(desired_zones, dict) else {}
    if not rz:
        out: dict[str, dict[str, Any]] = {}
        for ztype_key, by_id in dz.items():
            if isinstance(by_id, dict):
                out[str(ztype_key)] = {
                    zid: dict(state) if isinstance(state, dict) else state
                    for zid, state in by_id.items()
                }
            else:
                out[str(ztype_key)] = {}
        return out
    merged: dict[str, dict[str, Any]] = {}
    for ztype_key, by_id in rz.items():
        if not isinstance(by_id, dict):
            continue
        merged[str(ztype_key)] = {
            zid: dict(state) if isinstance(state, dict) else state
            for zid, state in by_id.items()
        }
    if not dz:
        return merged
    for ztype_key, desired_by_id in dz.items():
        key = str(ztype_key)
        if not isinstance(desired_by_id, dict):
            continue
        if key not in merged:
            merged[key] = {
                zid: dict(state) if isinstance(state, dict) else state
                for zid, state in desired_by_id.items()
            }
            continue
        cur = merged[key]
        for zid, dstate in desired_by_id.items():
            if isinstance(dstate, dict):
                if zid in cur and isinstance(cur[zid], dict):
                    cur[zid] = {**cur[zid], **dstate}
                else:
                    cur[zid] = dict(dstate)
            elif dstate is not None:
                cur[zid] = dstate
    return merged


def merge_desired_shadow_layers(
    previous_desired: Any,
    current_desired: Any,
) -> dict[str, Any]:
    """Merge named-shadow ``desired`` dicts; ``current`` wins on conflicts.

    When both carry a ``zones`` tree, shallow-merge per zone id so partial
    ``current.desired`` updates do not wipe unrelated keys from ``previous``.
    """
    pd = previous_desired if isinstance(previous_desired, dict) else {}
    cd = current_desired if isinstance(current_desired, dict) else {}
    if not pd and not cd:
        return {}
    out: dict[str, Any] = {**pd, **cd}
    if pd.get("zones") or cd.get("zones"):
        out["zones"] = merge_shadow_zone_trees(pd.get("zones"), cd.get("zones"))
    return out


def enrich_document_current_state_with_previous_desired(
    current_state: dict[str, Any],
    previous_state: dict[str, Any],
) -> dict[str, Any]:
    """Rebuild ``current.state`` so zone merges still see a meaningful ``desired``."""
    out = dict(current_state)
    prev_st = previous_state if isinstance(previous_state, dict) else {}
    merged_desired = merge_desired_shadow_layers(
        prev_st.get("desired"),
        out.get("desired"),
    )
    if merged_desired:
        out["desired"] = merged_desired
    return out


def install_mqtt_shadow_document_patch() -> None:
    """Fold ``previous.desired`` into document ``current.state`` before zone updates."""
    from gecko_iot_client.transporters.mqtt.transporter import MqttTransporter
    from gecko_iot_client.transporters.mqtt.utils import (
        notify_callbacks_safely,
        parse_json_safely,
    )

    if getattr(MqttTransporter._on_state_document_update, _DOC_PATCH_ATTR, False):
        return

    lib_logger = logging.getLogger("gecko_iot_client.transporters.mqtt.transporter")

    def _on_state_document_update(
        self: MqttTransporter, topic: str, payload: str
    ) -> None:
        lib_logger.debug("State document update received")
        document = parse_json_safely(payload)
        if not document:
            lib_logger.error("Failed to parse state document update")
            return
        current_inner = document.get("current", {}).get("state", {}) or {}
        previous_inner = document.get("previous", {}).get("state", {}) or {}
        enriched = enrich_document_current_state_with_previous_desired(
            current_inner if isinstance(current_inner, dict) else {},
            previous_inner if isinstance(previous_inner, dict) else {},
        )
        lib_logger.debug("Extracted state from document (HA desired carry-forward)")
        notify_callbacks_safely(
            self._callback_registry.get_callbacks("state_update"),
            {"state": enriched},
        )

    setattr(_on_state_document_update, _DOC_PATCH_ATTR, True)
    MqttTransporter._on_state_document_update = _on_state_document_update  # type: ignore[method-assign]
    _LOGGER.debug("Installed MQTT shadow document desired carry-forward patch")


def install_zone_parser_merge_patch() -> None:
    """Idempotently replace ``apply_state_to_zones`` on the vendored parser class."""
    from gecko_iot_client.models.zone_parser import ZoneConfigurationParser

    existing = getattr(ZoneConfigurationParser.apply_state_to_zones, _PATCH_ATTR, False)
    if existing:
        return

    lib_logger = logging.getLogger("gecko_iot_client.models.zone_parser")

    def apply_state_to_zones(
        self: ZoneConfigurationParser,
        zones: dict[Any, list[Any]],
        state_data: dict[str, Any],
    ) -> None:
        lib_logger.debug("Applying runtime state data to zones")
        state = state_data.get("state") or {}
        reported_state = state.get("reported") or {}
        desired_state = state.get("desired") or {}
        delta_state = state.get("delta") or {}
        zones_state = merge_shadow_zone_trees(
            merge_shadow_zone_trees(
                reported_state.get("zones"),
                desired_state.get("zones"),
            ),
            delta_state.get("zones"),
        )
        if not zones_state:
            lib_logger.debug("No zones runtime state found")
            return

        lib_logger.debug(
            "Found zones state data with %d zone type(s)", len(zones_state)
        )

        updated_count = 0
        for zone_type_key, zones_of_type_state in zones_state.items():
            zone_type = self.ZONE_TYPES.get(zone_type_key)
            if not zone_type:
                lib_logger.warning("Unknown zone type in state data: %s", zone_type_key)
                continue

            if zone_type not in zones:
                lib_logger.warning(
                    "Zone type %s found in state but no zones of this type exist",
                    zone_type_key,
                )
                continue

            zone_list = zones[zone_type]
            lib_logger.debug(
                "Processing %d zone(s) of type %s",
                len(zones_of_type_state),
                zone_type_key,
            )

            for zone_id, zone_runtime_state in zones_of_type_state.items():
                zone = next(
                    (
                        z
                        for z in zone_list
                        if _shadow_zone_ids_equal(getattr(z, "id", None), zone_id)
                    ),
                    None,
                )
                if zone:
                    try:
                        st = (
                            _normalize_merged_zone_runtime_state(
                                zone_type_key, dict(zone_runtime_state)
                            )
                            if isinstance(zone_runtime_state, dict)
                            else zone_runtime_state
                        )
                        zone.update_from_state(st)
                        lib_logger.debug(
                            "Updated zone %s of type %s with state: %s",
                            zone_id,
                            zone_type_key,
                            zone_runtime_state,
                        )
                        updated_count += 1
                    except Exception as e:
                        lib_logger.warning(
                            "Failed to update zone %s from state: %s", zone_id, e
                        )
                else:
                    lib_logger.warning(
                        "Zone %s of type %s found in state but not in configured zones",
                        zone_id,
                        zone_type_key,
                    )

        lib_logger.debug("Applied runtime state to %d zones", updated_count)

    setattr(apply_state_to_zones, _PATCH_ATTR, True)
    ZoneConfigurationParser.apply_state_to_zones = apply_state_to_zones  # type: ignore[method-assign]
    _LOGGER.debug("Installed gecko_iot_client zone shadow merge patch")
