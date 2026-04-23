"""Tests for cleaning-mode detection on GeckoCleaningModeBinarySensor."""

from types import SimpleNamespace

from custom_components.gecko.binary_sensor import GeckoCleaningModeBinarySensor


def test_cleaning_bool_false_does_not_short_circuit_mode_name() -> None:
    """Regression: False on is_cleaning must not skip mode_name / operation_mode."""
    status = SimpleNamespace(
        is_cleaning=False,
        mode_name="CLEANING",
        operation_mode=None,
    )
    # ``self`` is unused inside _is_cleaning_from_status
    assert GeckoCleaningModeBinarySensor._is_cleaning_from_status(None, status) is True


def test_cleaning_explicit_true_still_wins() -> None:
    status = SimpleNamespace(
        is_cleaning=True,
        mode_name="STANDARD",
        operation_mode=None,
    )
    assert GeckoCleaningModeBinarySensor._is_cleaning_from_status(None, status) is True
