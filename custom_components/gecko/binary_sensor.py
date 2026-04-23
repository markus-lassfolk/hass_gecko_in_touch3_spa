"""Binary sensor entities for Gecko spa integration."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from gecko_iot_client.models.flow_zone import FlowZone
from gecko_iot_client.models.temperature_control_zone import TemperatureControlZone
from gecko_iot_client.models.zone_types import ZoneType
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .connection_manager import GECKO_CONNECTION_MANAGER_KEY
from .const import CONF_ALERTS_POLL_INTERVAL, DEFAULT_ALERTS_POLL_INTERVAL, DOMAIN
from .coordinator import GeckoVesselCoordinator
from .entity import GeckoEntityAvailabilityMixin, gecko_zone_ids_equal
from .shadow_metrics import (
    binary_extension_enabled_by_default,
    classify_gecko_shadow_metric,
    humanize_shadow_path,
    infer_binary_sensor_device_class,
)
from .telemetry import (
    get_flow_initiators,
    get_flow_manual_demand_reason,
    get_flow_runtime_state,
    is_manual_flow_demand,
)

_LOGGER = logging.getLogger(__name__)

BINARY_SENSOR_DESCRIPTIONS: tuple[BinarySensorEntityDescription, ...] = (
    BinarySensorEntityDescription(
        key="gateway_status",
        translation_key="gateway_status",
        icon="mdi:router-wireless",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
    BinarySensorEntityDescription(
        key="vessel_status",
        translation_key="vessel_status",
        icon="mdi:hot-tub",
        device_class=BinarySensorDeviceClass.RUNNING,
    ),
    BinarySensorEntityDescription(
        key="transport_connection",
        translation_key="transport_connection",
        icon="mdi:cloud-check",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
    BinarySensorEntityDescription(
        key="overall_connection",
        translation_key="overall_connection",
        icon="mdi:connection",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
    BinarySensorEntityDescription(
        key="is_energy_saving",
        translation_key="is_energy_saving",
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
    if not hasattr(config_entry, "runtime_data") or not config_entry.runtime_data:
        _LOGGER.error("No runtime_data found for config entry")
        return

    coordinators = config_entry.runtime_data.coordinators
    if not coordinators:
        _LOGGER.warning("No vessel coordinators found")
        return

    # Track created temperature-zone sensors per vessel to avoid duplicates on
    # repeated zone-update callbacks.
    added_eco_zone_ids: dict[str, set[str]] = {}
    added_heating_zone_ids: dict[str, set[str]] = {}

    def create_temp_zone_discovery_callback(coordinator: GeckoVesselCoordinator):
        """Return a callback that discovers eco/heating sensors for new temp zones."""

        @callback
        def discover_temp_zone_sensors() -> None:
            vessel_key = f"{coordinator.entry_id}_{coordinator.vessel_id}"
            added_eco_zone_ids.setdefault(vessel_key, set())
            added_heating_zone_ids.setdefault(vessel_key, set())

            new_entities: list[BinarySensorEntity] = []
            for zone in coordinator.get_zones_by_type(
                ZoneType.TEMPERATURE_CONTROL_ZONE
            ):
                if not isinstance(zone, TemperatureControlZone):
                    continue
                zone_id = str(zone.id)
                if zone_id not in added_eco_zone_ids[vessel_key]:
                    new_entities.append(GeckoEcoModeBinarySensor(coordinator, zone))
                    added_eco_zone_ids[vessel_key].add(zone_id)
                if zone_id not in added_heating_zone_ids[vessel_key]:
                    new_entities.append(
                        GeckoTemperatureHeatingBinarySensor(coordinator, zone)
                    )
                    added_heating_zone_ids[vessel_key].add(zone_id)

            if new_entities:
                async_add_entities(new_entities)

        return discover_temp_zone_sensors

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

        # Vessel-level derived sensors (one per vessel regardless of zone count)
        entities.append(GeckoSpaInUseBinarySensor(coordinator, config_entry))
        entities.append(GeckoVesselHeatingBinarySensor(coordinator, config_entry))
        entities.append(GeckoCleaningModeBinarySensor(coordinator, config_entry))

        if (
            int(
                config_entry.options.get(
                    CONF_ALERTS_POLL_INTERVAL, DEFAULT_ALERTS_POLL_INTERVAL
                )
            )
            > 0
        ):
            entities.append(
                GeckoRestActiveAlertsBinarySensor(coordinator, config_entry)
            )

        for path in coordinator.take_pending_bool_paths():
            entities.append(
                GeckoShadowBoolBinarySensor(coordinator, config_entry, path)
            )

        @callback
        def _on_shadow_metric_discovery(
            coord: GeckoVesselCoordinator = coordinator,
        ) -> None:
            added = coord.take_pending_bool_paths()
            if not added:
                return
            async_add_entities(
                [GeckoShadowBoolBinarySensor(coord, config_entry, p) for p in added]
            )

        coordinator.register_shadow_metric_callback(_on_shadow_metric_discovery)

        # Discover temperature-zone sensors now, and re-run on every zone update
        temp_zone_callback = create_temp_zone_discovery_callback(coordinator)
        temp_zone_callback()
        coordinator.register_zone_update_callback(temp_zone_callback)

    if entities:
        _LOGGER.debug("Adding %d binary sensor entities", len(entities))
        async_add_entities(entities)
    else:
        _LOGGER.debug("No binary sensor entities created")


class GeckoBinarySensorEntity(
    CoordinatorEntity[GeckoVesselCoordinator], BinarySensorEntity
):
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

        self._attr_unique_id = (
            f"{config_entry.entry_id}_{coordinator.vessel_id}_{description.key}"
        )
        # Device info for grouping entities
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.vessel_id))},
        )
        self._attr_is_on: bool | None = None

    async def async_added_to_hass(self) -> None:
        """Called when entity is added to hass."""
        await super().async_added_to_hass()

        # Update state immediately when added to hass
        self._update_state()
        _LOGGER.debug(
            "Binary sensor %s added to hass with initial state: %s",
            self.entity_description.key,
            self._attr_is_on,
        )

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
                    self._attr_is_on = (
                        gecko_client.operation_mode_controller.is_energy_saving
                    )
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
            _LOGGER.debug(
                "Error updating binary sensor state for %s: %s",
                self.entity_description.key,
                e,
            )
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
            _LOGGER.warning(
                "Error updating connectivity binary sensor %s: %s",
                self.entity_description.key,
                e,
            )
            self._attr_is_on = False


class GeckoShadowBoolBinarySensor(
    GeckoEntityAvailabilityMixin, CoordinatorEntity, BinarySensorEntity
):
    """Boolean leaves from shadow (alarms, flags, etc.)."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    @property
    def available(self) -> bool:
        """REST-derived bools stay available with a value when MQTT is down."""
        if self._path.startswith("cloud.rest."):
            return self.coordinator.get_shadow_bool_value(self._path) is not None
        return super().available

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        config_entry: ConfigEntry,
        path: str,
    ) -> None:
        super().__init__(coordinator)
        self._path = path
        self._attr_name = humanize_shadow_path(path)
        path_hash = hashlib.sha256(path.encode("utf-8")).hexdigest()[:8]
        self._attr_unique_id = (
            f"{config_entry.entry_id}_{coordinator.monitor_id}_"
            f"bool_{path.replace('.', '_')}_{path_hash}"
        )
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
        self._attr_available = False

    @callback
    def _handle_coordinator_update(self) -> None:
        val = self.coordinator.get_shadow_bool_value(self._path)
        self._attr_is_on = bool(val) if val is not None else False
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        val = self.coordinator.get_shadow_bool_value(self._path)
        self._attr_is_on = bool(val) if val is not None else False


