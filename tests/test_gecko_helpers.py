"""Tests for small helpers across Gecko modules (PR additions)."""

from __future__ import annotations

from types import SimpleNamespace

import custom_components.gecko as gecko_pkg
import pytest
import voluptuous as vol
from custom_components.gecko import services as gecko_services
from custom_components.gecko.const import (
    CONF_ALERTS_POLL_INTERVAL,
    DEFAULT_ALERTS_POLL_INTERVAL,
    DEFAULT_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
    DEFAULT_CLOUD_REST_POLL_INTERVAL,
)


def test_cloud_rest_defaults_enable_polling() -> None:
    """Cloud REST polling is active by default so chemistry readings appear."""
    assert DEFAULT_CLOUD_REST_POLL_INTERVAL == 300
    assert DEFAULT_CLOUD_REST_ONLY_WHEN_MQTT_DOWN is False


def test_humanize_metric_name() -> None:
    from custom_components.gecko.shadow_metrics import humanize_shadow_path

    result = humanize_shadow_path("zones.water.z1.ph_value")
    assert "Ph" in result or "pH" in result
    assert humanize_shadow_path("") == ""


def test_rest_alerts_entities_enabled() -> None:
    entry = SimpleNamespace(
        entry_id="abc",
        options={CONF_ALERTS_POLL_INTERVAL: DEFAULT_ALERTS_POLL_INTERVAL},
    )
    assert gecko_pkg._rest_alerts_entities_enabled(entry) is False
    entry.options = {CONF_ALERTS_POLL_INTERVAL: 120}
    assert gecko_pkg._rest_alerts_entities_enabled(entry) is True


def test_services_as_dict_raises() -> None:
    with pytest.raises(vol.Invalid):
        gecko_services._as_dict("updates", [])


def test_services_as_desired_fragment_valid() -> None:
    frag = gecko_services._as_desired_fragment({"zones": {"pump": {"on": True}}})
    assert frag == {"zones": {"pump": {"on": True}}}


def test_services_as_desired_fragment_rejects_keys() -> None:
    with pytest.raises(vol.Invalid):
        gecko_services._as_desired_fragment({"zones": {}, "extra": 1})


def test_services_as_desired_fragment_size_limit() -> None:
    big = {"x": "y" * gecko_services._MAX_DESIRED_JSON_BYTES}
    with pytest.raises(vol.Invalid):
        gecko_services._as_desired_fragment({"zones": big})


def test_allowed_monitor_ids_and_vessel_id_for_monitor() -> None:
    entry = SimpleNamespace(
        data={
            "vessels": [
                {"monitorId": "m1", "vesselId": "v1"},
                {"monitorId": None},
                "bad",
            ]
        }
    )
    assert gecko_services._allowed_monitor_ids(entry) == {"m1"}
    assert gecko_services._vessel_id_for_monitor(entry, "m1") == "v1"
    assert gecko_services._vessel_id_for_monitor(entry, "unknown") == "unknown"
