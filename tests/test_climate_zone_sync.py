"""Climate entity must track the live zone object after each MQTT snapshot."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from custom_components.gecko.climate import GeckoClimate
from gecko_iot_client.models.zone_types import ZoneType


@pytest.fixture
def mock_hass():
    return MagicMock()


def test_gecko_climate_rebinds_zone_when_coordinator_replaces_models(mock_hass):
    """Coordinator assigns a new zone dict; climate must not keep a stale reference."""
    old_zone = SimpleNamespace(
        id=1,
        min_temperature_set_point_c=10.0,
        max_temperature_set_point_c=42.0,
        temperature=29.0,
        target_temperature=30.0,
        status=None,
        mode=None,
    )
    new_zone = SimpleNamespace(
        id=1,
        min_temperature_set_point_c=10.0,
        max_temperature_set_point_c=42.0,
        temperature=29.0,
        target_temperature=38.0,
        status=None,
        mode=None,
        set_target_temperature=MagicMock(),
    )

    snapshot: dict[str, list] = {"zones": [old_zone]}

    coordinator = SimpleNamespace(
        hass=mock_hass,
        entry_id="ent1",
        vessel_id="v1",
        get_zones_by_type=lambda zt: (
            snapshot["zones"] if zt is ZoneType.TEMPERATURE_CONTROL_ZONE else []
        ),
    )

    ent = GeckoClimate(coordinator, old_zone)
    assert ent._attr_target_temperature == 30.0
    assert ent._zone is old_zone

    snapshot["zones"] = [new_zone]
    ent._update_from_zone()
    assert ent._zone is new_zone
    assert ent._attr_target_temperature == 38.0
