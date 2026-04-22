"""Binary sensor entities for Gecko spa integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
    BinarySensorDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GeckoVesselCoordinator
from .connection_manager import GECKO_CONNECTION_MANAGER_KEY
from .entity import GeckoEntityAvailabilityMixin
from .shadow_metrics import (
    binary_extension_enabled_by_default,
    classify_gecko_shadow_metric,
    infer_binary_sensor_device_class,
    metric_path_to_entity_slug,
)

_LOGGER = logging.getLogger(__name__)

BINARY_SENSOR_DESCRIPTIONS: tuple[BinarySensorEntityDescription, ...] = (
    BinarySensorEntityDescription(
        key="gateway_status",
        name="Gateway Status",
        icon="mdi:router-wireless",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
    BinarySensorEntityDescription(
        key="vessel_status",
        name="Spa Status", 
        icon="mdi:hot-tub",
        device_class=BinarySensorDeviceClass.RUNNING,
    ),
    BinarySensorEntityDescription(
        key="transport_connection",
        name="Transport Connection",
        icon="mdi:cloud-check",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
    BinarySensorEntityDescription(
        key="overall_connection",
        name="Overall Connection",
        icon="mdi:connection",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
    BinarySensorEntityDescription(
        key="is_energy_saving",
        name="Energy Saving Mode",
        icon="mdi:leaf",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Gecko binary sensor entities from a config entry."""
    
    # Get the vessel coordinators from runtime_data
    if not hasattr(config_entry, 'runtime_data') or not config_entry.runtime_data:
        _LOGGER.error("No runtime_data found for config entry")
        return
    
    coordinators = config_entry.runtime_data.coordinators
    if not coordinators:
        _LOGGER.warning("No vessel coordinators found")
        return
    
    entities: list[BinarySensorEntity] = []
    for coordinator in coordinators:
        for description in BINARY_SENSOR_DESCRIPTIONS:
            entity = GeckoBinarySensorEntity(
                coordinator=coordinator,
                config_entry=config_entry,
                description=description,
            )
            entities.append(entity)
            _LOGGER.debug(
                "Created binary sensor entity %s for %s",
                description.key,
                coordinator.vessel_name,
            )

        entities.append(
            GeckoRestActiveAlertsBinarySensor(coordinator, config_entry)
        )

        await coordinator.async_refresh()
        await coordinator.async_wait_for_initial_zone_data(timeout=15.0)
        client = await coordinator.get_gecko_client()
        coordinator.sync_refresh_shadow_metrics(client)
        for path in coordinator.take_pending_bool_paths():
            entities.append(
                GeckoShadowBoolBinarySensor(coordinator, config_entry, path)
            )

        @callback
        def _shadow_bool_listener(coord: GeckoVesselCoordinator = coordinator) -> None:
            added = coord.take_pending_bool_paths()
            if not added:
                return
            async_add_entities(
                [
                    GeckoShadowBoolBinarySensor(coord, config_entry, p)
                    for p in added
                ]
            )

        config_entry.async_on_unload(
            coordinator.async_add_listener(_shadow_bool_listener)
        )

    if entities:
        _LOGGER.debug("Adding %d binary sensor entities", len(entities))
        async_add_entities(entities)
    else:
        _LOGGER.debug("No binary sensor entities created")


