"""Select entities for Gecko spa integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gecko_iot_client.models.events import EventChannel
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GeckoVesselCoordinator
from .entity import GeckoEntityAvailabilityMixin

_LOGGER = logging.getLogger(__name__)

# Internal HA option keys (snake_case, match entity.select.watercare_mode.state in strings.json).
# These are the values stored in HA state and passed to async_select_option.
WATERCARE_MODE_OPTIONS = [
    "away",
    "standard",
    "savings",
    "super_savings",
    "weekender",
    "other",
]

# Bidirectional mapping between HA option keys and library mode names.
_MODE_KEY_TO_LIBRARY: dict[str, str] = {
    "away": "Away",
    "standard": "Standard",
    "savings": "Savings",
    "super_savings": "Super Savings",
    "weekender": "Weekender",
    "other": "Other",
}
_LIBRARY_TO_MODE_KEY: dict[str, str] = {v: k for k, v in _MODE_KEY_TO_LIBRARY.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Gecko select entities."""
    _LOGGER.debug("Setting up Gecko select entities")

    # Get runtime data with per-vessel coordinators
    runtime_data = entry.runtime_data
    if not runtime_data or not runtime_data.coordinators:
        _LOGGER.error(
            "No coordinators found in runtime_data for config entry %s", entry.entry_id
        )
        return

    entities = []

    # Create a watercare mode select for each vessel coordinator
    for coordinator in runtime_data.coordinators:
        _LOGGER.debug(
            "Creating watercare select for vessel %s (ID: %s)",
            coordinator.vessel_name,
            coordinator.vessel_id,
        )

        # Add watercare mode select for each spa/vessel
        entities.append(
            GeckoWatercareSelectEntity(
                coordinator=coordinator,
                vessel_name=coordinator.vessel_name,
                vessel_id=coordinator.vessel_id,
            )
        )

    if entities:
        _LOGGER.debug("Adding %d Gecko select entities", len(entities))
        async_add_entities(entities)


