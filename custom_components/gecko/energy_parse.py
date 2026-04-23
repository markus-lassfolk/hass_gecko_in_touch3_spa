"""Parse Gecko premium energy REST payloads (shared by coordinator + sensors)."""

from __future__ import annotations

import re
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


# Watt-hour keys on Gecko ``/energy-consumption`` (v1). Order matters: period totals
# (``energyConsumptionWh``) are often 0 at the start of a billing window, while
# ``totalEnergyConsumptionWh`` carries the cumulative meter reading we need for
# ``TOTAL_INCREASING`` energy sensors.
_WH_CONSUMPTION_KEYS: tuple[str, ...] = (
    "totalEnergyConsumptionWh",
    "lifetimeEnergyConsumptionWh",
    "cumulativeEnergyConsumptionWh",
    "totalConsumptionWh",
    "consumptionWh",
    "energyConsumptionWh",
    "worstCaseConsumptionWh",
)


def _kwh_from_wh_dict(d: dict[str, Any]) -> float | None:
    """Return kWh from the first usable Wh counter on this dict branch."""
    for wh_key in _WH_CONSUMPTION_KEYS:
        wh = d.get(wh_key)
        if isinstance(wh, bool):
            continue
        if isinstance(wh, int | float):
            kwh = float(wh) / 1000.0
            if kwh >= 0:
                return kwh
        if isinstance(wh, str):
            try:
                kwh = float(wh.strip()) / 1000.0
            except ValueError:
                continue
            if kwh >= 0:
                return kwh
    return None


def _best_positive_wh_kwh_deep(obj: Any, depth: int = 0) -> float | None:
    """Largest positive kWh from any ``*Wh`` numeric leaf (unknown vendor layouts)."""
    if depth > 10:
        return None
    best: float | None = None
    if isinstance(obj, dict):
        for key, val in obj.items():
            kl = str(key)
            if isinstance(val, bool):
                continue
            key_l = kl.lower()
            # ``totalEnergyKWh`` / ``*kwh`` totals are kWh, not watt-hour counters.
            if key_l.endswith("kwh"):
                if isinstance(val, dict | list):
                    sub = _best_positive_wh_kwh_deep(val, depth + 1)
                    if sub is not None and (best is None or sub > best):
                        best = sub
                continue
            if kl.endswith("Wh") or "consumptionwh" in key_l.replace("_", ""):
                if isinstance(val, str):
                    try:
                        wh = float(val.strip())
                    except ValueError:
                        continue
                elif isinstance(val, int | float):
                    wh = float(val)
                else:
                    continue
                kwh = wh / 1000.0
                if kwh > 0 and (best is None or kwh > best):
                    best = kwh
            elif isinstance(val, dict | list):
                sub = _best_positive_wh_kwh_deep(val, depth + 1)
                if sub is not None and (best is None or sub > best):
                    best = sub
    elif isinstance(obj, list):
        for item in obj:
            sub = _best_positive_wh_kwh_deep(item, depth + 1)
            if sub is not None and (best is None or sub > best):
                best = sub
    return best


def coerce_energy_consumption_kwh(raw: Any) -> float | None:
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
        got = coerce_energy_consumption_kwh(first)
        if got is not None:
            return got
    if not isinstance(raw, dict):
        return None
    got_wh = _kwh_from_wh_dict(raw)
    deep_wh = _best_positive_wh_kwh_deep(raw)
    if deep_wh is not None and deep_wh > 0:
        if got_wh is None or got_wh == 0.0:
            got_wh = deep_wh
        else:
            got_wh = max(got_wh, deep_wh)
    if got_wh is not None:
        return got_wh
    inner = raw.get("data")
    if isinstance(inner, list) and inner:
        inner = inner[0] if isinstance(inner[0], dict) else None
    if isinstance(inner, dict):
        got_wh = _kwh_from_wh_dict(inner)
        if got_wh is not None:
            return got_wh
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
    ("energyCost", "amount"),
    ("energyCost", "totalCost"),
    ("energyCost", "total"),
    ("energyCost", "price"),
    ("energyCost", "estimatedCost"),
    ("energyCost", "netAmount"),
    ("energyCost", "grossAmount"),
    ("energyCost", "value"),
    ("energyCost", "cost"),
    ("data", "amount"),
)


def _parse_money_display_string(value: str) -> float | None:
    """Best-effort parse for API ``formatted*Cost`` strings (e.g. ``12,50 kr``)."""
    s = value.strip()
    if not s:
        return None
    # Strip common currency tokens / spaces (ASCII and NBSP).
    cleaned = (
        s.replace("\xa0", " ")
        .replace(" ", "")
        .replace("kr", "")
        .replace("SEK", "")
        .replace("EUR", "")
        .replace("USD", "")
        .replace("€", "")
        .replace("$", "")
    )
    # Prefer a simple decimal: optional digits, comma or dot, digits.
    m = re.search(r"-?\d+(?:[.,]\d+)?", cleaned)
    if not m:
        return None
    num = m.group(0)
    if "," in num and "." in num:
        # Assume thousands with dot, decimal with comma (e.g. 1.234,56).
        num = num.replace(".", "").replace(",", ".")
    elif "," in num:
        num = num.replace(",", ".")
    try:
        return float(num)
    except ValueError:
        return None


