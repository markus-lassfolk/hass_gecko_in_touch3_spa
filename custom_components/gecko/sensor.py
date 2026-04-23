"""Sensors for Gecko: extension shadow metrics (e.g. Waterlab) and connectivity."""

from __future__ import annotations

import hashlib
import logging
from typing import Literal

from gecko_iot_client.models.zone_types import ZoneType
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfEnergy,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ALERTS_POLL_INTERVAL, DEFAULT_ALERTS_POLL_INTERVAL, DOMAIN
from .coordinator import GeckoVesselCoordinator
from .energy_parse import (
    _coerce_energy_consumption_kwh,
    _coerce_energy_cost_amount,
    _coerce_energy_score_value,
)
from .entity import GeckoEntityAvailabilityMixin, gecko_zone_ids_equal
from .shadow_metrics import (
    apply_numeric_shadow_sensor_hints,
    chemistry_metric_enabled_by_default,
    classify_gecko_shadow_metric,
    humanize_shadow_path,
    shadow_extension_diagnostic_disables_registry_default,
    shadow_metric_icon,
    string_extension_enabled_by_default,
)

_LOGGER = logging.getLogger(__name__)

SpaTempKind = Literal["target", "current"]


class GeckoSpaTemperatureSensor(
    GeckoEntityAvailabilityMixin, CoordinatorEntity, SensorEntity
):
    """Numeric mirror of spa thermostat zone temps for cards that only accept ``sensor``."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        config_entry: ConfigEntry,
        zone_id: int,
        kind: SpaTempKind,
    ) -> None:
        super().__init__(coordinator)
        self._zone_id = zone_id
        self._kind: SpaTempKind = kind
        self._attr_translation_key = (
            "spa_target_temperature" if kind == "target" else "spa_current_temperature"
        )
        self._attr_translation_placeholders = {"zone_id": str(zone_id)}
        self._attr_unique_id = (
            f"{config_entry.entry_id}_{coordinator.vessel_id}_"
            f"spa_{kind}_temperature_{zone_id}"
        )
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.vessel_id))},
        )
        self._attr_available = False
        self._update_native_value()

    def _get_zone(self):
        for z in self.coordinator.get_zones_by_type(ZoneType.TEMPERATURE_CONTROL_ZONE):
            if gecko_zone_ids_equal(getattr(z, "id", None), self._zone_id):
                return z
        return None

    def _update_native_value(self) -> None:
        zone = self._get_zone()
        if not zone:
            self._attr_native_value = None
            return
        try:
            if self._kind == "target":
                raw = getattr(zone, "target_temperature", None)
            else:
                raw = getattr(zone, "temperature", None)
            self._attr_native_value = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            self._attr_native_value = None

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_native_value()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._update_native_value()


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
    spa_temp_zone_ids: dict[str, set[int]] = {}

    for coordinator in coordinators:
        pending = coordinator.take_pending_new_metric_paths()
        if pending:
            initial_entities.extend(
                GeckoShadowMetricSensor(coordinator, config_entry, p) for p in pending
            )
        pending_str = coordinator.take_pending_string_paths()
        if pending_str:
            initial_entities.extend(
                GeckoShadowStringSensor(coordinator, config_entry, p)
                for p in pending_str
            )

        @callback
        def _on_shadow_metric_discovery(
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

        coordinator.register_shadow_metric_callback(_on_shadow_metric_discovery)

        def _discover_spa_temperature_sensors(
            coord: GeckoVesselCoordinator = coordinator,
        ) -> None:
            vessel_key = f"{coord.entry_id}_{coord.vessel_id}"
            if vessel_key not in spa_temp_zone_ids:
                spa_temp_zone_ids[vessel_key] = set()
            zones = coord.get_zones_by_type(ZoneType.TEMPERATURE_CONTROL_ZONE)
            new_entities: list[SensorEntity] = []
            for zone in zones:
                zid = getattr(zone, "id", None)
                if zid is None:
                    continue
                zid_int = int(zid)
                if zid_int in spa_temp_zone_ids[vessel_key]:
                    continue
                new_entities.extend(
                    [
                        GeckoSpaTemperatureSensor(
                            coord, config_entry, zid_int, "target"
                        ),
                        GeckoSpaTemperatureSensor(
                            coord, config_entry, zid_int, "current"
                        ),
                    ]
                )
                spa_temp_zone_ids[vessel_key].add(zid_int)
            if new_entities:
                async_add_entities(new_entities)

        _discover_spa_temperature_sensors()

        def _on_zone_update_discover_spa_temps(
            coord: GeckoVesselCoordinator = coordinator,
        ) -> None:
            _discover_spa_temperature_sensors(coord)

        coordinator.register_zone_update_callback(_on_zone_update_discover_spa_temps)

    if initial_entities:
        async_add_entities(initial_entities)

    if (
        int(
            config_entry.options.get(
                CONF_ALERTS_POLL_INTERVAL, DEFAULT_ALERTS_POLL_INTERVAL
            )
        )
        > 0
    ):
        async_add_entities(
            [
                GeckoRestActiveAlertsSensor(coordinator, config_entry)
                for coordinator in coordinators
            ]
        )

    # Premium energy sensors — only when app token is linked
    if config_entry.data.get("app_token"):
        energy_entities: list[SensorEntity] = []
        for coordinator in coordinators:
            energy_entities.extend(
                [
                    GeckoEnergyConsumptionSensor(coordinator, config_entry),
                    GeckoEnergyCostSensor(coordinator, config_entry),
                    GeckoEnergyScoreSensor(coordinator, config_entry),
                ]
            )
        if energy_entities:
            async_add_entities(energy_entities)


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
            return (
                self.coordinator.get_shadow_metric_value(self._metric_path) is not None
            )
        return super().available

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        config_entry: ConfigEntry,
        metric_path: str,
    ) -> None:
        super().__init__(coordinator)
        self._metric_path = metric_path
        self._config_entry = config_entry

        self._attr_name = humanize_shadow_path(metric_path)
        self._attr_extra_state_attributes = {
            "shadow_path": metric_path,
            "gecko_diagnostic_group": classify_gecko_shadow_metric(metric_path),
        }
        path_hash = hashlib.sha256(metric_path.encode("utf-8")).hexdigest()[:8]
        self._attr_unique_id = (
            f"{config_entry.entry_id}_{coordinator.monitor_id}_"
            f"{metric_path.replace('.', '_')}_{path_hash}"
        )
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
        return super().available

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        config_entry: ConfigEntry,
        path: str,
    ) -> None:
        super().__init__(coordinator)
        self._path = path
        self._config_entry = config_entry
        self._attr_name = humanize_shadow_path(path)
        path_hash = hashlib.sha256(path.encode("utf-8")).hexdigest()[:8]
        self._attr_unique_id = (
            f"{config_entry.entry_id}_{coordinator.monitor_id}_"
            f"str_{path.replace('.', '_')}_{path_hash}"
        )
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
        self._attr_translation_key = "active_alerts_rest"
        self._attr_unique_id = (
            f"{config_entry.entry_id}_{coordinator.monitor_id}_rest_active_alerts"
        )
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


# ---------------------------------------------------------------------------
# Premium energy sensors (require app-client token)
# ---------------------------------------------------------------------------


class GeckoEnergyConsumptionSensor(
    GeckoEntityAvailabilityMixin, CoordinatorEntity, SensorEntity
):
    """Total energy consumed by the spa (kWh).

    Compatible with the HA Energy Dashboard as an individual device
    consumption source (device_class=ENERGY, state_class=TOTAL_INCREASING).
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "energy_consumption"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:lightning-bolt"
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{config_entry.entry_id}_{coordinator.vessel_id}_energy_consumption"
        )
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.vessel_id))},
        )
        self._attr_available = False
        self._refresh_value()

    def _refresh_value(self) -> None:
        energy = self.coordinator.get_energy_data()
        raw = energy.get("consumption")
        if raw is None:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return

        val = _coerce_energy_consumption_kwh(raw)

        self._attr_native_value = val
        self._attr_extra_state_attributes = (
            {"raw_response": raw} if isinstance(raw, dict) else {}
        )

    @property
    def available(self) -> bool:
        """REST energy consumption stays available when MQTT transport is disconnected."""
        if self.coordinator.has_premium_energy_api():
            return self._attr_native_value is not None
        return super().available

    @callback
    def _handle_coordinator_update(self) -> None:
        self._refresh_value()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._refresh_value()


