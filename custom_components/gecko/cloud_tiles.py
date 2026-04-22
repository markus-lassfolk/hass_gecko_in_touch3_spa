"""Parse app-style vessel summary fields from Gecko REST.

Vessel list/detail payloads vary by API version; this module reads common
numeric, string, and shallow boolean shapes used for dashboard tiles while
avoiding identifiers where possible. Keys are written under ``cloud.rest.*``
so they merge cleanly with MQTT shadow metrics (shadow wins on path
collision).
"""

from __future__ import annotations

import math
from typing import Any


def _num(v: Any) -> float | int | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    return None


def _status_dict(vessel: dict[str, Any]) -> dict[str, Any]:
    st = vessel.get("status")
    if isinstance(st, dict):
        return st
    return {}


def _disc_elements(status: dict[str, Any]) -> dict[str, Any]:
    for key in ("discElements", "disc_elements"):
        de = status.get(key)
        if isinstance(de, dict):
            return de
    return {}


def _ph_value(disc: dict[str, Any], status: dict[str, Any]) -> float | int | None:
    for root in (disc, status):
        ph = root.get("phStatus")
        if ph is None:
            ph = root.get("ph_status")
        if isinstance(ph, dict):
            for leaf in ("value", "ph", "reading", "current"):
                n = _num(ph.get(leaf))
                if n is not None:
                    return n
        n = _num(ph) if not isinstance(ph, dict) else None
        if n is not None:
            return n
    return None


def _orp_mv(disc: dict[str, Any], status: dict[str, Any]) -> float | int | None:
    for root in (disc, status):
        orp = root.get("orpStatus")
        if orp is None:
            orp = root.get("orp_status")
        if isinstance(orp, dict):
            for leaf in ("value", "orp", "reading", "current", "mv"):
                n = _num(orp.get(leaf))
                if n is not None:
                    return n
        n = _num(orp) if not isinstance(orp, dict) else None
        if n is not None:
            return n
    return None


def _temp_c(disc: dict[str, Any], status: dict[str, Any]) -> float | int | None:
    for root in (disc, status):
        if not isinstance(root, dict):
            continue
        for key in ("tempC", "temp_c", "temperatureC", "temperature"):
            n = _num(root.get(key))
            if n is not None:
                return n
    return None


def _string_leaf(v: Any) -> str | None:
    if isinstance(v, str) and v.strip():
        s = v.strip()
        if len(s) > 256:
            return None
        if s.startswith("eyJ"):
            return None
        return s
    return None


def extract_cloud_tile_strings(vessel: dict[str, Any]) -> dict[str, str]:
    """Human-readable REST fields for text sensors (``cloud.rest.*``)."""
    out: dict[str, str] = {}
    if not isinstance(vessel, dict):
        return out
    status = _status_dict(vessel)
    disc = _disc_elements(status)

    for label, root in (("disc", disc), ("status", status)):
        if not isinstance(root, dict):
            continue
        for key in (
            "waterStatus",
            "water_status",
            "flowStatus",
            "flow_status",
            "statusText",
            "status_text",
            "message",
            "text",
        ):
            raw = root.get(key)
            if isinstance(raw, dict):
                for leaf in ("text", "message", "value", "name", "label", "description"):
                    s = _string_leaf(raw.get(leaf))
                    if s:
                        out[f"cloud.rest.{label}.{key}.{leaf}"] = s
                        break
            else:
                s = _string_leaf(raw)
                if s:
                    out[f"cloud.rest.{label}.{key}"] = s

    return out


def extract_cloud_tile_booleans(vessel: dict[str, Any]) -> dict[str, bool]:
    """Boolean leaves from REST status / disc tiles (``cloud.rest.*``; MQTT shadow wins on overlap)."""
    out: dict[str, bool] = {}
    if not isinstance(vessel, dict):
        return out

    def add_bools(base: str, root: dict[str, Any]) -> None:
        if not isinstance(root, dict):
            return
        for key, val in root.items():
            if not isinstance(key, str):
                continue
            if isinstance(val, bool):
                out[f"{base}.{key}"] = val
            elif isinstance(val, dict):
                for k2, v2 in val.items():
                    if isinstance(k2, str) and isinstance(v2, bool):
                        out[f"{base}.{key}.{k2}"] = v2

    status = _status_dict(vessel)
    disc = _disc_elements(status)
    add_bools("cloud.rest.status", status)
    add_bools("cloud.rest.disc_elements", disc)
    return out


def extract_cloud_tile_metrics(vessel: dict[str, Any]) -> dict[str, float | int]:
    """Return dotted metric paths for REST-derived tile values."""
    out: dict[str, float | int] = {}
    if not isinstance(vessel, dict):
        return out

    status = _status_dict(vessel)
    disc = _disc_elements(status)

    tc = _temp_c(disc, status)
    if tc is not None:
        out["cloud.rest.disc_elements.temp_c"] = tc

    ph = _ph_value(disc, status)
    if ph is not None:
        out["cloud.rest.summary.ph"] = ph

    orp = _orp_mv(disc, status)
    if orp is not None:
        out["cloud.rest.summary.orp_mv"] = orp

    return out


def find_vessel_record(
    vessels: list[dict[str, Any]], vessel_id: str | int
) -> dict[str, Any] | None:
    """Pick the vessel dict for ``vessel_id`` from an account vessels list."""
    vid = str(vessel_id)
    for v in vessels:
        if not isinstance(v, dict):
            continue
        if str(v.get("vesselId", "")) == vid:
            return v
        if str(v.get("id", "")) == vid:
            return v
        if str(v.get("vessel_id", "")) == vid:
            return v
    return None
