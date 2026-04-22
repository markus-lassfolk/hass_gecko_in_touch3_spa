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

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfTemperature

# Zone families handled by gecko-iot-client (avoid duplicating climate/pumps/lights).
KNOWN_ZONE_TYPES = frozenset({"flow", "lighting", "temperatureControl"})

_MAX_DEPTH = 16
_MAX_SENSORS = 192
_MAX_BOOLEANS = 128
_MAX_STRINGS = 128

_UNKNOWN_ZONE_SETPOINT_RE = re.compile(
    r"^zones\.(?P<zt>[^.]+)\.(?P<zid>[^.]+)\.(?P<leaf>[^.]+)$"
)
_SETPOINT_LEAF_RE = re.compile(
    r"(setpoint|set_point|targettemp|target_temp|targettemperature|goal|sp)$",
    re.IGNORECASE,
)


def path_reserved_for_number_control(path: str) -> bool:
    """True for single-leaf unknown-zone paths that get a Number entity instead of Sensor."""
    m = _UNKNOWN_ZONE_SETPOINT_RE.match(path)
    if not m:
        return False
    zt = m.group("zt")
    if zt in KNOWN_ZONE_TYPES:
        return False
    return bool(_SETPOINT_LEAF_RE.search(m.group("leaf")))


def _path_looks_sensitive(path: str) -> bool:
    lower = path.lower()
    return any(
        tok in lower
        for tok in ("password", "secret", "token", "credential", "ssid", "email")
    )


def _string_value_ok(s: str) -> bool:
    if not s or len(s) > 256:
        return False
    if s.startswith("eyJ"):
        return False
    return True


def _path_segments(path: str) -> list[str]:
    """Lowercased path segments (split on ``.``, ``_``, ``-``)."""
    return [s for s in re.split(r"[._-]+", path.lower()) if s]


_PH_FALSE_POSITIVE_SEGMENTS = frozenset(
    {
        "phase",
        "phone",
        "photo",
        "phantom",
        "phosphate",
        "photon",
        "physical",
        "phonetic",
        "phoning",
        "phoney",
        "phosphor",
    }
)

_ORP_FALSE_POSITIVE_SEGMENTS = frozenset({"orphan", "orphaned"})


def _segment_is_ph(seg: str) -> bool:
    """True if segment denotes pH (handles camelCase keys like ``phValue`` → ``phvalue``)."""
    if seg == "ph":
        return True
    if not seg.startswith("ph"):
        return False
    if seg in _PH_FALSE_POSITIVE_SEGMENTS:
        return False
    return bool(re.fullmatch(r"ph[a-z0-9]+", seg))


def _segment_is_orp(seg: str) -> bool:
    """True for ORP tokens including ``orpValue`` / ``orpmv`` style segments."""
    if seg == "orp":
        return True
    if not seg.startswith("orp"):
        return False
    if seg in _ORP_FALSE_POSITIVE_SEGMENTS:
        return False
    return bool(re.fullmatch(r"orp[a-z0-9]+", seg))


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


def _flatten_bool(
    obj: Any,
    prefix: str,
    out: dict[str, bool],
    depth: int,
) -> None:
    if depth > _MAX_DEPTH or len(out) >= _MAX_BOOLEANS:
        return
    if isinstance(obj, bool):
        out[prefix] = obj
        return
    if not isinstance(obj, dict):
        return
    for key, val in obj.items():
        if not isinstance(key, str):
            continue
        path = f"{prefix}.{key}" if prefix else key
        _flatten_bool(val, path, out, depth + 1)


def _flatten_string(
    obj: Any,
    prefix: str,
    out: dict[str, str],
    depth: int,
) -> None:
    if depth > _MAX_DEPTH or len(out) >= _MAX_STRINGS:
        return
    if isinstance(obj, str):
        if _string_value_ok(obj) and not _path_looks_sensitive(prefix):
            out[prefix] = obj
        return
    if not isinstance(obj, dict):
        return
    for key, val in obj.items():
        if not isinstance(key, str):
            continue
        path = f"{prefix}.{key}" if prefix else key
        _flatten_string(val, path, out, depth + 1)


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
            if not isinstance(feat_key, str):
                continue
            _flatten_numeric(feat_val, f"features.{feat_key}", out, 0)

    # Do not flatten arbitrary other top-level ``reported`` keys: firmware counters,
    # timestamps, and vendor metadata would become misleading "measurement" sensors.

    # Top-level connectivity / RF-style roots (skipped above to avoid crowding chemistry).
    for root_key, root_val in reported.items():
        if not isinstance(root_key, str):
            continue
        lk = root_key.lower()
        if lk == "connectivity" or lk.startswith("connectivity"):
            _flatten_numeric(root_val, root_key, out, 0)

    return out


