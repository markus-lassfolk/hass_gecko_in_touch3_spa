"""Tests for ``custom_components.gecko.cloud_tiles``."""

from __future__ import annotations

import pytest
from custom_components.gecko import cloud_tiles


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, None),
        (7, 7),
        (1.5, 1.5),
        (float("nan"), None),
        (float("inf"), None),
        ("x", None),
    ],
)
def test_num(value, expected) -> None:
    assert cloud_tiles._num(value) == expected


def test_ph_value_nested_and_scalar() -> None:
    disc = {"phStatus": {"value": 7.4}}
    assert cloud_tiles._ph_value(disc, {}) == 7.4
    assert cloud_tiles._ph_value({}, {"ph_status": 7.0}) == 7.0


def test_orp_mv_reads_dict() -> None:
    status = {"orpStatus": {"mv": 650}}
    assert cloud_tiles._orp_mv({}, status) == 650


def test_temp_c_aliases() -> None:
    assert cloud_tiles._temp_c({}, {"tempC": 36.5}) == 36.5
    assert cloud_tiles._temp_c({"temperature": 20}, {}) == 20


def test_string_leaf_rejects_jwt_prefix() -> None:
    assert cloud_tiles._string_leaf("eyJabc") is None
    assert cloud_tiles._string_leaf("  ok  ") == "ok"


def test_extract_cloud_tile_strings_nested_dict() -> None:
    vessel = {
        "status": {
            "waterStatus": {"text": "  Flow OK  "},
        }
    }
    out = cloud_tiles.extract_cloud_tile_strings(vessel)
    assert "cloud.rest.status.waterStatus.text" in out


def test_extract_cloud_tile_booleans_empty_vessel() -> None:
    assert cloud_tiles.extract_cloud_tile_booleans({}) == {}


def test_extract_cloud_tile_booleans_rejects_non_dict() -> None:
    assert cloud_tiles.extract_cloud_tile_booleans("nope") == {}


def test_extract_cloud_tile_metrics_combined() -> None:
    vessel = {
        "status": {
            "discElements": {
                "tempC": 37.0,
                "phStatus": {"value": 7.2},
                "orpStatus": {"mv": 400},
            }
        }
    }
    m = cloud_tiles.extract_cloud_tile_metrics(vessel)
    assert m["cloud.rest.disc_elements.temp_c"] == 37.0
    assert m["cloud.rest.summary.ph"] == 7.2
    assert m["cloud.rest.summary.orp_mv"] == 400


def test_find_vessel_record() -> None:
    vessels = [
        {"vesselId": "a", "name": "A"},
        {"id": "b"},
        {"vessel_id": "c"},
    ]
    assert cloud_tiles.find_vessel_record(vessels, "a")["name"] == "A"
    assert cloud_tiles.find_vessel_record(vessels, "b")["id"] == "b"
    assert cloud_tiles.find_vessel_record(vessels, 99) is None