class GeckoBinarySensorEntity(CoordinatorEntity[GeckoVesselCoordinator], BinarySensorEntity):
    """Representation of a Gecko binary sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        config_entry: ConfigEntry,
        description: BinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        
        self.entity_description = description
        self._monitor_id = coordinator.monitor_id
        self._vessel_name = coordinator.vessel_name
        self._vessel_id = coordinator.vessel_id
        
        # Set up entity attributes
        vessel_id_name = coordinator.vessel_name.lower().replace(" ", "_").replace("-", "_")
        self._attr_name = description.name
        self._attr_unique_id = f"{config_entry.entry_id}_{coordinator.vessel_id}_{description.key}"
        self.entity_id = f"binary_sensor.{vessel_id_name}_{description.key}"
        
        # Device info for grouping entities
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.vessel_id))},
        )

    async def async_added_to_hass(self) -> None:
        """Called when entity is added to hass."""
        await super().async_added_to_hass()
        
        # Update state immediately when added to hass
        self._update_state()
        _LOGGER.debug("Binary sensor %s added to hass with initial state: %s", self._attr_name, self._attr_is_on)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_state()
        self.async_write_ha_state()

    def _update_state(self) -> None:
        """Update the binary sensor state from spa data."""
        try:
            connection_manager = self.hass.data.get(GECKO_CONNECTION_MANAGER_KEY)

            if not connection_manager:
                self._attr_is_on = False
                return

            connection = connection_manager.get_connection(self._monitor_id)
            if not connection:
                self._attr_is_on = False
                return

            if self.entity_description.key == "is_energy_saving":
                gecko_client = connection.gecko_client
                if gecko_client and gecko_client.operation_mode_controller:
                    self._attr_is_on = gecko_client.operation_mode_controller.is_energy_saving
                else:
                    self._attr_is_on = False
                return

            connectivity_status = connection.connectivity_status
            if not connectivity_status and connection.gecko_client:
                connectivity_status = connection.gecko_client.connectivity_status

            if not connectivity_status:
                self._attr_is_on = False
                return

            self._update_connectivity_from_status(connectivity_status)

        except Exception as e:
            _LOGGER.debug("Error updating binary sensor state for %s: %s", self._attr_name, e)
            self._attr_is_on = False

    def _update_connectivity_from_status(self, connectivity_status) -> None:
        """Update connectivity binary sensor state from connectivity status object."""
        try:
            if self.entity_description.key == "gateway_status":
                # Gateway status is "connected" when connected
                status = str(connectivity_status.gateway_status).lower()
                self._attr_is_on = status == "connected"
                
            elif self.entity_description.key == "vessel_status":
                # Vessel status is "running" when running
                status = str(connectivity_status.vessel_status).lower()
                self._attr_is_on = status == "running"
                
            elif self.entity_description.key == "transport_connection":
                # Transport connection is a boolean
                self._attr_is_on = bool(connectivity_status.transport_connected)
                
            elif self.entity_description.key == "overall_connection":
                # Overall connection is fully connected or not
                self._attr_is_on = bool(connectivity_status.is_fully_connected)
                
        except Exception as e:
            _LOGGER.warning("Error updating connectivity binary sensor %s: %s", self._attr_name, e)
            self._attr_is_on = False


class GeckoShadowBoolBinarySensor(
    GeckoEntityAvailabilityMixin, CoordinatorEntity, BinarySensorEntity
):
    """Boolean leaves from shadow (alarms, flags, etc.)."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        config_entry: ConfigEntry,
        path: str,
    ) -> None:
        BinarySensorEntity.__init__(self)
        CoordinatorEntity.__init__(self, coordinator)
        self._path = path
        vessel_slug = coordinator.vessel_name.lower().replace(" ", "_").replace(
            "-", "_"
        )
        slug = metric_path_to_entity_slug(path)
        tail = path.split(".")[-1]
        self._attr_name = tail.replace("_", " ").strip().title() or tail
        self._attr_unique_id = (
            f"{config_entry.entry_id}_{coordinator.monitor_id}_bool_{slug}"
        )
        self.entity_id = f"binary_sensor.{vessel_slug}_bool_{slug}"
        self._attr_extra_state_attributes = {
            "shadow_path": path,
            "gecko_diagnostic_group": classify_gecko_shadow_metric(path),
        }
        dc = infer_binary_sensor_device_class(path)
        if dc is not None:
            self._attr_device_class = dc
        if binary_extension_enabled_by_default(path):
            self._attr_entity_registry_enabled_default = True
            self._attr_entity_category = None
        else:
            self._attr_entity_registry_enabled_default = False
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.vessel_id))},
        )
        raw = coordinator.get_shadow_bool_value(path)
        self._attr_is_on = bool(raw) if raw is not None else False

    @callback
    def _handle_coordinator_update(self) -> None:
        val = self.coordinator.get_shadow_bool_value(self._path)
        self._attr_is_on = bool(val) if val is not None else False
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        val = self.coordinator.get_shadow_bool_value(self._path)
        self._attr_is_on = bool(val) if val is not None else False


class GeckoRestActiveAlertsBinarySensor(
    CoordinatorEntity[GeckoVesselCoordinator], BinarySensorEntity
):
    """True when REST reports active vessel actions or scoped unread messages."""

    _attr_should_poll = False
    _attr_has_entity_name = True

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
        self._attr_name = "Active alerts"
        self._attr_unique_id = (
            f"{config_entry.entry_id}_{coordinator.monitor_id}_rest_active_alerts_bin"
        )
        self.entity_id = f"binary_sensor.{vessel_slug}_rest_active_alerts"
        self._attr_device_class = BinarySensorDeviceClass.PROBLEM
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.vessel_id))},
        )
        self._attr_icon = "mdi:bell-alert"
        self._refresh_from_snapshot()

    def _refresh_from_snapshot(self) -> None:
        snap = self.coordinator.get_rest_alerts_snapshot()
        total = int(snap.get("total") or 0)
        self._attr_is_on = total > 0
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
