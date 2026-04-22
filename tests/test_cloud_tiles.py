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


def test_disc_elements_snake_case_under_status() -> None:
    vessel = {
        "status": {
            "disc_elements": {
                "phStatus": {"value": 7.1},
            }
        }
    }
    m = cloud_tiles.extract_cloud_tile_metrics(vessel)
    assert m["cloud.rest.summary.ph"] == 7.1


def test_extract_strings_status_not_dict() -> None:
    vessel = {"status": "ok"}
    assert cloud_tiles.extract_cloud_tile_strings(vessel) == {}


_V6_VESSEL_DETAIL = {
    "vesselId": 25657,
    "readings": {
        "ph": {
            "readingType": "ph",
            "value": 7.85,
            "unit": "ph",
            "status": "high",
            "abbreviation": "pH",
            "title": "pH",
            "source": "monitor",
        },
        "orp": {
            "readingType": "orp",
            "value": 198,
            "unit": "mV",
            "status": "really_low",
            "abbreviation": "ORP",
            "title": "Oxidation Reduction Potential",
            "source": "monitor",
        },
        "waterTemp": {
            "readingType": "waterTemp",
            "value": 29,
            "unit": "C",
            "status": "ok",
            "title": "Water Temperature",
            "source": "monitor",
        },
        "totalAlkalinity": {
            "readingType": "totalAlkalinity",
            "value": 120,
            "unit": "ppm",
            "status": "ok",
            "title": "Total Alkalinity",
            "source": "report",
        },
        "freeChlorine": {
            "readingType": "freeChlorine",
            "value": 0,
            "unit": "ppm",
            "status": "really_low",
            "title": "Free Chlorine",
            "source": "report",
        },
        "lsi": {
            "readingType": "lsi",
            "value": -0.45,
            "unit": "lsi",
            "status": "really_low",
            "title": "Langelier Saturation Index",
            "source": "computed",
        },
        "wifiRssi": {
            "readingType": "wifiRssi",
            "value": 82,
            "unit": "db",
            "status": "ok",
            "title": "WiFi RSSI",
            "source": "monitor",
        },
    },
    "monitorReadings": {
        "ph": {
            "readingType": "ph",
            "value": 7.85,
            "unit": "ph",
        },
    },
}


def test_extract_vessel_readings_metrics() -> None:
    m = cloud_tiles.extract_vessel_readings_metrics(_V6_VESSEL_DETAIL)
    assert m["cloud.rest.readings.ph"] == 7.85
    assert m["cloud.rest.readings.orp"] == 198
    assert m["cloud.rest.readings.waterTemp"] == 29
    assert m["cloud.rest.readings.totalAlkalinity"] == 120
    assert m["cloud.rest.readings.freeChlorine"] == 0
    assert m["cloud.rest.readings.lsi"] == -0.45
    assert m["cloud.rest.readings.wifiRssi"] == 82


def test_extract_vessel_readings_metrics_empty() -> None:
    assert cloud_tiles.extract_vessel_readings_metrics({}) == {}
    assert cloud_tiles.extract_vessel_readings_metrics("bad") == {}


def test_extract_vessel_readings_no_duplicate_from_monitor_readings() -> None:
    """readings takes priority; monitorReadings should not overwrite."""
    m = cloud_tiles.extract_vessel_readings_metrics(_V6_VESSEL_DETAIL)
    assert m["cloud.rest.readings.ph"] == 7.85


def test_extract_vessel_readings_strings() -> None:
    s = cloud_tiles.extract_vessel_readings_strings(_V6_VESSEL_DETAIL)
    assert s["cloud.rest.readings.ph.status"] == "high"
    assert s["cloud.rest.readings.ph.title"] == "pH"
    assert s["cloud.rest.readings.ph.source"] == "monitor"
    assert s["cloud.rest.readings.orp.abbreviation"] == "ORP"
    assert s["cloud.rest.readings.waterTemp.title"] == "Water Temperature"


def test_extract_vessel_readings_strings_empty() -> None:
    assert cloud_tiles.extract_vessel_readings_strings({}) == {}


def test_is_wifi_diagnostic_reading() -> None:
    assert cloud_tiles.is_wifi_diagnostic_reading("wifiRssi") is True
    assert cloud_tiles.is_wifi_diagnostic_reading("ph") is False
