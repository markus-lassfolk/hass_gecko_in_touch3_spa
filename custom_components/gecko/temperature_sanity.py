"""Coerce spa *measured* water temperatures for Home Assistant state.

Grafana and long-term stats treat numeric ``0`` as a real reading. Gecko
transports sometimes surface **0** or out-of-range values when the reading is
not meaningful (same class of issue as legacy in.touch3 integrations).
We map those to ``None`` so climate / sensors report **unknown** instead of
bogus °C.
"""

from __future__ import annotations

from typing import Any

# Aligns with plausible spa water from ``infer_number_setpoint_limits`` (shadow),
# with a slightly wider high bound for sensors / heat soak.
SPA_CURRENT_WATER_TEMP_MIN_C = 4.0
SPA_CURRENT_WATER_TEMP_MAX_C = 45.0


def coerce_spa_water_temperature_c(raw: Any) -> float | None:
    """Return float °C for live water temperature, or ``None`` if not plausible.

    Used only for **current** (measured) water temperature — not setpoints.
    """
    if raw is None:
        return None
    try:
        t = float(raw)
    except (TypeError, ValueError):
        return None
    if t == 0.0:
        return None
    if t < SPA_CURRENT_WATER_TEMP_MIN_C or t > SPA_CURRENT_WATER_TEMP_MAX_C:
        return None
    return t
