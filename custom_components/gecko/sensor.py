"""Sensors for Gecko: extension shadow metrics (e.g. Waterlab) and connectivity."""

from __future__ import annotations

import hashlib
import logging
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GeckoVesselCoordinator
from .entity import GeckoEntityAvailabilityMixin
from .shadow_metrics import (
    apply_numeric_shadow_sensor_hints,
    chemistry_metric_enabled_by_default,
    classify_gecko_shadow_metric,
    metric_path_to_entity_slug,
    shadow_extension_diagnostic_disables_registry_default,
    shadow_metric_icon,
    string_extension_enabled_by_default,
)

_LOGGER = logging.getLogger(__name__)


def _humanize_metric_name(path: str) -> str:
    """Short display name from dotted path."""
    tail = path.split(".")[-1]
    return tail.replace("_", " ").strip().title() or path


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Gecko sensor entities."""
    if not hasattr(config_entry, "runtime_data") or not config_entry.runtime_data:
        _LOGGER.error("No runtime_data found for config entry")
        return

    coordinators = config_entry.runtime_data.coordinators
    if not coordinators:
        _LOGGER.warning("No vessel coordinators found")
        return

    initial_entities: list[SensorEntity] = []

    for coordinator in coordinators:
        await coordinator.async_ensure_initial_setup()
        pending = coordinator.take_pending_new_metric_paths()
        if pending:
            initial_entities.extend(
                GeckoShadowMetricSensor(coordinator, config_entry, p)
                for p in pending
            )
        pending_str = coordinator.take_pending_string_paths()
        if pending_str:
            initial_entities.extend(
                GeckoShadowStringSensor(coordinator, config_entry, p)
                for p in pending_str
            )

        @callback
        def _on_coordinator_listener(
            coord: GeckoVesselCoordinator = coordinator,
        ) -> None:
            added = coord.take_pending_new_metric_paths()
            if added:
                async_add_entities(
                    [
                        GeckoShadowMetricSensor(coord, config_entry, path)
                        for path in added
                    ]
                )
            added_s = coord.take_pending_string_paths()
            if added_s:
                async_add_entities(
                    [
                        GeckoShadowStringSensor(coord, config_entry, path)
                        for path in added_s
                    ]
                )

        config_entry.async_on_unload(
            coordinator.async_add_listener(_on_coordinator_listener)
        )

    if initial_entities:
        async_add_entities(initial_entities)

    alert_entities = [
        GeckoRestActiveAlertsSensor(coordinator, config_entry)
        for coordinator in coordinators
    ]
    async_add_entities(alert_entities)


class GeckoShadowMetricSensor(
    GeckoEntityAvailabilityMixin, CoordinatorEntity, SensorEntity
):
    """Numeric metric parsed from device shadow outside modeled zone types."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    @property
    def available(self) -> bool:
        """REST tile metrics stay available with a value even when MQTT is down."""
        if self._metric_path.startswith("cloud.rest."):
            return self.coordinator.get_shadow_metric_value(self._metric_path) is not None
        return GeckoEntityAvailabilityMixin.available.fget(self)

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        config_entry: ConfigEntry,
        metric_path: str,
    ) -> None:
        super().__init__(coordinator)
        self._metric_path = metric_path
        self._config_entry = config_entry

        vessel_slug = coordinator.vessel_name.lower().replace(" ", "_").replace(
            "-", "_"
        )
        path_slug = metric_path_to_entity_slug(metric_path)
        self._attr_name = _humanize_metric_name(metric_path)
        self._attr_extra_state_attributes = {
            "shadow_path": metric_path,
            "gecko_diagnostic_group": classify_gecko_shadow_metric(metric_path),
        }
        path_hash = hashlib.sha256(metric_path.encode("utf-8")).hexdigest()[:8]
        self._attr_unique_id = (
            f"{config_entry.entry_id}_{coordinator.monitor_id}_"
            f"{metric_path.replace('.', '_')}_{path_hash}"
        )
        self.entity_id = f"sensor.{vessel_slug}_{path_slug}"

        apply_numeric_shadow_sensor_hints(self, metric_path)

        chem_on = chemistry_metric_enabled_by_default(metric_path)
        diag_off = shadow_extension_diagnostic_disables_registry_default(metric_path)
        if chem_on and not diag_off:
            self._attr_entity_registry_enabled_default = True
            self._attr_entity_category = None
        else:
            self._attr_entity_registry_enabled_default = False
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

        self._attr_icon = shadow_metric_icon(metric_path)
        self._attr_native_value = coordinator.get_shadow_metric_value(metric_path)

        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.vessel_id))},
        )
        self._attr_available = False

    @callback
    def _handle_coordinator_update(self) -> None:
        """Refresh value when coordinator or shadow metrics update."""
        self._attr_native_value = self.coordinator.get_shadow_metric_value(
            self._metric_path
        )
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Register availability mixin and set initial value."""
        await super().async_added_to_hass()
        self._attr_native_value = self.coordinator.get_shadow_metric_value(
            self._metric_path
        )


class GeckoShadowStringSensor(
    GeckoEntityAvailabilityMixin, CoordinatorEntity, SensorEntity
):
    """String leaves from shadow / REST (status text, messages)."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    @property
    def available(self) -> bool:
        if self._path.startswith("cloud.rest."):
            return self.coordinator.get_shadow_string_value(self._path) is not None
        return GeckoEntityAvailabilityMixin.available.fget(self)

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        config_entry: ConfigEntry,
        path: str,
    ) -> None:
        super().__init__(coordinator)
        self._path = path
        self._config_entry = config_entry
        vessel_slug = coordinator.vessel_name.lower().replace(" ", "_").replace(
            "-", "_"
        )
        path_slug = metric_path_to_entity_slug(path)
        self._attr_name = _humanize_metric_name(path)
        self._attr_unique_id = (
            f"{config_entry.entry_id}_{coordinator.monitor_id}_str_{path_slug}"
        )
        self.entity_id = f"sensor.{vessel_slug}_str_{path_slug}"
        self._attr_extra_state_attributes = {
            "shadow_path": path,
            "gecko_diagnostic_group": classify_gecko_shadow_metric(path),
        }
        self._attr_native_value = coordinator.get_shadow_string_value(path)
        self._attr_icon = shadow_metric_icon(path)
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.vessel_id))},
        )
        if string_extension_enabled_by_default(path):
            self._attr_entity_registry_enabled_default = True
            self._attr_entity_category = None
        else:
            self._attr_entity_registry_enabled_default = False
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_available = False

    @callback
    def _handle_coordinator_update(self) -> None:
        self._attr_native_value = self.coordinator.get_shadow_string_value(self._path)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._attr_native_value = self.coordinator.get_shadow_string_value(self._path)


class GeckoRestActiveAlertsSensor(CoordinatorEntity, SensorEntity):
    """Count of active REST alerts (unread messages scoped to vessel + open actions)."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = None
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        vessel_slug = coordinator.vessel_name.lower().replace(" ", "_").replace(
            "-", "_"
        )
        self._attr_name = "Active alerts (REST)"
        self._attr_unique_id = (
            f"{config_entry.entry_id}_{coordinator.monitor_id}_rest_active_alerts"
        )
        self.entity_id = f"sensor.{vessel_slug}_rest_active_alerts"
        self._attr_icon = "mdi:bell-badge"
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.vessel_id))},
        )
        self._refresh_from_snapshot()

    def _refresh_from_snapshot(self) -> None:
        snap = self.coordinator.get_rest_alerts_snapshot()
        self._attr_native_value = int(snap.get("total") or 0)
        self._attr_extra_state_attributes = {
            "messages": snap.get("messages") or [],
            "actions": snap.get("actions") or [],
            "updated_at": snap.get("updated_at"),
            "error": snap.get("error"),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        self._refresh_from_snapshot()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._refresh_from_snapshot()