def _iter_extension_bases(
    state_data: dict[str, Any] | None,
) -> list[tuple[str, Any]]:
    """(prefix, subtree) pairs matching extension numeric coverage (bool/string)."""
    reported = _get_reported(state_data)
    if not reported:
        return []
    pairs: list[tuple[str, Any]] = []
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
                pairs.append((f"zones.{zone_type}.{zone_id}", zone_state))
    features = reported.get("features")
    if isinstance(features, dict):
        for feat_key, feat_val in features.items():
            if not isinstance(feat_key, str):
                continue
            pairs.append((f"features.{feat_key}", feat_val))
    for root_key, root_val in reported.items():
        if not isinstance(root_key, str):
            continue
        lk = root_key.lower()
        if lk == "connectivity" or lk.startswith("connectivity"):
            pairs.append((root_key, root_val))

    return pairs


def extract_extension_booleans(
    state_data: dict[str, Any] | None,
) -> dict[str, bool]:
    """Boolean leaves under unknown zones, features, and other reported roots."""
    out: dict[str, bool] = {}
    for base, obj in _iter_extension_bases(state_data):
        if _path_looks_sensitive(base):
            continue
        _flatten_bool(obj, base, out, 0)
    return out


def extract_extension_strings(
    state_data: dict[str, Any] | None,
) -> dict[str, str]:
    """String leaves (short, non-sensitive paths) for text sensors."""
    out: dict[str, str] = {}
    for base, obj in _iter_extension_bases(state_data):
        if _path_looks_sensitive(base):
            continue
        _flatten_string(obj, base, out, 0)
    # Watercare mode is already a Select entity; skip duplicate strings.
    return {
        k: v
        for k, v in out.items()
        if not k.lower().startswith("features.operationmode")
    }


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


def infer_sensor_metadata(
    path: str,
) -> tuple[SensorDeviceClass | None, str | None]:
    """Return (``SensorDeviceClass``, unit) heuristics from dotted shadow path."""
    lower = path.lower()
    device_class: SensorDeviceClass | None = None
    unit: str | None = None

    if _is_calibration_or_model_param_path(path):
        return None, None

    segs = _path_segments(path)
    if any(_segment_is_ph(s) for s in segs):
        device_class = SensorDeviceClass.PH
    elif any(_segment_is_orp(s) for s in segs) or re.search(
        r"\b(oxidation|redox|eh)\b", lower
    ):
        unit = "mV"
    elif (
        "temperature" in lower
        or lower.endswith("_temp")
        or "temp_" in lower
        or re.search(r"\btemp\b", lower)
    ):
        device_class = SensorDeviceClass.TEMPERATURE
        unit = UnitOfTemperature.CELSIUS
    elif re.search(r"\b(humidity|rh)\b", lower):
        device_class = SensorDeviceClass.HUMIDITY
        unit = "%"
    elif re.search(r"\b(moisture)\b", lower):
        device_class = SensorDeviceClass.MOISTURE
        unit = "%"
    elif re.search(r"\b(voltage|volt)\b", lower) and "orp" not in lower:
        device_class = SensorDeviceClass.VOLTAGE
        unit = "V"
    elif re.search(r"\b(current|amperage|amp)\b", lower):
        device_class = SensorDeviceClass.CURRENT
        unit = "A"
    elif re.search(r"\b(power|watt)\b", lower):
        device_class = SensorDeviceClass.POWER
        unit = "W"
    elif re.search(r"\b(energy|kwh|kilowatt)\b", lower):
        device_class = SensorDeviceClass.ENERGY
        unit = "kWh"
    elif re.search(r"\b(frequency|hz)\b", lower):
        device_class = SensorDeviceClass.FREQUENCY
        unit = "Hz"
    elif re.search(r"\b(pressure|psi|bar)\b", lower):
        device_class = SensorDeviceClass.PRESSURE
        unit = "psi" if "psi" in lower else "bar" if "bar" in lower else None
    elif re.search(r"\b(flow|gpm|lpm)\b", lower):
        vfr = getattr(SensorDeviceClass, "VOLUME_FLOW_RATE", None)
        if vfr is not None:
            device_class = vfr
        unit = "gal/min" if "gpm" in lower or "gal" in lower else "L/min"
    elif re.search(r"\b(conductivity|microsiemens|µs|us/cm)\b", lower):
        cond_dc = getattr(SensorDeviceClass, "CONDUCTIVITY", None)
        if cond_dc is not None:
            device_class = cond_dc
        unit = "µS/cm"
    elif re.search(r"\b(tds|salinity)\b", lower):
        tds_dc = getattr(SensorDeviceClass, "TDS", None)
        device_class = tds_dc if tds_dc is not None else None
        unit = "ppm" if device_class is None else None
    elif re.search(r"\b(chlorine|bromine|sanitizer)\b", lower):
        unit = "ppm"
    elif re.search(
        r"\b(alkalinity|hardness|calcium|cyanuric|bromide|turbidity)\b",
        lower,
    ):
        unit = "ppm"
    elif "uv" in lower and re.search(r"\b(intensity|dose|power)\b", lower):
        unit = "%"
    elif re.search(r"\b(duration|runtime|uptime)\b", lower) and (
        "second" in lower or "sec" in lower or "min" in lower
    ):
        dur_dc = getattr(SensorDeviceClass, "DURATION", None)
        if dur_dc is not None:
            device_class = dur_dc
        unit = "s" if "second" in lower or lower.endswith("sec") else "min"

    return device_class, unit


