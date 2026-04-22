"""Data update coordinator for Gecko."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from datetime import timedelta
from typing import Any, Dict, List, Set

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

# Import from geckoIotClient
from gecko_iot_client.models.zone_types import ZoneType, AbstractZone

from .cloud_tiles import (
    extract_cloud_tile_booleans,
    extract_cloud_tile_metrics,
    extract_cloud_tile_strings,
    find_vessel_record,
)
from .const import (
    CONF_ALERTS_POLL_INTERVAL,
    CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
    CONF_CLOUD_REST_POLL_INTERVAL,
    DEFAULT_ALERTS_POLL_INTERVAL,
    DEFAULT_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
    DEFAULT_CLOUD_REST_POLL_INTERVAL,
    DOMAIN,
)
from .rest_alerts import build_alerts_snapshot
from .shadow_metrics import (
    extract_extension_booleans,
    extract_extension_metrics,
    extract_extension_strings,
    path_reserved_for_number_control,
)
from .connection_manager import (
    async_get_connection_manager,
    GeckoMonitorConnection,
)

_LOGGER = logging.getLogger(__name__)

# Constants
UPDATE_INTERVAL_SECONDS = 30  # seconds between coordinator updates
MAX_CONSECUTIVE_FAILURES = 2  # max failures before attempting reconnect
RECONNECT_DELAY = 1  # seconds to wait before reconnecting
INITIAL_ZONE_TIMEOUT = 60.0  # seconds to wait for initial zone data


class GeckoVesselCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for a single Gecko vessel/spa following Home Assistant best practices."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        account_id: str,
        vessel_id: str,
        monitor_id: str,
        vessel_name: str,
    ) -> None:
        """Initialize the vessel coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{vessel_id}",
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self.entry_id = entry_id
        self.account_id = account_id
        self.vessel_id = vessel_id
        self.monitor_id = monitor_id
        self.vessel_name = vessel_name
        
        # Store zones for this vessel only (no monitor_id dictionary needed)
        self._zones: Dict[ZoneType, List[AbstractZone]] = {}
        
        # Store real-time state data for this vessel
        self._spa_state: Dict[str, Any] = {}
        
        # Track if this vessel has received initial zone data
        self._has_initial_zones = False
        
        # Event to signal when initial zone data is loaded
        self._initial_zones_loaded_event = asyncio.Event()
        
        # Callbacks for zone updates (for dynamic entity creation)
        self._zone_update_callbacks: list = []
        
        # Simple connection tracking
        self._consecutive_failures = 0

        # Extension metrics from device shadow (Waterlab, unknown zone types, extra features)
        self._shadow_metric_values: Dict[str, float | int] = {}
        self._registered_shadow_metric_paths: Set[str] = set()
        self._pending_new_metric_paths: Set[str] = set()

        self._shadow_bool_values: Dict[str, bool] = {}
        self._registered_bool_paths: Set[str] = set()
        self._pending_bool_paths: Set[str] = set()

        self._shadow_string_values: Dict[str, str] = {}
        self._registered_string_paths: Set[str] = set()
        self._pending_string_paths: Set[str] = set()

        self._registered_number_paths: Set[str] = set()
        self._pending_number_paths: Set[str] = set()

        # Optional REST tile metrics (merged under ``cloud.rest.*``; shadow wins on overlap)
        self._cloud_tile_metrics: Dict[str, float | int] = {}
        self._cloud_string_metrics: Dict[str, str] = {}
        self._cloud_bool_metrics: Dict[str, bool] = {}
        self._last_cloud_poll_monotonic: float | None = None

        # REST: unread messages (scoped) + vessel actions — not history.
        self._rest_alerts_snapshot: Dict[str, Any] = {
            "total": 0,
            "messages": [],
            "actions": [],
            "updated_at": None,
            "error": None,
        }
        self._last_alerts_poll_monotonic: float | None = None

    def register_zone_update_callback(self, callback):
        """Register a callback to be called when zone data updates."""
        self._zone_update_callbacks.append(callback)

    async def _async_handle_zone_update(self, data: dict[str, Any]) -> None:
        """Handle zone update in the event loop."""
        gecko_client = await self.get_gecko_client()
        self.sync_refresh_shadow_metrics(gecko_client)

        # Trigger entity discovery when zones are updated
        self.async_set_updated_data(data)
        
        _LOGGER.debug("Zone data updated for vessel %s", self.vessel_name)

        # Call registered callbacks for dynamic entity creation
        for callback in self._zone_update_callbacks:
            try:
                if callable(callback) and callback is not None:
                    result = callback()
                    # If callback returns a coroutine, await it
                    if inspect.iscoroutine(result):
                        await result
            except Exception as ex:
                _LOGGER.error("Error in zone update callback for vessel %s: %s", self.vessel_name, ex, exc_info=True)

    def _entry_options(self) -> dict[str, Any]:
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if not entry:
            return {}
        return dict(entry.options)

    async def _async_poll_cloud_tiles_if_due(
        self,
        connection: GeckoMonitorConnection | None,
    ) -> None:
        """Optionally refresh app-style tile metrics from Gecko REST (account/vessel IDs from config)."""
        opts = self._entry_options()
        interval = int(
            opts.get(
                CONF_CLOUD_REST_POLL_INTERVAL, DEFAULT_CLOUD_REST_POLL_INTERVAL
            )
        )
        if interval <= 0 or not self.account_id:
            return

        only_when_mqtt_down = bool(
            opts.get(
                CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
                DEFAULT_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
            )
        )
        mqtt_up = bool(connection and connection.is_connected)
        if only_when_mqtt_down and mqtt_up:
            return

        now = time.monotonic()
        if (
            self._last_cloud_poll_monotonic is not None
            and (now - self._last_cloud_poll_monotonic) < interval
        ):
            return

        self._last_cloud_poll_monotonic = now
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if not entry or not getattr(entry, "runtime_data", None):
            return
        api = entry.runtime_data.api_client
        rd = entry.runtime_data
        vessels: List[Any] | None = None
        try:
            if (
                rd.rest_vessels_response_cache is not None
                and rd.rest_vessels_response_cache_mono is not None
                and (now - rd.rest_vessels_response_cache_mono) < interval
            ):
                vessels = rd.rest_vessels_response_cache
            else:
                vessels = await api.async_get_vessels(str(self.account_id))
                rd.rest_vessels_response_cache = vessels
                rd.rest_vessels_response_cache_mono = now
        except Exception as err:
            _LOGGER.debug(
                "Cloud tile REST poll skipped for %s: %s", self.vessel_name, err
            )
            return

        vessel_rec = find_vessel_record(vessels, self.vessel_id)
        if not vessel_rec:
            _LOGGER.debug(
                "Cloud tile REST: no vessel row for vessel_id=%s", self.vessel_id
            )
            return

        self._cloud_tile_metrics = extract_cloud_tile_metrics(vessel_rec)
        self._cloud_string_metrics = extract_cloud_tile_strings(vessel_rec)
        self._cloud_bool_metrics = extract_cloud_tile_booleans(vessel_rec)

    async def _async_poll_alerts_if_due(self) -> None:
        """Poll Gecko REST for new/active alerts (messages + vessel actions)."""
        opts = self._entry_options()
        interval = int(
            opts.get(CONF_ALERTS_POLL_INTERVAL, DEFAULT_ALERTS_POLL_INTERVAL)
        )
        if interval <= 0 or not self.account_id:
            return

        now = time.monotonic()
        if (
            self._last_alerts_poll_monotonic is not None
            and (now - self._last_alerts_poll_monotonic) < interval
        ):
            return

        self._last_alerts_poll_monotonic = now
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if not entry or not getattr(entry, "runtime_data", None):
            return
        api = entry.runtime_data.api_client
        messages_payload: Any | None = None
        actions_payload: Any | None = None
        err: str | None = None
        try:
            messages_payload = await api.async_get_messages_unread(
                str(self.account_id)
            )
        except Exception as exc:
            err = f"messages_unread:{type(exc).__name__}"
            _LOGGER.debug(
                "Alerts poll: messages/unread failed for %s: %s",
                self.vessel_name,
                exc,
            )
        try:
            actions_payload = await api.async_get_vessel_actions_v2(
                str(self.account_id), str(self.vessel_id)
            )
        except Exception as exc:
            aerr = f"actions:{type(exc).__name__}"
            err = f"{err};{aerr}" if err else aerr
            _LOGGER.debug(
                "Alerts poll: vessel actions failed for %s: %s",
                self.vessel_name,
                exc,
            )

        snap = build_alerts_snapshot(
            messages_payload=messages_payload,
            actions_payload=actions_payload,
            vessel_id=str(self.vessel_id),
            monitor_id=str(self.monitor_id),
        )
        if err:
            snap = {**snap, "error": err}
        self._rest_alerts_snapshot = snap

    def get_rest_alerts_snapshot(self) -> dict[str, Any]:
        """Latest merged REST alerts (counts + short previews)."""
        return dict(self._rest_alerts_snapshot)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Gecko API."""
        try:
            # Check if connection exists and is active
            connection_manager = await async_get_connection_manager(self.hass)
            connection = connection_manager._connections.get(self.monitor_id)
            
            if not connection or not connection.is_connected:
                self._consecutive_failures += 1
                
                # After 2 consecutive failures (1 minute), try to reconnect with fresh token
                if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    _LOGGER.warning("Connection lost for %s, attempting reconnect", self.vessel_name)
                    await self._simple_reconnect()
                    self._consecutive_failures = 0
            else:
                self._consecutive_failures = 0

            await self._async_poll_cloud_tiles_if_due(connection)
            await self._async_poll_alerts_if_due()

            client = await self.get_gecko_client()
            self.sync_refresh_shadow_metrics(client)

            # Data will be updated by geckoIotClient callbacks
            return {"status": "active", "vessel_id": self.vessel_id}
        except Exception as exception:
            raise UpdateFailed(f"Error communicating with Gecko API for vessel {self.vessel_name}: {exception}") from exception

    def get_zones_by_type(self, zone_type: ZoneType) -> List[AbstractZone]:
        """Get zones of a specific type for this vessel (no monitor_id needed)."""
        return self._zones.get(zone_type, [])

    def get_all_zones(self) -> Dict[ZoneType, List[AbstractZone]]:
        """Get all zones for this vessel."""
        return self._zones

    def sync_refresh_shadow_metrics(self, gecko_client: Any | None) -> None:
        """Parse extension metrics from shadow; merge optional REST tile metrics (MQTT wins)."""
        state = getattr(gecko_client, "_state", None) if gecko_client else None
        mqtt_metrics = extract_extension_metrics(state) if state else {}
        merged: Dict[str, float | int] = dict(self._cloud_tile_metrics)
        merged.update(mqtt_metrics)
        self._shadow_metric_values = merged

        reserved_numbers = {
            p for p in self._shadow_metric_values if path_reserved_for_number_control(p)
        }
        self._pending_new_metric_paths |= (
            set(self._shadow_metric_values)
            - reserved_numbers
            - self._registered_shadow_metric_paths
        )
        self._pending_number_paths |= reserved_numbers - self._registered_number_paths

        mqtt_bools = extract_extension_booleans(state) if state else {}
        merged_bools: Dict[str, bool] = dict(self._cloud_bool_metrics)
        merged_bools.update(mqtt_bools)
        self._shadow_bool_values = merged_bools
        self._pending_bool_paths |= (
            set(self._shadow_bool_values) - self._registered_bool_paths
        )

        mqtt_strings = extract_extension_strings(state) if state else {}
        merged_strings: Dict[str, str] = dict(self._cloud_string_metrics)
        merged_strings.update(mqtt_strings)
        self._shadow_string_values = merged_strings
        self._pending_string_paths |= (
            set(self._shadow_string_values) - self._registered_string_paths
        )

    def get_shadow_metric_value(self, metric_path: str) -> float | int | None:
        """Return a single numeric leaf from the last shadow refresh."""
        val = self._shadow_metric_values.get(metric_path)
        return val if val is not None else None

    def take_pending_new_metric_paths(self) -> list[str]:
        """Paths not yet bound to sensor entities; marks them registered."""
        out = sorted(self._pending_new_metric_paths)
        self._registered_shadow_metric_paths.update(out)
        self._pending_new_metric_paths.clear()
        return out

    def take_pending_number_paths(self) -> list[str]:
        """Unknown-zone setpoint paths for Number entities."""
        out = sorted(self._pending_number_paths)
        self._registered_number_paths.update(out)
        self._pending_number_paths.clear()
        return out

    def take_pending_bool_paths(self) -> list[str]:
        out = sorted(self._pending_bool_paths)
        self._registered_bool_paths.update(out)
        self._pending_bool_paths.clear()
        return out

    def take_pending_string_paths(self) -> list[str]:
        out = sorted(self._pending_string_paths)
        self._registered_string_paths.update(out)
        self._pending_string_paths.clear()
        return out

    def get_shadow_bool_value(self, path: str) -> bool | None:
        if path not in self._shadow_bool_values:
            return None
        return self._shadow_bool_values[path]

    def get_shadow_string_value(self, path: str) -> str | None:
        if path not in self._shadow_string_values:
            return None
        return self._shadow_string_values[path]

    async def _simple_reconnect(self) -> None:
        """Simple reconnection - let geckoIotClient handle token refresh."""
        try:
            connection_manager = await async_get_connection_manager(self.hass)
            
            # Reconnect - the geckoIotClient will automatically call the token
            # refresh callback to get a fresh URL with new tokens
            success = await connection_manager.async_reconnect_monitor(self.monitor_id)
            
            if success:
                _LOGGER.info("Reconnected %s", self.vessel_name)
            else:
                _LOGGER.error("Failed to reconnect %s", self.vessel_name)
                
        except Exception as e:
            _LOGGER.error("Failed to reconnect %s: %s", self.vessel_name, e)

    async def get_gecko_client(self):
        """Get the gecko client for this vessel's monitor."""
        try:
            connection_manager = await async_get_connection_manager(self.hass)
            connection = connection_manager._connections.get(self.monitor_id)
            
            if connection and connection.is_connected:
                return connection.gecko_client
            else:
                _LOGGER.warning("No active connection found for vessel %s (monitor %s)", self.vessel_name, self.monitor_id)
                return None
                
        except Exception as e:
            _LOGGER.error("Failed to get gecko client for vessel %s: %s", self.vessel_name, e)
            return None

    def _create_refresh_token_callback(self, websocket_url: str):
        """Create a refresh token callback for this vessel's monitor.
        
        This callback is invoked by the geckoIotClient when websocket tokens expire
        or are about to expire. It fetches a fresh websocket URL with new JWT tokens
        from the Gecko API using the OAuth2-managed access token.
        """
        def refresh_token_callback(monitor_id: str | None = None) -> str:
            """Handle token refresh by getting a new websocket URL.
            
            This is a synchronous callback invoked from background threads by the
            geckoIotClient library. We use run_coroutine_threadsafe to safely
            execute the async API call on Home Assistant's event loop.
            
            Args:
                monitor_id: The monitor ID that needs token refresh (optional, uses self.monitor_id if not provided)
                
            Returns:
                New websocket URL with fresh JWT token, or original URL on failure
            """
            # Use provided monitor_id or fall back to coordinator's monitor_id
            target_monitor_id = monitor_id or self.monitor_id
            
            try:
                # Get the config entry
                entry = self.hass.config_entries.async_get_entry(self.entry_id)
                if not entry:
                    _LOGGER.error("Config entry %s not found for vessel %s - cannot refresh token", self.entry_id, self.vessel_name)
                    return websocket_url
                
                # Get API client from runtime data
                if not hasattr(entry, 'runtime_data') or not entry.runtime_data:
                    _LOGGER.error("No runtime_data found for vessel %s - cannot refresh token", self.vessel_name)
                    return websocket_url
                
                api_client = entry.runtime_data.api_client
                if not api_client:
                    _LOGGER.error("No API client found for vessel %s - cannot refresh token", self.vessel_name)
                    return websocket_url
                
                # Fetch new livestream URL with fresh JWT token
                # This is a sync callback from background thread, so use run_coroutine_threadsafe
                future = asyncio.run_coroutine_threadsafe(
                    api_client.async_get_monitor_livestream(target_monitor_id),
                    self.hass.loop
                )
                
                # Wait for the API call to complete (with timeout)
                livestream_data = future.result(timeout=30.0)
                
                # Extract the new websocket URL
                new_url = livestream_data.get("brokerUrl")
                if new_url:
                    return new_url
                else:
                    _LOGGER.error("No brokerUrl in livestream response for vessel %s", self.vessel_name)
                    return websocket_url
                    
            except TimeoutError:
                _LOGGER.error("Timeout fetching new websocket URL for vessel %s - API call took too long", self.vessel_name)
                return websocket_url
            except Exception as e:
                _LOGGER.error("Failed to refresh token for vessel %s: %s", self.vessel_name, e, exc_info=True)
                return websocket_url
        
        return refresh_token_callback

    async def async_setup_monitor_connection(self, websocket_url: str) -> bool:
        """Set up a connection to this vessel's monitor using the singleton connection manager."""
        try:
            # Get the singleton connection manager
            connection_manager = await async_get_connection_manager(self.hass)
            
            # Create update callback for this vessel's coordinator
            def on_zone_update(updated_zones):
                # Store the updated zones from GeckoIotClient (these have state managers!)
                self._zones = updated_zones
                
                # Mark this vessel as having received zones
                if not self._has_initial_zones:
                    self._has_initial_zones = True
                    if not self._initial_zones_loaded_event.is_set():
                        self._initial_zones_loaded_event.set()
                
                # Schedule the async call to run on the event loop from background thread
                asyncio.run_coroutine_threadsafe(
                    self._async_handle_zone_update({"last_update": "zone_update"}),
                    self.hass.loop
                )
            
            # Create refresh token callback
            refresh_token_callback = self._create_refresh_token_callback(websocket_url)
                
            # Get or create connection with refresh token callback
            await connection_manager.async_get_or_create_connection(
                monitor_id=self.monitor_id,
                websocket_url=websocket_url,
                vessel_name=self.vessel_name,
                update_callback=on_zone_update,
                refresh_token_callback=refresh_token_callback,
            )
            
            return True
            
        except Exception as e:
            _LOGGER.error("Failed to set up connection for vessel %s: %s", self.vessel_name, e, exc_info=True)
            return False

    async def async_get_operation_mode_status(self):
        """Get operation mode status for this vessel's monitor."""
        gecko_client = await self.get_gecko_client()
        if gecko_client:
            return gecko_client.operation_mode_status
        return None

    def update_spa_state(self, state_data: Dict[str, Any]) -> None:
        """Update spa state data and trigger coordinator update."""
        self._spa_state = state_data
        
        # Schedule the async call to run on the event loop from background thread
        asyncio.run_coroutine_threadsafe(
            self._async_handle_zone_update({"last_update": state_data}),
            self.hass.loop
        )

    async def async_wait_for_initial_zone_data(self, timeout: float = INITIAL_ZONE_TIMEOUT) -> bool:
        """Wait for this vessel to receive its initial zone data."""
        try:
            await asyncio.wait_for(self._initial_zones_loaded_event.wait(), timeout=timeout)
            _LOGGER.debug("Initial zone data loaded for vessel %s within timeout", self.vessel_name)
            return True
        except asyncio.TimeoutError:
            _LOGGER.warning("Timeout waiting for initial zone data for vessel %s", self.vessel_name)
            return False

    def get_spa_state(self) -> Dict[str, Any] | None:
        """Get spa state data for this vessel."""
        return self._spa_state

    async def async_shutdown(self) -> None:
        """Shutdown coordinator and cleanup resources."""
        _LOGGER.debug("Shutting down coordinator for vessel %s (entry %s)", self.vessel_name, self.entry_id)
        
        try:
            # Connection manager will handle cleanup during Home Assistant shutdown
            # We don't disconnect here as the connection may be shared
            _LOGGER.debug("Coordinator releasing vessel %s (monitor %s)", self.vessel_name, self.monitor_id)
            
        except Exception as ex:
            _LOGGER.warning("Error during coordinator shutdown for vessel %s: %s", self.vessel_name, ex)
        
        self._zones.clear()
        self._spa_state.clear()
        self._zone_update_callbacks.clear()
        self._shadow_metric_values.clear()
        self._registered_shadow_metric_paths.clear()
        self._pending_new_metric_paths.clear()
        self._shadow_bool_values.clear()
        self._registered_bool_paths.clear()
        self._pending_bool_paths.clear()
        self._shadow_string_values.clear()
        self._registered_string_paths.clear()
        self._pending_string_paths.clear()
        self._registered_number_paths.clear()
        self._pending_number_paths.clear()
        self._cloud_tile_metrics.clear()
        self._cloud_string_metrics.clear()
        self._last_cloud_poll_monotonic = None
        self._rest_alerts_snapshot = {
            "total": 0,
            "messages": [],
            "actions": [],
            "updated_at": None,
            "error": None,
        }
        self._last_alerts_poll_monotonic = None