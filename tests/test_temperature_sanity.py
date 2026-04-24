"""Tests for ``coerce_spa_water_temperature_c`` (Grafana-safe current water °C)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_MOD_PATH = _ROOT / "custom_components" / "gecko" / "temperature_sanity.py"
_spec = importlib.util.spec_from_file_location("gecko_temperature_sanity", _MOD_PATH)
assert _spec and _spec.loader
_ts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ts)
coerce_spa_water_temperature_c = _ts.coerce_spa_water_temperature_c
SPA_CURRENT_WATER_TEMP_MAX_C = _ts.SPA_CURRENT_WATER_TEMP_MAX_C
SPA_CURRENT_WATER_TEMP_MIN_C = _ts.SPA_CURRENT_WATER_TEMP_MIN_C


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
    ],
)
def test_coerce_spa_water_temperature_c(raw, expected: float | None) -> None:
    assert coerce_spa_water_temperature_c(raw) == expected


def test_plausible_band_constants() -> None:
    assert SPA_CURRENT_WATER_TEMP_MIN_C < SPA_CURRENT_WATER_TEMP_MAX_C
