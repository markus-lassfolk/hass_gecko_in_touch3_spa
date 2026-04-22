"""Extract water-quality and other metrics from Gecko IoT device shadow state.

The official gecko-iot-client only models flow, lighting, and temperatureControl
zones. Waterlab and similar data typically appear under other ``zones.*`` keys or
nested under ``features``. This module walks those branches and collects numeric
leaves for Home Assistant sensors, without hard-coding unpublished field names.

**Observed live shadow (Waterlab):** chemistry hardware often exposes calibration and
model parameters under ``reported.features.waterlab.sensor`` — e.g. ``ph`` and
``orp`` subtrees with ``offsetMv*``, ``slopeMvPerPh``, and ``therm`` with ``R0`` /
``T0`` / ``beta`` (thermistor constants). Those are **not** live pH/ORP/temperature
readings; heuristics below treat them as diagnostics so we do not assign PH/ORP device
classes or enable them as default water-chemistry sensors. Actual readings, when
present, tend to use different path shapes (e.g. measured values outside ``sensor``
calibration leaves).
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from homeassistant.const import UnitOfTemperature

# Zone families handled by gecko-iot-client (avoid duplicating climate/pumps/lights).
KNOWN_ZONE_TYPES = frozenset({"flow", "lighting", "temperatureControl"})

_MAX_DEPTH = 16
_MAX_SENSORS = 160


def _path_segments(path: str) -> list[str]:
    """Lowercased path segments (split on ``.``, ``_``, ``-``)."""
    return [s for s in re.split(r"[._-]+", path.lower()) if s]


def _segment_is_ph(seg: str) -> bool:
    """True if segment denotes pH (not ``phase``, ``graph``, or ``alpha`` substrings)."""
    return seg == "ph"


def _is_calibration_or_model_param_path(path: str) -> bool:
    """True when the path is sensor calibration / thermistor model data, not a live reading.

    Derived from production shadow samples under ``features.waterlab.sensor.*`` where
    ``ph`` / ``orp`` leaves are offset/slope in mV, and ``therm`` holds R0/T0/beta.
    """
    lower = path.lower()
    # Millivolt offsets and slopes (Waterlab pH/ORP sensor calibration).
    if "offsetmv" in lower or "slopemv" in lower or "mvperph" in lower or "mvatph" in lower:
        return True
    # Thermistor / NTC model parameters (not spa water temperature).
    if re.search(r"\.therm\.(r0|t0|beta)(\.|$)", lower):
        return True
    return False


def _skip_extension_reported_root_key(key: str) -> bool:
    """Skip connectivity-style noise that can crowd out chemistry metrics."""
    if key in ("zones", "features"):
        return True
    lk = key.lower()
    return lk == "connectivity_" or lk.startswith("connectivity")


def _get_reported(state_data: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize shadow payload to the ``reported`` object."""
    if not state_data or not isinstance(state_data, dict):
        return {}
    inner = state_data.get("state")
    if isinstance(inner, dict):
        reported = inner.get("reported")
        if isinstance(reported, dict):
            return reported
    reported = state_data.get("reported")
    if isinstance(reported, dict):
        return reported
    return {}


def _flatten_numeric(
    obj: Any,
    prefix: str,
    out: dict[str, float | int],
    depth: int,
) -> None:
    """Append numeric leaves to ``out`` keyed by dotted path."""
    if depth > _MAX_DEPTH or len(out) >= _MAX_SENSORS:
        return
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            return
        out[prefix] = obj
        return
    if not isinstance(obj, dict):
        return
    for key, val in obj.items():
        if not isinstance(key, str):
            continue
        path = f"{prefix}.{key}" if prefix else key
        _flatten_numeric(val, path, out, depth + 1)


def extract_extension_metrics(
    state_data: dict[str, Any] | None,
) -> dict[str, float | int]:
    """Return path -> numeric value for unknown zone types and non-mode features."""
    reported = _get_reported(state_data)
    if not reported:
        return {}

    out: dict[str, float | int] = {}

    zones = reported.get("zones")
    if isinstance(zones, dict):
        for zone_type, zone_bundle in zones.items():
            if not isinstance(zone_type, str) or not isinstance(zone_bundle, dict):
                continue
            if zone_type in KNOWN_ZONE_TYPES:
                continue
            for zone_id, zone_state in zone_bundle.items():
                if not isinstance(zone_id, str):
                    continue
                base = f"zones.{zone_type}.{zone_id}"
                _flatten_numeric(zone_state, base, out, 0)

    features = reported.get("features")
    if isinstance(features, dict):
        for feat_key, feat_val in features.items():
            if feat_key == "operationMode":
                continue
            if not isinstance(feat_key, str):
                continue
            _flatten_numeric(feat_val, f"features.{feat_key}", out, 0)

    for root_key, root_val in reported.items():
        if _skip_extension_reported_root_key(root_key):
            continue
        if not isinstance(root_key, str):
            continue
        _flatten_numeric(root_val, root_key, out, 0)

    return out


