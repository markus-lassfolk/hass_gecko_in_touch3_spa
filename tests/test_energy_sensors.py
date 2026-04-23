"""Tests for premium energy sensor payload parsing."""

from custom_components.gecko.sensor import _first_valid_float, _safe_float


def test_first_valid_float_preserves_zero() -> None:
    """Or-chaining would skip 0.0; first-valid must not."""
    raw = {"totalKwh": 0.0}
    assert _first_valid_float(raw, ("totalKwh",), ("value",)) == 0.0
    assert _first_valid_float({"value": 0.0}, ("missing",), ("value",)) == 0.0
    assert _first_valid_float({}, ("a",)) is None


def test_first_valid_float_prefers_first_non_none() -> None:
    raw = {"a": 1.0, "b": 2.5}
    assert _first_valid_float(raw, ("a",), ("b",)) == 1.0


def test_safe_float_nested() -> None:
    data = {"a": {"b": 3.14}}
    assert _safe_float(data, "a", "b") == 3.14
    assert _safe_float(data, "a", "missing") is None