def _is_rf_diagnostic_path(path: str) -> bool:
    """RF / radio link diagnostics (not live chemistry readings)."""
    lower = path.lower()
    if "waterlab" in lower and "rf" in lower:
        return True
    if re.search(
        r"\b(rssi|lqi|snr|linkquality|spreadingfactor|sf\d|ewma|duty\s*cycle|crc)\b",
        lower,
    ):
        return True
    if re.search(r"\b(rf|radio)\b", lower) and re.search(
        r"\b(signal|strength|noise|channel|power)\b", lower
    ):
        return True
    return False


def _is_connectivity_shadow_metric_path(path: str) -> bool:
    """Paths under reported ``connectivity`` roots (top-level or nested, e.g. ``features``)."""
    lower = path.lower()
    if lower.startswith("connectivity"):
        return True
    return bool(re.search(r"(^|\.)(connectivity)(\.|$)", lower))


def shadow_extension_diagnostic_disables_registry_default(path: str) -> bool:
    """When True: keep entity under *Diagnostics* and *disabled by default* in the registry."""
    if _is_calibration_or_model_param_path(path):
        return True
    if _is_rf_diagnostic_path(path):
        return True
    if _is_connectivity_shadow_metric_path(path):
        return True
    lower = path.lower()
    if "waterlab" in lower and re.search(
        r"\b(factory|firmware|diag|debug|internal|raw|nvram|eeprom)\b",
        lower,
    ):
        return True
    return False


def classify_gecko_shadow_metric(path: str) -> str:
    """Bucket for icons / ``extra_state_attributes`` (developer-facing, not PII)."""
    if _is_calibration_or_model_param_path(path):
        return "calibration_model"
    if _is_rf_diagnostic_path(path):
        return "rf"
    if _is_connectivity_shadow_metric_path(path):
        return "connectivity"
    if chemistry_metric_enabled_by_default(path):
        return "chemistry_live"
    lower = path.lower()
    if "waterlab" in lower or re.search(
        r"\b(chlorine|bromine|orp|ph|tds|sanitizer|salinity|alkalinity)\b", lower
    ):
        return "chemistry_other"
    return "other"


def shadow_metric_icon(path: str) -> str:
    """Suggested MDI icon by Gecko metric bucket."""
    bucket = classify_gecko_shadow_metric(path)
    return {
        "calibration_model": "mdi:wrench-cog",
        "rf": "mdi:radio-tower",
        "connectivity": "mdi:access-point-network",
        "chemistry_live": "mdi:water-opacity",
        "chemistry_other": "mdi:flask",
        "other": "mdi:gauge",
    }.get(bucket, "mdi:gauge")


def apply_numeric_shadow_sensor_hints(entity: Any, path: str) -> None:
    """Configure a ``SensorEntity`` from ``infer_sensor_metadata``."""
    dc, unit = infer_sensor_metadata(path)
    entity._attr_native_unit_of_measurement = unit
    entity._attr_device_class = dc
    if dc == SensorDeviceClass.PH:
        entity._attr_state_class = SensorStateClass.MEASUREMENT
        entity._attr_suggested_display_precision = 2
    elif dc == SensorDeviceClass.TEMPERATURE:
        entity._attr_state_class = SensorStateClass.MEASUREMENT
    elif dc == SensorDeviceClass.ENERGY:
        entity._attr_state_class = SensorStateClass.TOTAL_INCREASING
    elif dc is not None:
        entity._attr_state_class = SensorStateClass.MEASUREMENT
    else:
        entity._attr_state_class = SensorStateClass.MEASUREMENT