def shadow_topology_summary(state_data: dict[str, Any] | None) -> dict[str, Any]:
    """Redacted structural summary for diagnostics (no large leaf values)."""
    reported = _get_reported(state_data)
    if not reported:
        return {"reported_top_level_keys": []}

    summary: dict[str, Any] = {
        "reported_top_level_keys": sorted(reported.keys()),
    }

    zones = reported.get("zones")
    if isinstance(zones, dict):
        summary["zones_zone_type_keys"] = sorted(zones.keys())
        unknown = sorted(
            k for k in zones if isinstance(k, str) and k not in KNOWN_ZONE_TYPES
        )
        summary["zones_unknown_types"] = unknown
        for zt in unknown[:8]:
            zb = zones.get(zt)
            if isinstance(zb, dict):
                summary[f"zones.{zt}_zone_ids"] = sorted(zb.keys())[:16]

    features = reported.get("features")
    if isinstance(features, dict):
        summary["features_keys"] = sorted(features.keys())

    return summary


def metric_path_to_entity_slug(path: str, max_len: int = 48) -> str:
    """Turn a metric path into a safe unique entity name suffix.

    When truncated, append a short hash of the full path so long paths that
    share a prefix do not collide on ``entity_id``.
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_").lower()
    if not slug:
        slug = "metric"
    if len(slug) > max_len:
        digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:8]
        keep = max_len - len(digest) - 1
        if keep < 1:
            keep = 1
        slug = f"{slug[:keep]}_{digest}"
    return slug


def infer_sensor_metadata(path: str) -> tuple[str | None, str | None]:
    """Return (device_class, native_unit_of_measurement) heuristics from path."""
    lower = path.lower()
    device_class: str | None = None
    unit: str | None = None

    if _is_calibration_or_model_param_path(path):
        return None, None

    segs = _path_segments(path)
    if any(_segment_is_ph(s) for s in segs):
        device_class = "ph"
    elif any(s == "orp" or s.startswith("orp_") for s in segs) or re.search(
        r"\b(oxidation|redox|eh)\b", lower
    ):
        # Millivolts are not HA ``SensorDeviceClass.VOLTAGE`` (SI volts).
        unit = "mV"
    elif (
        "temperature" in lower
        or lower.endswith("_temp")
        or "temp_" in lower
        or re.search(r"\btemp\b", lower)
    ):
        device_class = "temperature"
        unit = UnitOfTemperature.CELSIUS
    elif re.search(r"\b(chlorine|bromine|sanitizer)\b", lower):
        unit = "ppm"
    elif re.search(r"\b(tds|salinity)\b", lower):
        unit = "ppm"
    elif re.search(
        r"\b(alkalinity|hardness|calcium|cyanuric|bromide|turbidity|conductivity)\b",
        lower,
    ):
        unit = "ppm"
    elif "uv" in lower and re.search(r"\b(intensity|dose|power)\b", lower):
        unit = "%"

    return device_class, unit


def chemistry_metric_enabled_by_default(path: str) -> bool:
    """Whether a shadow path is likely water chemistry and safe to enable by default."""
    lower = path.lower()
    if _is_calibration_or_model_param_path(path):
        return False

    segs = _path_segments(path)
    if any(_segment_is_ph(s) for s in segs):
        return True
    if any(s == "orp" or s.startswith("orp_") for s in segs) or re.search(
        r"\b(oxidation|redox|eh)\b", lower
    ):
        return True
    chem_tokens = frozenset(
        {
            "chlorine",
            "bromine",
            "salinity",
            "tds",
            "sanitizer",
            "alkalinity",
            "hardness",
            "calcium",
            "cyanuric",
            "bromide",
            "turbidity",
            "conductivity",
        }
    )
    if chem_tokens.intersection(segs):
        return True
    # Do not enable every numeric under waterlab (calibration, RF, etc.): segment match only.
    if "waterlab" in segs:
        return True
    if lower.startswith("cloud.rest.") and any(
        tail in lower for tail in (".ph", ".orp", "orp_mv", "temp_c", "disc_elements.")
    ):
        return True
    return False
