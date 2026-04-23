"""Number entities for unknown shadow zone setpoints (MQTT desired state)."""

from __future__ import annotations

import hashlib
import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .connection_manager import async_get_connection_manager
from .const import DOMAIN
from .coordinator import GeckoVesselCoordinator
from .entity import GeckoEntityAvailabilityMixin
from .shadow_metrics import (
    humanize_shadow_path,
    infer_number_setpoint_limits,
    parse_unknown_zone_setpoint_path,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Gecko number entities."""
    if not hasattr(config_entry, "runtime_data") or not config_entry.runtime_data:
        return
    coordinators = config_entry.runtime_data.coordinators
    if not coordinators:
        return

    initial: list[NumberEntity] = []

    for coordinator in coordinators:
        for path in coordinator.take_pending_number_paths():
            initial.append(
                GeckoUnknownZoneSetpointNumber(coordinator, config_entry, path)
            )

        @callback
        def _on_shadow_metric_discovery(
            coord: GeckoVesselCoordinator = coordinator,
        ) -> None:
            added = coord.take_pending_number_paths()
            if not added:
                return
            async_add_entities(
                [GeckoUnknownZoneSetpointNumber(coord, config_entry, p) for p in added]
            )

        coordinator.register_shadow_metric_callback(_on_shadow_metric_discovery)

    if initial:
        async_add_entities(initial)


class GeckoUnknownZoneSetpointNumber(
    GeckoEntityAvailabilityMixin, CoordinatorEntity, NumberEntity
):
    """Write single-leaf unknown-zone setpoints via shadow desired (same wire shape as app)."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        config_entry: ConfigEntry,
        path: str,
    ) -> None:
        super().__init__(coordinator)
        self._path = path
        parsed = parse_unknown_zone_setpoint_path(path)
        if not parsed:
            raise ValueError(f"Not a setpoint path: {path}")
        self._zone_type, self._zone_id, self._field_key = parsed
        nmin, nmax, step = infer_number_setpoint_limits(path, self._field_key)
        self._attr_native_min_value = nmin
        self._attr_native_max_value = nmax
        self._attr_native_step = step

        self._attr_name = f"Setpoint {humanize_shadow_path(path)}"
        path_hash = hashlib.sha256(path.encode("utf-8")).hexdigest()[:8]
        self._attr_unique_id = (
            f"{config_entry.entry_id}_{coordinator.monitor_id}_"
            f"num_{path.replace('.', '_')}_{path_hash}"
        )
        self._attr_entity_category = None
        self._attr_extra_state_attributes = {
            "shadow_path": path,
            "zone_type": self._zone_type,
            "zone_id": self._zone_id,
            "field_key": self._field_key,
        }
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.vessel_id))},
        )
        raw = coordinator.get_shadow_metric_value(path)
        self._attr_native_value = (
            float(raw) if raw is not None else float(self._attr_native_min_value)
        )
        self._attr_available = False

    @callback
    def _handle_coordinator_update(self) -> None:
        raw = self.coordinator.get_shadow_metric_value(self._path)
        if raw is not None:
            self._attr_native_value = float(raw)
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        mgr = await async_get_connection_manager(self.hass)
        conn = mgr.get_connection(self.coordinator.monitor_id)
        if not conn or not conn.is_connected or not conn.gecko_client:
            raise HomeAssistantError("Gecko MQTT connection is not available")

        gecko_client = conn.gecko_client
        desired = {
            "zones": {
                self._zone_type: {self._zone_id: {self._field_key: value}},
            }
        }

        # Publish on the event loop — AWS IoT MQTT client is not thread-safe for
        # arbitrary worker threads (same constraint as ``climate`` setpoint writes).
        gecko_client.transporter.publish_desired_state(desired)
        self._attr_native_value = value
        self.async_write_ha_state()