class GeckoWatercareSelectEntity(
    GeckoEntityAvailabilityMixin, CoordinatorEntity, SelectEntity
):
    """Representation of a Gecko watercare mode select."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        vessel_name: str,
        vessel_id: str,
    ) -> None:
        """Initialize the select."""
        SelectEntity.__init__(self)
        CoordinatorEntity.__init__(self, coordinator)

        self._vessel_name = vessel_name
        self._vessel_id = vessel_id

        # Set up entity attributes
        self._attr_translation_key = "watercare_mode"
        self._attr_unique_id = f"{vessel_id}_watercare_mode"
        self._attr_icon = "mdi:hot-tub"
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_options = WATERCARE_MODE_OPTIONS

        # Device info for grouping entities
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, str(vessel_id))},
        )

        # Initialize state
        self._attr_current_option = None

        # Initialize availability (will be updated by mixin when added to hass)
        self._attr_available = False
        self._operation_mode_callback_registered = False
        self._op_mode_bound_client: Any | None = None
        self._update_lock = asyncio.Lock()

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        # Call parent classes - this ensures the mixin's connectivity registration happens
        await super().async_added_to_hass()
        await self._manage_operation_mode_callback(register=True)
        await self._async_update_state()

    async def async_will_remove_from_hass(self) -> None:
        """When entity is removed from hass."""
        await self._manage_operation_mode_callback(register=False)
        await super().async_will_remove_from_hass()

    async def _manage_operation_mode_callback(self, register: bool) -> None:
        """Register or unregister operation mode push updates from gecko_iot_client."""
        if not register:
            if self._op_mode_bound_client is not None:
                self._op_mode_bound_client.off(
                    EventChannel.OPERATION_MODE_UPDATE, self._on_operation_mode_update
                )
            self._op_mode_bound_client = None
            self._operation_mode_callback_registered = False
            return

        gecko_client = await self.coordinator.get_gecko_client()
        if not gecko_client:
            if self._op_mode_bound_client is not None:
                self._op_mode_bound_client.off(
                    EventChannel.OPERATION_MODE_UPDATE, self._on_operation_mode_update
                )
            self._op_mode_bound_client = None
            self._operation_mode_callback_registered = False
            return

        if (
            self._operation_mode_callback_registered
            and self._op_mode_bound_client is gecko_client
        ):
            return

        if self._op_mode_bound_client is not None:
            self._op_mode_bound_client.off(
                EventChannel.OPERATION_MODE_UPDATE, self._on_operation_mode_update
            )

        gecko_client.on(
            EventChannel.OPERATION_MODE_UPDATE, self._on_operation_mode_update
        )
        self._op_mode_bound_client = gecko_client
        self._operation_mode_callback_registered = True

    def _on_operation_mode_update(self, operation_mode_controller) -> None:
        """Handle operation mode updates (may run on library background thread)."""
        try:
            new_option = _LIBRARY_TO_MODE_KEY.get(
                operation_mode_controller.mode_name,
                operation_mode_controller.mode_name.lower().replace(" ", "_"),
            )

            def _update_and_write() -> None:
                self._attr_current_option = new_option
                self.async_write_ha_state()

            self.hass.loop.call_soon_threadsafe(_update_and_write)
        except Exception as e:
            _LOGGER.debug(
                "Error handling operation mode update for %s: %s", self._vessel_name, e
            )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        try:
            # Schedule async state update with lock to prevent concurrent updates
            self.hass.async_create_task(self._async_update_state_locked())
        except Exception as e:
            _LOGGER.debug(
                "Error scheduling state update for %s: %s", self._vessel_name, e
            )

    async def _async_update_state_locked(self) -> None:
        """Update the select state asynchronously with lock to serialize concurrent updates."""
        async with self._update_lock:
            await self._async_update_state()

    async def _async_update_state(self) -> None:
        """Update the select state asynchronously."""
        try:
            # Get the gecko client for this vessel's monitor
            gecko_client = await self.coordinator.get_gecko_client()

            if gecko_client and gecko_client.operation_mode_controller:
                library_name = gecko_client.operation_mode_controller.mode_name
                self._attr_current_option = _LIBRARY_TO_MODE_KEY.get(
                    library_name,
                    library_name.lower().replace(" ", "_"),
                )
                _LOGGER.debug(
                    "Updated watercare mode for %s: %s",
                    self._vessel_name,
                    self._attr_current_option,
                )
            else:
                _LOGGER.debug(
                    "Gecko client or operation mode controller not available for %s",
                    self._vessel_name,
                )
                self._attr_current_option = None

        except Exception as e:
            _LOGGER.debug("Could not get operation mode for %s: %s", self._vessel_name, e)
            self._attr_current_option = None

        await self._manage_operation_mode_callback(register=True)

        # Availability is now updated via CONNECTIVITY_UPDATE events, not polling

        # Write the updated state
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        if option not in WATERCARE_MODE_OPTIONS:
            _LOGGER.error("Invalid watercare mode option: %s", option)
            return

        library_name = _MODE_KEY_TO_LIBRARY.get(option, option)
        _LOGGER.debug(
            "Setting watercare mode for vessel %s to %s (library: %s)",
            self._vessel_name,
            option,
            library_name,
        )

        try:
            gecko_client = await self.coordinator.get_gecko_client()

            if not gecko_client:
                _LOGGER.error(
                    "No gecko client available for vessel %s", self._vessel_name
                )
                return

            if not gecko_client.operation_mode_controller:
                _LOGGER.error(
                    "Operation mode controller not available for vessel %s",
                    self._vessel_name,
                )
                return

            gecko_client.operation_mode_controller.set_mode_by_name(library_name)

            _LOGGER.debug(
                "Sent watercare mode command (%s) for vessel %s",
                library_name,
                self._vessel_name,
            )

            await self.coordinator.async_request_refresh()

        except Exception as e:
            _LOGGER.error(
                "Error setting watercare mode for vessel %s: %s", self._vessel_name, e
            )
