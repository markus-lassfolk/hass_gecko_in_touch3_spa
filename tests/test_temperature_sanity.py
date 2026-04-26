"""Tests for ``coerce_spa_water_temperature_c`` (Grafana-safe current water °C)."""

from __future__ import annotations

import pytest
from custom_components.gecko.temperature_sanity import (
    SPA_CURRENT_WATER_TEMP_MAX_C,
    SPA_CURRENT_WATER_TEMP_MIN_C,
    coerce_spa_water_temperature_c,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        ("", None),
        ("not-a-number", None),
        (0, None),
        (0.0, None),
        (3.9, None),
        (4.0, 4.0),
        (36.5, 36.5),
        ("36.5", 36.5),
        (45.0, 45.0),
        (45.1, None),
        (float("nan"), None),
        ("nan", None),
        (float("inf"), None),
        ("-inf", None),
    ],
)
def test_coerce_spa_water_temperature_c(raw, expected: float | None) -> None:
    assert coerce_spa_water_temperature_c(raw) == expected


def test_plausible_band_constants() -> None:
    assert SPA_CURRENT_WATER_TEMP_MIN_C < SPA_CURRENT_WATER_TEMP_MAX_C