class GeckoSpaInUseBinarySensor(
    GeckoEntityAvailabilityMixin,
    CoordinatorEntity[GeckoVesselCoordinator],
    BinarySensorEntity,
):
    """True when the spa is actively in use (lights on or manual pump demand)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:hot-tub"

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = "spa_in_use"
        self._attr_unique_id = (
            f"{config_entry.entry_id}_{coordinator.vessel_id}_spa_in_use"
        )
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.vessel_id))},
        )
        self._attr_available = False
        self._active_light_zone_ids: list[str] = []
        self._manual_flow_zone_ids: list[str] = []
        self._active_flow_zone_ids: list[str] = []
        self._flow_initiators_by_zone_id: dict[str, list[str]] = {}
        self._flow_manual_reason_by_zone_id: dict[str, str] = {}
        self._raw_flow_state_by_zone_id: dict[str, dict[str, Any]] = {}
        self._update_state()

    def _update_state(self) -> None:
        light_zones = self.coordinator.get_zones_by_type(ZoneType.LIGHTING_ZONE)
        flow_zones = self.coordinator.get_zones_by_type(ZoneType.FLOW_ZONE)
        temperature_zones = self.coordinator.get_zones_by_type(
            ZoneType.TEMPERATURE_CONTROL_ZONE
        )
        spa_state = self.coordinator.get_spa_state()

        self._active_light_zone_ids = [
            str(zone.id) for zone in light_zones if getattr(zone, "active", False)
        ]
        self._active_flow_zone_ids = [
            str(zone.id) for zone in flow_zones if getattr(zone, "active", False)
        ]
        self._flow_initiators_by_zone_id = {
            str(zone.id): sorted(get_flow_initiators(zone, spa_state))
            for zone in flow_zones
            if isinstance(zone, FlowZone)
        }
        self._flow_manual_reason_by_zone_id = {
            str(zone.id): get_flow_manual_demand_reason(
                zone, spa_state, temperature_zones
            )
            for zone in flow_zones
            if isinstance(zone, FlowZone)
        }
        self._raw_flow_state_by_zone_id = {
            str(zone.id): get_flow_runtime_state(zone, spa_state)
            for zone in flow_zones
            if isinstance(zone, FlowZone)
        }
        self._manual_flow_zone_ids = [
            str(zone.id)
            for zone in flow_zones
            if isinstance(zone, FlowZone)
            and is_manual_flow_demand(zone, spa_state, temperature_zones)
        ]
        self._attr_is_on = bool(
            self._active_light_zone_ids or self._manual_flow_zone_ids
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "active_light_zone_ids": self._active_light_zone_ids,
            "manual_flow_zone_ids": self._manual_flow_zone_ids,
            "active_flow_zone_ids": self._active_flow_zone_ids,
            "flow_initiators_by_zone_id": self._flow_initiators_by_zone_id,
            "flow_manual_reason_by_zone_id": self._flow_manual_reason_by_zone_id,
            "raw_flow_state_by_zone_id": self._raw_flow_state_by_zone_id,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_state()
        self.async_write_ha_state()


class GeckoTemperatureZoneBinarySensorBase(
    GeckoEntityAvailabilityMixin,
    CoordinatorEntity[GeckoVesselCoordinator],
    BinarySensorEntity,
):
    """Base class for binary sensors tracking a single temperature control zone."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        zone: TemperatureControlZone,
    ) -> None:
        super().__init__(coordinator)
        self._zone = zone
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.vessel_id))},
        )
        self._attr_available = False

    def _update_state(self) -> None:
        """Subclasses sync the zone reference and set ``_attr_is_on``."""
        raise NotImplementedError

    def _sync_zone_from_coordinator(self) -> None:
        """Re-bind ``self._zone`` to the live model after each coordinator refresh."""
        zones = self.coordinator.get_zones_by_type(ZoneType.TEMPERATURE_CONTROL_ZONE)
        my_id = getattr(self._zone, "id", None)
        for z in zones:
            if gecko_zone_ids_equal(getattr(z, "id", None), my_id):
                self._zone = z
                return

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "status": self._zone.status.name if self._zone.status else None,
            "current_temperature": self._zone.temperature,
            "target_temperature": self._zone.target_temperature,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_state()
        self.async_write_ha_state()


