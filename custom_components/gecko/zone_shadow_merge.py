"""Patch gecko_iot_client zone shadow application so desired values are not dropped.

``ZoneConfigurationParser.apply_state_to_zones`` (gecko-iot-client 0.2.5) uses only
``state.reported.zones`` whenever it is non-empty. After a thermostat writes
``desired``, the next MQTT snapshot still carries the *old* setpoint under
``reported`` until the spa applies the change. Re-applying reported alone then
overwrites the in-memory setpoint and Home Assistant rubberbands back.

We shallow-merge ``desired.zones`` over ``reported.zones`` per zone id so fields
present in ``desired`` (for example ``setPoint``) win until reported catches up.
"""

from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

_PATCH_ATTR = "_gecko_ha_zone_shadow_merge_patch"


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


def install_zone_parser_merge_patch() -> None:
    """Idempotently replace ``apply_state_to_zones`` on the vendored parser class."""
    from gecko_iot_client.models.zone_parser import ZoneConfigurationParser

    existing = getattr(
        ZoneConfigurationParser.apply_state_to_zones, _PATCH_ATTR, False
    )
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
        zones_state = merge_shadow_zone_trees(
            reported_state.get("zones"),
            desired_state.get("zones"),
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
                zone = next((z for z in zone_list if z.id == zone_id), None)
                if zone:
                    try:
                        zone.update_from_state(zone_runtime_state)
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
