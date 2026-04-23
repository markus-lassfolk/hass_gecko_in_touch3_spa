"""Tests for flow-zone speed capability and HA percentage ↔ mode mapping."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.gecko.telemetry import (
    derive_flow_percentage,
    get_flow_speed_mode_for_percentage,
    get_supported_flow_speed_modes,
    zone_supports_speed_control,
)


def test_zone_supports_speed_control_false_for_set_speed_only_pump() -> None:
    """On/off-style pumps must not advertise speed control without a ladder."""
    zone = SimpleNamespace(
        id="p1",
        speed_config=None,
        set_speed=lambda *_a, **_k: None,
    )
    assert not zone_supports_speed_control(zone)


def test_zone_supports_speed_control_true_with_speed_ladder() -> None:
    """Zones with explicit min/max/step in speed_config support speed."""
    zone = SimpleNamespace(
        id="p2",
        speed_config={"minimum": 1.0, "maximum": 3.0, "stepIncrement": 1.0},
    )
    assert zone_supports_speed_control(zone)


def test_three_mode_percentage_round_trips_with_derive_flow_percentage() -> None:
    """Slider percentage from derive_flow must map back to the same mode (Bugbot)."""
    zone = SimpleNamespace(
        id="p3",
        active=True,
        speed=2.0,
        speed_config={"minimum": 1.0, "maximum": 3.0, "stepIncrement": 1.0},
    )
    modes = get_supported_flow_speed_modes(zone)
    assert modes == ("low", "medium", "high")

    pct = derive_flow_percentage(zone)
    assert pct == 67
    assert get_flow_speed_mode_for_percentage(zone, pct) == "medium"
