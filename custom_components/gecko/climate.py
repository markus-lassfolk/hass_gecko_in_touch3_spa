"""Support for Gecko climate entities (temperature control)."""

from __future__ import annotations

import logging
from typing import Any

from gecko_iot_client.models.temperature_control_zone import (
    TemperatureControlZone,
    TemperatureControlZoneStatus,
)
from gecko_iot_client.models.zone_types import ZoneType
from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .connection_manager import async_get_connection_manager
from .const import DOMAIN
from .coordinator import GeckoVesselCoordinator
from .entity import GeckoEntityAvailabilityMixin, gecko_zone_ids_equal

_LOGGER = logging.getLogger(__name__)

# Map all TemperatureControlZoneStatus values to HVACAction
_HVAC_ACTION_MAP = {
    TemperatureControlZoneStatus.IDLE: HVACAction.IDLE,
    TemperatureControlZoneStatus.HEATING: HVACAction.HEATING,
    TemperatureControlZoneStatus.COOLING: HVACAction.COOLING,
    TemperatureControlZoneStatus.INVALID: HVACAction.IDLE,
    TemperatureControlZoneStatus.HEAT_PUMP_HEATING: HVACAction.HEATING,
    TemperatureControlZoneStatus.HEAT_PUMP_AND_HEATER_HEATING: HVACAction.HEATING,
    TemperatureControlZoneStatus.HEAT_PUMP_COOLING: HVACAction.COOLING,
    TemperatureControlZoneStatus.HEAT_PUMP_DEFROSTING: HVACAction.DEFROSTING,
    TemperatureControlZoneStatus.HEAT_PUMP_ERROR: HVACAction.IDLE,
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Gecko climate entities from a config entry."""
    coordinators = config_entry.runtime_data.coordinators

    # Track which zones have already been added for each coordinator
    added_zones: dict[str, set[int]] = {}

    @callback
    def discover_new_climate_entities(coordinator: GeckoVesselCoordinator) -> None:
        """Discover climate entities for temperature control zones."""
        zones = coordinator.get_zones_by_type(ZoneType.TEMPERATURE_CONTROL_ZONE)

        # Get or create the set of added zone IDs for this coordinator
        vessel_key = f"{coordinator.entry_id}_{coordinator.vessel_id}"
        if vessel_key not in added_zones:
            added_zones[vessel_key] = set()

        entities = []
        for zone in zones:
            if not hasattr(zone, "id"):
                _LOGGER.warning("Zone object missing 'id' attribute: %s", zone)
                continue

            # Only add if not already added
            if zone.id not in added_zones[vessel_key]:
                entities.append(GeckoClimate(coordinator, zone))
                added_zones[vessel_key].add(zone.id)

        if entities:
            async_add_entities(entities)
            _LOGGER.debug(
                "Added %d climate entities for vessel %s",
                len(entities),
                coordinator.vessel_name,
            )
        else:
            _LOGGER.debug(
                "No new climate entities to add for vessel %s", coordinator.vessel_name
            )

    # Set up initial entities and register for updates
    for coordinator in coordinators:
        discover_new_climate_entities(coordinator)
        coordinator.register_zone_update_callback(
            lambda coord=coordinator: discover_new_climate_entities(coord)
        )


class GeckoClimate(
    GeckoEntityAvailabilityMixin,
    CoordinatorEntity[GeckoVesselCoordinator],
    ClimateEntity,
):
    """Representation of a Gecko climate control."""

    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_hvac_modes = [HVACMode.HEAT]
    _attr_hvac_mode = HVACMode.HEAT
    _attr_target_temperature_step = 0.5

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        zone: TemperatureControlZone,
    ) -> None:
        """Initialize the climate control."""
        super().__init__(coordinator)
        self._zone = zone
        self._attr_unique_id = (
            f"{coordinator.entry_id}_{coordinator.vessel_id}_climate_{zone.id}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.vessel_id))}
        )
        self._attr_translation_key = "thermostat"
        self._attr_translation_placeholders = {"zone_id": str(zone.id)}

        # Set temperature limits
        self._attr_min_temp = self._zone.min_temperature_set_point_c
        self._attr_max_temp = self._zone.max_temperature_set_point_c

        # Initialize availability (will be set by async_added_to_hass event registration)
        self._attr_available = False

        # Initialize state from zone
        self._update_from_zone()

    def _sync_zone_from_coordinator(self) -> None:
        """Re-bind ``self._zone`` to the live model after each MQTT snapshot.

        The coordinator replaces ``_zones`` in ``on_zone_update``; keeping the
        original zone object would read stale temps and send setpoints nowhere.
        """
        zones = self.coordinator.get_zones_by_type(ZoneType.TEMPERATURE_CONTROL_ZONE)
        my_id = getattr(self._zone, "id", None)
        for z in zones:
            if gecko_zone_ids_equal(getattr(z, "id", None), my_id):
                self._zone = z
                return
        _LOGGER.debug(
            "Temperature zone id %r not in coordinator snapshot for %s",
            my_id,
            getattr(self, "entity_id", "?"),
        )

    def _update_from_zone(self) -> None:
        """Update state attributes from zone data."""
        self._sync_zone_from_coordinator()
        status = self._zone.status
        if status is None:
            self._attr_hvac_action = HVACAction.IDLE
        elif isinstance(status, TemperatureControlZoneStatus):
            self._attr_hvac_action = _HVAC_ACTION_MAP.get(status, HVACAction.IDLE)
        else:
            _LOGGER.warning(
                "Unexpected temperature zone status type %s (value=%r); expected %s",
                type(status).__name__,
                status,
                TemperatureControlZoneStatus.__name__,
            )
            self._attr_hvac_action = HVACAction.IDLE

        self._attr_current_temperature = self._zone.temperature
        self._attr_target_temperature = self._zone.target_temperature
        self._attr_max_temp = self._zone.max_temperature_set_point_c
        self._attr_min_temp = self._zone.min_temperature_set_point_c

        _LOGGER.debug(
            "Zone %s: current=%s°C, target=%s°C",
            self._zone.id,
            self._attr_current_temperature,
            self._attr_target_temperature,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose detailed spa-pack status and eco mode for automations."""
        self._sync_zone_from_coordinator()
        attrs: dict[str, Any] = {}
        st = self._zone.status
        if isinstance(st, TemperatureControlZoneStatus):
            attrs["detailed_status"] = st.name
        mode = self._zone.mode
        if mode is not None and hasattr(mode, "eco"):
            attrs["eco_mode"] = mode.eco
        return attrs

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        _LOGGER.debug("Updating climate entity %s", self.entity_id)
        self._update_from_zone()
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature via the Gecko zone model (MQTT shadow desired).

        ``gecko_iot_client`` wires ``TemperatureControlZone.set_target_temperature`` to
        ``MqttTransporter.publish_desired_state`` with an ``is_connected`` guard. That
        call must run on the Home Assistant event loop — the AWS IoT MQTT5 client used
        by the transporter is not safe to invoke from ``async_add_executor_job`` worker
        threads (publishes can fail silently or error depending on platform).
        """
        if (temperature := kwargs.get("temperature")) is None:
            return

        temperature = float(temperature)
        self._sync_zone_from_coordinator()

        lo = self._zone.min_temperature_set_point_c
        hi = self._zone.max_temperature_set_point_c
        if lo is None or hi is None:
            raise HomeAssistantError(
                "Temperature limits are not available yet; wait for the spa to finish "
                "loading configuration, then try again."
            )
        if not (lo <= temperature <= hi):
            raise ServiceValidationError(
                f"Temperature must be between {lo:.1f} and {hi:.1f} °C (got {temperature:.1f} °C).",
                translation_domain=DOMAIN,
                translation_key="thermostat_temperature_out_of_range",
                translation_placeholders={
                    "lo": f"{lo:.1f}",
                    "hi": f"{hi:.1f}",
                    "value": f"{temperature:.1f}",
                },
            )

        mgr = await async_get_connection_manager(self.hass)
        conn = mgr.get_connection(self.coordinator.monitor_id)
        if not conn or not conn.is_connected or not conn.gecko_client:
            raise HomeAssistantError(
                "Gecko MQTT connection is not available; check connectivity and try again."
            )

        zone_id = str(self._zone.id)
        setter = getattr(self._zone, "set_target_temperature", None)
        if callable(setter):
            try:
                setter(temperature)
            except ValueError as err:
                msg = str(err).strip() or "Could not apply temperature setpoint"
                _LOGGER.warning(
                    "set_target_temperature failed for %s zone %s: %s",
                    self.entity_id,
                    zone_id,
                    err,
                )
                if "outside configured range" in msg.lower():
                    raise ServiceValidationError(
                        msg,
                        translation_domain=DOMAIN,
                        translation_key="thermostat_temperature_out_of_range",
                        translation_placeholders={
                            "lo": f"{lo:.1f}",
                            "hi": f"{hi:.1f}",
                            "value": f"{temperature:.1f}",
                        },
                    ) from err
                raise HomeAssistantError(msg) from err
            except Exception as err:
                _LOGGER.error(
                    "Unexpected error setting temperature for %s: %s",
                    self.entity_id,
                    err,
                    exc_info=True,
                )
                raise HomeAssistantError(f"Failed to set temperature: {err}") from err
        else:
            # Extremely defensive fallback (custom / mocked zone objects).
            gecko_client = conn.gecko_client
            desired = {
                "zones": {
                    "temperatureControl": {zone_id: {"setPoint": temperature}},
                }
            }
            try:
                gecko_client.transporter.publish_desired_state(desired)
            except Exception as err:
                _LOGGER.error(
                    "Failed to publish thermostat setpoint for %s: %s",
                    self.entity_id,
                    err,
                    exc_info=True,
                )
                raise HomeAssistantError(f"Failed to set temperature: {err}") from err

        self._attr_target_temperature = temperature
        self.async_write_ha_state()

        _LOGGER.info(
            "Thermostat setpoint %.1f °C requested for zone %s (%s); "
            "waiting for spa shadow reported state to match",
            temperature,
            zone_id,
            self.entity_id,
        )
        _LOGGER.debug(
            "Applied target temperature %.1f°C for zone %s (%s)",
            temperature,
            zone_id,
            self.entity_id,
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        if hvac_mode != HVACMode.HEAT:
            raise ServiceValidationError(
                f"Unsupported HVAC mode: {hvac_mode}. Only HEAT mode is supported.",
                translation_domain=DOMAIN,
                translation_key="unsupported_hvac_mode",
                translation_placeholders={"hvac_mode": str(hvac_mode)},
            )

        # HEAT mode is the only supported mode and is always active
        _LOGGER.debug(
            "HVAC mode set to HEAT for %s (no action required)", self.entity_id
        )
