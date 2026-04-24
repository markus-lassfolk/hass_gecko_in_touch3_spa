"""Summarize REST ``spa_configuration`` for diagnostics (accessory ↔ zone IDs).

Mirrors the mapping idea from legacy in.touch3 integrations: which pump/light
IDs the cloud assigned to which flow/lighting zone at link time. IDs only —
no user-chosen names.
"""

from __future__ import annotations

from typing import Any

_MAX_MAPPED_IDS = 48


def summarize_spa_configuration_zones(
    spa_configuration: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return compact pump/light ↔ zone maps, or ``{"present": False}``."""
    if not isinstance(spa_configuration, dict) or not spa_configuration:
        return {"present": False}

    accessories = spa_configuration.get("accessories")
    zones = spa_configuration.get("zones")
    acc = accessories if isinstance(accessories, dict) else {}
    zr = zones if isinstance(zones, dict) else {}

    pump_to_flow: dict[str, str] = {}
    waterfall_to_flow: dict[str, str] = {}
    blower_to_flow: dict[str, str] = {}
    light_to_lighting: dict[str, str] = {}

    flow_zones = zr.get("flow")
    if isinstance(flow_zones, dict):

        def _fill(
            target: dict[str, str],
            zone_id: str,
            ids: Any,
        ) -> None:
            if not isinstance(ids, list):
                return
            for aid in ids:
                if len(target) >= _MAX_MAPPED_IDS:
                    return
                target[str(aid)] = zone_id

        for zone_id, zone_info in flow_zones.items():
            if not isinstance(zone_info, dict):
                continue
            zkey = str(zone_id)
            _fill(pump_to_flow, zkey, zone_info.get("pumps"))
            _fill(waterfall_to_flow, zkey, zone_info.get("waterfalls"))
            _fill(blower_to_flow, zkey, zone_info.get("blowers"))

    lighting_zones = zr.get("lighting")
    if isinstance(lighting_zones, dict):
        for zone_id, zone_info in lighting_zones.items():
            if len(light_to_lighting) >= _MAX_MAPPED_IDS:
                break
            if not isinstance(zone_info, dict):
                continue
            zkey = str(zone_id)
            lights = zone_info.get("lights")
            if not isinstance(lights, list):
                continue
            for lid in lights:
                if len(light_to_lighting) >= _MAX_MAPPED_IDS:
                    break
                light_to_lighting[str(lid)] = zkey

    def _count_map(d: dict[str, Any]) -> int:
        return len(d) if isinstance(d, dict) else 0

    return {
        "present": True,
        "accessory_counts": {
            "pumps": _count_map(acc.get("pumps")),
            "lights": _count_map(acc.get("lights")),
            "waterfalls": _count_map(acc.get("waterfalls")),
            "blowers": _count_map(acc.get("blowers")),
        },
        "pump_id_to_flow_zone_id": pump_to_flow,
        "waterfall_id_to_flow_zone_id": waterfall_to_flow,
        "blower_id_to_flow_zone_id": blower_to_flow,
        "light_id_to_lighting_zone_id": light_to_lighting,
    }