class GeckoEcoModeBinarySensor(GeckoTemperatureZoneBinarySensorBase):
    """Eco mode state for a single temperature control zone."""

    _attr_icon = "mdi:leaf"

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        zone: TemperatureControlZone,
    ) -> None:
        super().__init__(coordinator, zone)
        self._attr_translation_key = "eco_mode"
        self._attr_unique_id = (
            f"{coordinator.entry_id}_{coordinator.vessel_id}_eco_mode_{zone.id}"
        )
        self._update_state()

    def _update_state(self) -> None:
        self._sync_zone_from_coordinator()
        mode = getattr(self._zone, "mode", None)
        self._attr_is_on = bool(mode and getattr(mode, "eco", False))


class GeckoTemperatureHeatingBinarySensor(GeckoTemperatureZoneBinarySensorBase):
    """Heating state for a single temperature control zone."""

    _attr_icon = "mdi:fire"

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        zone: TemperatureControlZone,
    ) -> None:
        super().__init__(coordinator, zone)
        self._attr_translation_key = "zone_heating"
        self._attr_unique_id = (
            f"{coordinator.entry_id}_{coordinator.vessel_id}_heating_{zone.id}"
        )
        self._update_state()

    def _update_state(self) -> None:
        self._sync_zone_from_coordinator()
        status = getattr(self._zone, "status", None)
        self._attr_is_on = bool(status and getattr(status, "is_heating", False))


