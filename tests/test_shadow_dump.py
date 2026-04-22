"""Tests for ``custom_components.gecko.shadow_dump`` (shadow export / sanitization)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from custom_components.gecko import shadow_dump


def test_integration_version_reads_manifest() -> None:
    v = shadow_dump.integration_version()
    assert v == "2.3.0"


def test_key_segments_splits_camel_and_separators() -> None:
    assert shadow_dump._key_segments("fooBar.baz-Qux") == ["foo", "bar", "baz", "qux"]


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("monitorId", True),
        ("nested.monitor_id.value", True),
        ("apiKey", True),
        ("safeTemperature", False),
    ],
)
def test_segment_key_is_sensitive(key: str, expected: bool) -> None:
    assert shadow_dump._segment_key_is_sensitive(key) is expected


def test_opaque_fingerprint_stable_per_input() -> None:
    a = shadow_dump._opaque_fingerprint("monitor", "mid-1")
    b = shadow_dump._opaque_fingerprint("monitor", "mid-1")
    c = shadow_dump._opaque_fingerprint("monitor", "mid-2")
    assert a == b
    assert a != c
    assert a.startswith("anon_monitor_")


@pytest.mark.parametrize(
    ("text", "private"),
    [
        ("192.168.1.1", True),
        ("10.0.0.1", True),
        ("127.0.0.1", True),
        ("8.8.8.8", False),
        ("not-an-ip", False),
    ],
)
def test_is_private_ipv4(text: str, private: bool) -> None:
    assert shadow_dump._is_private_ipv4(text) is private


def test_redact_string_scalars_scrubs_patterns() -> None:
    s = "Contact a@b.com token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U mac aa:bb:cc:dd:ee:ff"
    out = shadow_dump._redact_string_scalars(s)
    assert "a@b.com" not in out
    assert "eyJ" not in out
    assert "aa:bb:cc:dd:ee:ff" not in out
    assert shadow_dump._REDACTED in out


def test_deep_sanitize_redacts_keys_and_nested_strings() -> None:
    obj = {
        "nested": {"password": "secret"},
        "body": "mail x@y.co",
        "nums": {"geoLon": 12.3, "ok": 1},
        "list": [{"token": "abc"}],
    }
    out = shadow_dump._deep_sanitize(obj)
    assert out["nested"]["password"] == shadow_dump._REDACTED
    assert "x@y.co" not in str(out["body"])
    assert out["nums"]["geoLon"] is None
    assert out["nums"]["ok"] == 1


def test_deep_sanitize_preserves_bool_for_geo_keys() -> None:
    """Booleans must not be treated as numeric lat/long candidates."""
    assert shadow_dump._deep_sanitize({"geoLon": True}) == {"geoLon": True}


def test_sanitize_export_payload_fingerprints_ids() -> None:
    payload = {
        "monitor_id": "m1",
        "vessel_id": "v1",
        "transporter": {"monitor_id": "m1", "class": "T"},
        "device_shadow_state": {"nested": {"apiKey": "k"}},
    }
    out = shadow_dump.sanitize_export_payload(payload)
    assert out["sanitized_for_public_share"] is True
    assert out["monitor_id"].startswith("anon_monitor_")
    assert out["vessel_id"].startswith("anon_vessel_")
    assert out["transporter"]["monitor_id"].startswith("anon_monitor_")
    assert out["device_shadow_state"]["nested"]["apiKey"] == shadow_dump._REDACTED


def test_safe_export_filename_no_traversal() -> None:
    name = shadow_dump.safe_export_filename(None, "mon_1", anonymous=False)
    assert name.endswith(".json")
    assert ".." not in name
    assert "/" not in name


def test_safe_export_filename_sanitizes_malicious_base() -> None:
    name = shadow_dump.safe_export_filename("../etc/passwd", "mid", anonymous=False)
    assert ".." not in name
    assert name.endswith(".json")


def test_build_shadow_export_payload_structure() -> None:
    fixed_dt = MagicMock()
    fixed_dt.isoformat.return_value = "2020-01-01T00:00:00+00:00"
    fixed_dt.strftime.return_value = "20200101_000000"

    class _DummyTransporter:
        _monitor_id = "tmid"

    client = SimpleNamespace(
        _state={"state": {"reported": {"zones": {}}}},
        _configuration={"cfg": 1},
        transporter=_DummyTransporter(),
    )
    client.list_zones = lambda: [{"id": "z1"}]  # type: ignore[attr-defined]

    with patch.object(shadow_dump, "_utc_now", return_value=fixed_dt):
        payload = shadow_dump.build_shadow_export_payload(
            monitor_id="m",
            vessel_id="v",
            gecko_client=client,
            include_configuration=True,
            include_derived=True,
            mqtt_connected=True,
            sanitize_for_public_share=False,
        )
    assert payload["export_format"] == "gecko_ha_shadow_dump"
    assert payload["mqtt_connected"] is True
    assert payload["sanitized_for_public_share"] is False
    assert payload["zones_list"] == [{"id": "z1"}]
    assert "shadow_topology_summary" in payload


def test_write_json_file_roundtrip(tmp_path) -> None:
    p = tmp_path / "sub" / "out.json"
    data = {"a": 1, "b": [2, 3]}
    shadow_dump.write_json_file(p, data)
    assert json.loads(p.read_text(encoding="utf-8")) == data


def test_segment_key_sensitive_api_key_pair() -> None:
    assert shadow_dump._segment_key_is_sensitive("nested.api.key.path")


def test_safe_export_filename_with_user_part() -> None:
    name = shadow_dump.safe_export_filename("My_Spa_export", "m1", anonymous=False)
    assert name.startswith("My_Spa_export")
    assert name.endswith(".json")


def test_safe_export_filename_anonymous_prefix() -> None:
    name = shadow_dump.safe_export_filename(None, "m1", anonymous=True)
    assert name.startswith("gecko_shadow_export_")
    assert name.endswith(".json")


def test_sanitize_export_empty_ids_use_redacted_or_anon() -> None:
    out = shadow_dump.sanitize_export_payload(
        {"monitor_id": "", "vessel_id": "", "device_shadow_state": None}
    )
    assert out["monitor_id"] == shadow_dump._REDACTED
    assert out["vessel_id"] == shadow_dump._REDACTED


def test_build_shadow_export_list_zones_exception_returns_empty() -> None:
    client = SimpleNamespace(_state=None, transporter=None)

    def boom():
        raise RuntimeError("no zones")

    client.list_zones = boom  # type: ignore[attr-defined]
    fixed = MagicMock()
    fixed.isoformat.return_value = "2020-01-01T00:00:00+00:00"
    with patch.object(shadow_dump, "_utc_now", return_value=fixed):
        payload = shadow_dump.build_shadow_export_payload(
            monitor_id="m",
            vessel_id="v",
            gecko_client=client,
            include_configuration=False,
            include_derived=False,
            mqtt_connected=False,
            sanitize_for_public_share=False,
        )
    assert payload["zones_list"] == []
