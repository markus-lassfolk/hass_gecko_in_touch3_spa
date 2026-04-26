"""Tests for ``summarize_spa_configuration_zones``."""

from __future__ import annotations

from custom_components.gecko.spa_config_summary import summarize_spa_configuration_zones


def test_summarize_missing_or_empty() -> None:
    assert summarize_spa_configuration_zones(None) == {"present": False}
    assert summarize_spa_configuration_zones({}) == {"present": False}


def test_summarize_flow_and_lighting_maps() -> None:
    cfg = {
        "accessories": {
            "pumps": {"1": {}, "2": {}},
            "lights": {"10": {}},
            "waterfalls": {},
            "blowers": {},
        },
        "zones": {
            "flow": {
                "1": {"pumps": [1, 2], "waterfalls": [3], "blowers": [4]},
            },
            "lighting": {
                "2": {"lights": [10, 11]},
            },
        },
    }
    out = summarize_spa_configuration_zones(cfg)
    assert out["present"] is True
    assert out["accessory_counts"] == {
        "pumps": 2,
        "lights": 1,
        "waterfalls": 0,
        "blowers": 0,
    }
    assert out["pump_id_to_flow_zone_id"] == {"1": "1", "2": "1"}
    assert out["waterfall_id_to_flow_zone_id"] == {"3": "1"}
    assert out["blower_id_to_flow_zone_id"] == {"4": "1"}
    assert out["light_id_to_lighting_zone_id"] == {"10": "2", "11": "2"}
