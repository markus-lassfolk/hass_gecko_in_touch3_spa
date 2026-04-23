"""Home Assistant sensor platform rules: ``device_class`` vs ``state_class``.

Core logs a warning when the pair is invalid (``DEVICE_CLASS_STATE_CLASSES``).
These tests catch regressions without loading entities into a running ``hass``.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import custom_components.gecko.sensor as gecko_sensor
import pytest
from custom_components.gecko import shadow_metrics
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import DEVICE_CLASS_STATE_CLASSES


def _shadow_paths_exercising_infer_hints() -> list[str]:
    """Paths intended to cover ``infer_sensor_metadata`` + ``apply_numeric_shadow_sensor_hints``."""
    return [
        "zones.x.phValue",
        "zones.z.phReading",
        "something.orp_mv.reading",
        "zones.flow.temp_sensor",
        "zones.spa.humidity.value",
        "zones.soil.moisture.value",
        "zones.line.pressure.psi",
        "zones.meter.kwh",
        "zones.pump.power.reading",
        "zones.flow.reading",
        "zones.meter.voltage.reading",
        "zones.pump.amperage",
        "zones.line.frequency.hz",
        "zones.filter.flow_gpm",
        "zones.probe.conductivity.us_cm",
        "zones.pump.runtime_seconds",
        "cloud.rest.readings.ph",
        "cloud.rest.readings.orp",
        "cloud.rest.readings.waterTemp",
        "cloud.rest.readings.tds",
        "cloud.rest.readings.salinity",
        "cloud.rest.readings.freeChlorine",
        "cloud.rest.readings.lsi",
        "cloud.rest.readings.phStc20",
        "features.waterlab.zone1.temp_c",
    ]


@pytest.mark.parametrize("path", _shadow_paths_exercising_infer_hints())
def test_apply_numeric_shadow_sensor_hints_device_class_state_class_matrix(
    path: str,
) -> None:
    ent = SimpleNamespace()
    shadow_metrics.apply_numeric_shadow_sensor_hints(ent, path)
    dc = getattr(ent, "_attr_device_class", None)
    sc = getattr(ent, "_attr_state_class", None)
    assert sc is not None, f"{path}: state_class required for statistics / graphs"
    if dc is None:
        return
    allowed = DEVICE_CLASS_STATE_CLASSES.get(dc)
    assert allowed is not None, f"{path}: unknown device_class {dc!r}"
    assert sc in allowed, f"{path}: {dc=} {sc=} not in {allowed}"


def test_gecko_sensor_classes_static_device_class_state_class_matrix() -> None:
    """Classes that declare both attrs on the class body must match HA's matrix.

    Home Assistant 2025+ stores class defaults on ``__attr_*`` keys; ``_attr_*``
    names are properties and must not be read from ``cls.__dict__`` directly.
    """
    for _, cls in inspect.getmembers(gecko_sensor, inspect.isclass):
        if not issubclass(cls, SensorEntity) or cls is SensorEntity:
            continue
        if cls.__module__ != gecko_sensor.__name__:
            continue
        dct = cls.__dict__
        dc = dct.get("__attr_device_class")
        sc = dct.get("__attr_state_class")
        if dc is None or sc is None:
            continue
        if isinstance(dc, property) or isinstance(sc, property):
            continue
        allowed = DEVICE_CLASS_STATE_CLASSES.get(dc)
        assert allowed is not None, f"{cls.__name__}: unknown device_class {dc!r}"
        assert sc in allowed, f"{cls.__name__}: {dc=} {sc=} not in {allowed}"
