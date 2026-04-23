"""Parse Gecko premium energy REST payloads (shared by coordinator + sensors)."""

from __future__ import annotations

from typing import Any


def _safe_float(data: Any, *keys: str) -> float | None:
    """Walk a nested dict by *keys* and return the leaf as a float, or None."""
    current = data
    for k in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(k)
    if current is None:
        return None
    if isinstance(current, bool):
        return None
    try:
        return float(current)
    except (TypeError, ValueError):
        return None


def _first_valid_float(data: Any, *key_paths: tuple[str, ...]) -> float | None:
    """Try each key path and return the first non-None float result."""
    for keys in key_paths:
        val = _safe_float(data, *keys)
        if val is not None:
            return val
    return None


_ENERGY_CONSUMPTION_FLOAT_PATHS: tuple[tuple[str, ...], ...] = (
    ("totalKwh",),
    ("total_kwh",),
    ("totalKWh",),
    ("totalEnergyKwh",),
    ("totalEnergyKWh",),
    ("energyKwh",),
    ("energy_kwh",),
    ("kwh",),
    ("consumptionKwh",),
    ("consumption_kwh",),
    ("value",),
    ("consumption", "totalKwh"),
    ("consumption", "value"),
    ("reading",),
    ("totalElectricityKwh",),
    ("total_electricity_kwh",),
    ("cumulativeKwh",),
    ("cumulative_kwh",),
    ("lifetimeKwh",),
    ("lifetime_kwh",),
    ("totalConsumptionKwh",),
    ("electricityConsumptionKwh",),
    ("data", "totalKwh"),
    ("data", "totalKWh"),
    ("data", "totalEnergyKWh"),
    ("data", "consumptionKwh"),
    ("data", "consumption"),
    ("data", "kwh"),
    ("data", "reading", "totalKwh"),
    ("data", "values", "totalKwh"),
    ("energy", "totalKwh"),
    ("energy", "totalKWh"),
    ("consumption", "totalKwh"),
    ("aggregates", "totalKwh"),
    ("reading", "totalKwh"),
    ("readings", "totalKwh"),
    ("result", "totalKwh"),
    ("payload", "totalKwh"),
)


def _float_from_kwh_named_keys(obj: Any, depth: int = 0) -> float | None:
    """Last resort: find a numeric value under dict keys that look like kWh totals."""
    if depth > 5 or not isinstance(obj, dict):
        return None
    for key, val in obj.items():
        key_l = str(key).lower()
        if "kwh" in key_l or key_l.endswith("kilowatthour"):
            if isinstance(val, bool):
                continue
            if isinstance(val, int | float):
                return float(val)
            if isinstance(val, str):
                try:
                    return float(val.strip())
                except ValueError:
                    continue
            if isinstance(val, dict):
                got = _float_from_kwh_named_keys(val, depth + 1)
                if got is not None:
                    return got
        if isinstance(val, dict):
            got = _float_from_kwh_named_keys(val, depth + 1)
            if got is not None:
                return got
    return None


def _coerce_energy_consumption_kwh(raw: Any) -> float | None:
    """Parse ``/energy-consumption`` payloads into kWh (vendor shapes vary)."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw.strip())
        except ValueError:
            return None
    if isinstance(raw, list) and raw:
        first = raw[0]
        got = _coerce_energy_consumption_kwh(first)
        if got is not None:
            return got
    if not isinstance(raw, dict):
        return None
    inner = raw.get("data")
    if isinstance(inner, list) and inner:
        inner = inner[0] if isinstance(inner[0], dict) else None
    if isinstance(inner, dict):
        from_inner = _first_valid_float(inner, *_ENERGY_CONSUMPTION_FLOAT_PATHS)
        if from_inner is not None:
            return from_inner
        from_inner = _float_from_kwh_named_keys(inner)
        if from_inner is not None:
            return from_inner
    direct = _first_valid_float(raw, *_ENERGY_CONSUMPTION_FLOAT_PATHS)
    if direct is not None:
        return direct
    return _float_from_kwh_named_keys(raw)


_ENERGY_COST_PATHS: tuple[tuple[str, ...], ...] = (
    ("totalCost",),
    ("total_cost",),
    ("cost",),
    ("value",),
    ("amount",),
    ("totalAmount",),
    ("estimatedCost",),
    ("grandTotal",),
    ("data", "totalCost"),
    ("data", "cost"),
)


def _coerce_energy_cost_amount(raw: Any) -> float | None:
    """Parse ``energyCost`` payloads into a currency amount."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw.strip())
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    inner = raw.get("data")
    if isinstance(inner, dict):
        v = _first_valid_float(inner, *_ENERGY_COST_PATHS)
        if v is not None:
            return v
    v = _first_valid_float(raw, *_ENERGY_COST_PATHS)
    if v is not None:
        return v
    return None


_ENERGY_SCORE_PATHS: tuple[tuple[str, ...], ...] = (
    ("score",),
    ("value",),
    ("rating",),
    ("efficiencyScore",),
    ("energyScore",),
    ("points",),
    ("data", "score"),
    ("data", "value"),
)


def _coerce_energy_score_value(raw: Any) -> float | None:
    """Parse ``energy/score`` payloads."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw.strip())
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    inner = raw.get("data")
    if isinstance(inner, dict):
        v = _first_valid_float(inner, *_ENERGY_SCORE_PATHS)
        if v is not None:
            return v
    return _first_valid_float(raw, *_ENERGY_SCORE_PATHS)


def premium_energy_poll_has_usable_values(energy: dict[str, Any]) -> bool:
    """True if at least one premium endpoint returned a value we can show in HA."""
    if _coerce_energy_consumption_kwh(energy.get("consumption")) is not None:
        return True
    if _coerce_energy_cost_amount(energy.get("cost")) is not None:
        return True
    if _coerce_energy_score_value(energy.get("score")) is not None:
        return True
    return False