class GeckoEnergyCostSensor(
    GeckoEntityAvailabilityMixin, CoordinatorEntity, SensorEntity
):
    """Estimated energy cost for the spa.

    Uses MONETARY device class so HA can track it alongside consumption.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "energy_cost"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:currency-usd"
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{config_entry.entry_id}_{coordinator.vessel_id}_energy_cost"
        )
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.vessel_id))},
        )
        self._attr_available = False
        self._latched_currency: str | None = None
        self._refresh_value()

    def _refresh_value(self) -> None:
        energy = self.coordinator.get_energy_data()
        raw = energy.get("cost")
        if raw is None:
            self._attr_native_value = None
            if self._latched_currency is None:
                self._attr_native_unit_of_measurement = None
            self._attr_extra_state_attributes = {}
            return

        val = _coerce_energy_cost_amount(raw)

        currency = None
        if isinstance(raw, dict):
            currency = raw.get("currency") or raw.get("currencyCode") or raw.get("unit")

        if self._latched_currency is None and currency:
            self._latched_currency = currency
            self._attr_native_unit_of_measurement = currency
        elif self._latched_currency:
            self._attr_native_unit_of_measurement = self._latched_currency

        self._attr_native_value = val
        self._attr_extra_state_attributes = (
            {"raw_response": raw} if isinstance(raw, dict) else {}
        )

    @property
    def available(self) -> bool:
        """REST energy cost stays available when MQTT transport is disconnected."""
        if self.coordinator.has_premium_energy_api():
            return self._attr_native_value is not None
        return super().available

    @callback
    def _handle_coordinator_update(self) -> None:
        self._refresh_value()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._refresh_value()


class GeckoEnergyScoreSensor(
    GeckoEntityAvailabilityMixin, CoordinatorEntity, SensorEntity
):
    """Energy efficiency score from the Gecko app."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "energy_score"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:leaf"
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{config_entry.entry_id}_{coordinator.vessel_id}_energy_score"
        )
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.vessel_id))},
        )
        self._attr_available = False
        self._refresh_value()

    def _refresh_value(self) -> None:
        energy = self.coordinator.get_energy_data()
        raw = energy.get("score")
        if raw is None:
            self._attr_native_value = None
            self._attr_native_unit_of_measurement = None
            self._attr_extra_state_attributes = {}
            return

        val = _coerce_energy_score_value(raw)

        unit: str | None = None
        if isinstance(raw, dict):
            u = raw.get("unit") or raw.get("scale")
            if u is not None and str(u).strip():
                unit = str(u).strip()
        # Do not assume "%" for scalar payloads — wrong long-term statistics semantics.
        self._attr_native_unit_of_measurement = unit

        self._attr_native_value = val
        self._attr_extra_state_attributes = (
            {"raw_response": raw} if isinstance(raw, dict) else {}
        )

    @property
    def available(self) -> bool:
        """REST energy score stays available when MQTT transport is disconnected."""
        if self.coordinator.has_premium_energy_api():
            return self._attr_native_value is not None
        return super().available

    @callback
    def _handle_coordinator_update(self) -> None:
        self._refresh_value()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._refresh_value()