def _scan_dict_for_cost_number(obj: Any, depth: int = 0) -> float | None:
    """Find a plausible monetary scalar when vendor keys do not match fixed paths."""
    if depth > 8 or not isinstance(obj, dict):
        return None

    priority_keys = (
        "amount",
        "totalCost",
        "total_cost",
        "estimatedCost",
        "grandTotal",
        "totalAmount",
        "netAmount",
        "grossAmount",
        "price",
        "total",
        "value",
        "cost",
    )
    for pk in priority_keys:
        if pk not in obj:
            continue
        val = obj[pk]
        if isinstance(val, bool):
            continue
        if isinstance(val, int | float):
            return float(val)
        if isinstance(val, dict):
            got = _scan_dict_for_cost_number(val, depth + 1)
            if got is not None:
                return got

    skip_tokens = (
        "currency",
        "formatted",
        "period",
        "status",
        "generated",
        "ispremium",
    )
    for key, val in obj.items():
        kl = str(key).lower()
        if any(tok in kl for tok in skip_tokens):
            continue
        if isinstance(val, bool):
            continue
        if isinstance(val, int | float):
            if any(tok in kl for tok in ("amount", "cost", "price", "total", "sum")):
                fv = float(val)
                if -1e9 < fv < 1e9:
                    return fv
        elif isinstance(val, dict):
            got = _scan_dict_for_cost_number(val, depth + 1)
            if got is not None:
                return got
        elif isinstance(val, list):
            for item in val:
                got = _scan_dict_for_cost_number(item, depth + 1)
                if got is not None:
                    return got
    return None


def coerce_energy_cost_amount(raw: Any) -> float | None:
    """Parse ``energyCost`` payloads into a currency amount."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw.strip())
        except ValueError:
            parsed = _parse_money_display_string(raw)
            if parsed is not None:
                return parsed
            return None
    if isinstance(raw, list) and raw:
        return coerce_energy_cost_amount(raw[0])
    if not isinstance(raw, dict):
        return None
    # Wrapper shape: ``{"energyCost": {...}}`` or ``{"energyCost": 12.34}``.
    if "energyCost" in raw:
        ec = raw.get("energyCost")
        if ec is not None and not isinstance(ec, bool):
            nested = coerce_energy_cost_amount(ec)
            if nested is not None:
                return nested
    for fmt_key in (
        "formattedEnergyCost",
        "formattedWorstCaseEnergyCost",
        "formattedSavings",
    ):
        fv = raw.get(fmt_key)
        if isinstance(fv, str):
            parsed = _parse_money_display_string(fv)
            if parsed is not None:
                return parsed
    inner = raw.get("data")
    if isinstance(inner, list) and inner:
        inner = inner[0] if isinstance(inner[0], dict) else None
    if isinstance(inner, dict):
        v = _first_valid_float(inner, *_ENERGY_COST_PATHS)
        if v is not None:
            return v
        nested_inner = coerce_energy_cost_amount(inner)
        if nested_inner is not None:
            return nested_inner
    v = _first_valid_float(raw, *_ENERGY_COST_PATHS)
    if v is not None:
        return v
    scanned = _scan_dict_for_cost_number(raw)
    if scanned is not None:
        return scanned
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
    ("score", "value"),
    ("score", "rating"),
    ("score", "current"),
)


def coerce_energy_score_value(raw: Any) -> float | None:
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
    score_obj = raw.get("score")
    if isinstance(score_obj, dict):
        v = _first_valid_float(score_obj, ("value",), ("rating",), ("score",))
        if v is not None:
            return v
    elif isinstance(score_obj, int | float):
        return float(score_obj)
    inner = raw.get("data")
    if isinstance(inner, dict):
        v = _first_valid_float(inner, *_ENERGY_SCORE_PATHS)
        if v is not None:
            return v
    return _first_valid_float(raw, *_ENERGY_SCORE_PATHS)


def premium_energy_poll_has_usable_values(energy: dict[str, Any]) -> bool:
    """True if at least one premium endpoint returned a value we can show in HA."""
    if coerce_energy_consumption_kwh(energy.get("consumption")) is not None:
        return True
    if coerce_energy_cost_amount(energy.get("cost")) is not None:
        return True
    if coerce_energy_score_value(energy.get("score")) is not None:
        return True
    return False


# Back-compat for tests and older call sites.
_coerce_energy_consumption_kwh = coerce_energy_consumption_kwh
_coerce_energy_cost_amount = coerce_energy_cost_amount
_coerce_energy_score_value = coerce_energy_score_value