class GeckoVesselHeatingBinarySensor(
    GeckoEntityAvailabilityMixin,
    CoordinatorEntity[GeckoVesselCoordinator],
    BinarySensorEntity,
):
    """Aggregate heating state across all temperature zones for a vessel."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:fire"

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = "vessel_heating"
        self._attr_unique_id = (
            f"{config_entry.entry_id}_{coordinator.vessel_id}_heating"
        )
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.vessel_id))},
        )
        self._attr_available = False
        self._heating_zone_ids: list[str] = []
        self._temperature_zone_count = 0
        self._update_state()

    def _update_state(self) -> None:
        temperature_zones = self.coordinator.get_zones_by_type(
            ZoneType.TEMPERATURE_CONTROL_ZONE
        )
        self._temperature_zone_count = len(temperature_zones)
        self._heating_zone_ids = [
            str(zone.id)
            for zone in temperature_zones
            if isinstance(zone, TemperatureControlZone)
            and getattr(zone, "status", None)
            and getattr(zone.status, "is_heating", False)
        ]
        self._attr_is_on = bool(self._heating_zone_ids)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "heating_zone_ids": self._heating_zone_ids,
            "heating_zone_count": len(self._heating_zone_ids),
            "temperature_zone_count": self._temperature_zone_count,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_state()
        self.async_write_ha_state()


class GeckoCleaningModeBinarySensor(
    GeckoEntityAvailabilityMixin,
    CoordinatorEntity[GeckoVesselCoordinator],
    BinarySensorEntity,
):
    """True when the vessel operation mode indicates cleaning."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:spray-bottle"

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = "cleaning_mode"
        self._attr_unique_id = (
            f"{config_entry.entry_id}_{coordinator.vessel_id}_cleaning_mode"
        )
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.vessel_id))},
        )
        self._attr_available = False
        self._mode_name: str | None = None
        self._operation_mode_raw: str | None = None

    def _is_cleaning_from_status(self, status: Any) -> bool:
        for attr in ("is_cleaning", "cleaning", "cleaning_mode", "is_cleaning_mode"):
            value = getattr(status, attr, None)
            # Only short-circuit on explicit True — False may be stale while
            # mode_name / operation_mode still indicate a cleaning cycle.
            if isinstance(value, bool) and value:
                return True
        mode_name = getattr(status, "mode_name", None)
        if mode_name and "clean" in str(mode_name).lower():
            return True
        operation_mode = getattr(status, "operation_mode", None)
        if operation_mode is not None:
            combined = (
                f"{getattr(operation_mode, 'name', '')} "
                f"{getattr(operation_mode, 'value', '')}"
            ).lower()
            if "clean" in combined:
                return True
        return False

    def _update_state(self) -> None:
        self._mode_name = None
        self._operation_mode_raw = None
        self._attr_is_on = False
        try:
            status = self.coordinator.get_cached_operation_mode_status()
            if not status:
                return
            mode_name = getattr(status, "mode_name", None)
            self._mode_name = str(mode_name) if mode_name is not None else None
            operation_mode = getattr(status, "operation_mode", None)
            if operation_mode is not None:
                n = getattr(operation_mode, "name", None)
                v = getattr(operation_mode, "value", None)
                self._operation_mode_raw = (
                    f"{n}:{v}"
                    if (n is not None or v is not None)
                    else str(operation_mode)
                )
            self._attr_is_on = self._is_cleaning_from_status(status)
        except Exception as ex:
            _LOGGER.debug(
                "Could not update cleaning mode for %s: %s", self._attr_name, ex
            )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._update_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "mode_name": self._mode_name,
            "operation_mode": self._operation_mode_raw,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_state()
        self.async_write_ha_state()


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
        self._attr_translation_key = "active_alerts"
        self._attr_unique_id = (
            f"{config_entry.entry_id}_{coordinator.monitor_id}_rest_active_alerts_bin"
        )
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