def chemistry_metric_enabled_by_default(path: str) -> bool:
    """Whether a shadow path is likely water chemistry and safe to enable by default."""
    lower = path.lower()
    if shadow_extension_diagnostic_disables_registry_default(path):
        return False
    if _is_calibration_or_model_param_path(path):
        return False
    if "operationmode" in lower:
        return False

    segs = _path_segments(path)
    if any(_segment_is_ph(s) for s in segs):
        return True
    if any(_segment_is_orp(s) for s in segs) or re.search(
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
    if re.search(
        r"\b(chlorine|bromine|salinity|tds|sanitizer|alkalinity|hardness|calcium|cyanuric|bromide|turbidity|conductivity)\b",
        lower,
    ):
        return True
    if lower.startswith("cloud.rest.") and any(
        tail in lower for tail in (".ph", ".orp", "orp_mv", "temp_c", "disc_elements.")
    ):
        return True
    return False


def parse_unknown_zone_setpoint_path(path: str) -> tuple[str, str, str] | None:
    """If ``path`` is an unknown-zone setpoint leaf, return (zone_type, zone_id, field_key)."""
    m = _UNKNOWN_ZONE_SETPOINT_RE.match(path)
    if not m:
        return None
    zt, zid, leaf = m.group("zt"), m.group("zid"), m.group("leaf")
    if zt in KNOWN_ZONE_TYPES:
        return None
    if not _SETPOINT_LEAF_RE.search(leaf):
        return None
    return zt, zid, leaf


def infer_number_setpoint_limits(path: str, leaf: str) -> tuple[float, float, float]:
    """Return (native_min, native_max, step) for unknown-zone setpoint numbers."""
    lower = path.lower()
    lk = leaf.lower()
    if "ph" in lower or lk == "ph" or lk.startswith("ph"):
        return 0.0, 14.0, 0.1
    if "orp" in lower:
        return 0.0, 1000.0, 1.0
    if any(t in lower for t in ("temp", "temperature")) or any(
        t in lk for t in ("temp", "temperature")
    ):
        return 4.0, 42.0, 0.5
    return 0.0, 100.0, 1.0


def infer_binary_sensor_device_class(path: str) -> BinarySensorDeviceClass | None:
    """Map shadow path tokens to HA binary device classes when unambiguous."""
    lower = path.lower()
    if re.search(r"\b(leak|flood)\b", lower):
        return BinarySensorDeviceClass.PROBLEM
    if re.search(r"\b(connect|online|reachable)\b", lower):
        return BinarySensorDeviceClass.CONNECTIVITY
    if re.search(r"\b(running|active|pumping|circulat)\b", lower):
        return BinarySensorDeviceClass.RUNNING
    if re.search(r"\b(heat|heating)\b", lower):
        return BinarySensorDeviceClass.HEAT
    if re.search(r"\b(cool|cooling)\b", lower):
        return BinarySensorDeviceClass.COLD
    if re.search(r"\b(lock|locked)\b", lower):
        return BinarySensorDeviceClass.LOCK
    if re.search(r"\b(motion|occup|presence)\b", lower):
        return BinarySensorDeviceClass.MOTION
    if re.search(r"\b(problem|fault|error|alarm|warn|trip)\b", lower):
        return BinarySensorDeviceClass.PROBLEM
    if re.search(r"\b(power|on|enable|enabled)\b", lower):
        return BinarySensorDeviceClass.POWER
    return None


def binary_extension_enabled_by_default(path: str) -> bool:
    """Expose likely user-facing fault/alarm booleans by default."""
    if _is_connectivity_shadow_metric_path(path) or _is_rf_diagnostic_path(path):
        return False
    lower = path.lower()
    return bool(
        re.search(r"\b(alarm|error|fault|leak|warning|trip|problem)\b", lower)
    )


def string_extension_enabled_by_default(path: str) -> bool:
    """Enable a subset of REST / status strings by default."""
    if _is_connectivity_shadow_metric_path(path) or _is_rf_diagnostic_path(path):
        return False
    lower = path.lower()
    if lower.startswith("cloud.rest."):
        return any(
            tok in lower
            for tok in ("water", "status", "message", "text", "mode", "tile", "summary")
        )
    return bool(
        re.search(r"\b(alarm|message|status|text|reason|fault)\b", lower)
    )
