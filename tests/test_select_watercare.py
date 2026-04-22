"""Tests for watercare select option normalization (legacy labels + library fallback)."""

from __future__ import annotations

import custom_components.gecko.select as sel


def test_normalize_watercare_user_option_snake_case() -> None:
    assert sel._normalize_watercare_user_option("away") == "away"
    assert sel._normalize_watercare_user_option("super_savings") == "super_savings"


def test_normalize_watercare_user_option_legacy_title_case() -> None:
    assert sel._normalize_watercare_user_option("Away") == "away"
    assert sel._normalize_watercare_user_option("Super Savings") == "super_savings"
    assert sel._normalize_watercare_user_option("super savings") == "super_savings"


def test_normalize_watercare_user_option_invalid() -> None:
    assert sel._normalize_watercare_user_option("not-a-mode") is None


def test_coerce_library_mode_to_option_known() -> None:
    assert sel._coerce_library_mode_to_option("Away") == "away"
    assert sel._coerce_library_mode_to_option("Standard") == "standard"


def test_coerce_library_mode_to_option_unknown_maps_to_other() -> None:
    assert sel._coerce_library_mode_to_option("Future Spa Mode") == "other"


def test_coerce_library_mode_to_option_snake_in_options() -> None:
    assert sel._coerce_library_mode_to_option("custom_weekender") == "other"
    assert sel._coerce_library_mode_to_option("Weekender") == "weekender"
