"""Gecko IoT connection manager using Home Assistant singleton pattern."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from gecko_iot_client import GeckoIotClient
from gecko_iot_client.models.events import EventChannel
from gecko_iot_client.transporters.mqtt import MqttTransporter
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.singleton import singleton
from homeassistant.util.hass_dict import HassKey

from .const import CONFIG_TIMEOUT, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Constants
TOKEN_REFRESH_DELAY = 1  # seconds to wait before getting new token
RECONNECT_DELAY = 2  # seconds to wait before reconnecting

# Global key for the connection manager
GECKO_CONNECTION_MANAGER_KEY: HassKey[GeckoConnectionManager] = HassKey(
    f"{DOMAIN}_connection_manager"
)

# Home Assistant 2025.12+ prefers ``async_=True`` for async singletons; older cores
# detect coroutine functions automatically and reject the keyword.
_singleton_cm = (
    singleton(GECKO_CONNECTION_MANAGER_KEY, async_=True)
    if "async_" in inspect.signature(singleton).parameters
    else singleton(GECKO_CONNECTION_MANAGER_KEY)
)


@dataclass
class GeckoMonitorConnection:
    """Represents a connection to a specific monitor."""

    monitor_id: str
    gecko_client: Any  # GeckoIotClient instance
    websocket_url: str
    vessel_name: str
    update_callbacks: list[Callable[[dict], None]] = field(default_factory=list)
    is_connected: bool = False
    connectivity_status: Any = None  # ConnectivityStatus from geckoIotClient
    refresh_token_callback: Callable[[str | None], str] | None = (
        None  # Store callback for reconnection
    )


class GeckoConnectionManager:
    """Manages shared Gecko IoT connections to prevent conflicts."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the connection manager."""
        self.hass = hass
        self._connections: dict[Any, GeckoMonitorConnection] = {}
        self._connection_lock = asyncio.Lock()
        self._shutdown_callbacks: list[Callable[[], None]] = []

        # Register shutdown handler
        self._cleanup_listener = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._async_shutdown
        )

    @staticmethod
    def _canonical_monitor_id(monitor_id: Any) -> str:
        """Normalize monitor id for new connections and wire/API calls."""
        return str(monitor_id).strip()

    def _resolved_connection_key(self, monitor_id: Any) -> Any | None:
        """Return the dict key used in ``_connections`` for this monitor, if any."""
        s = self._canonical_monitor_id(monitor_id)
        if not s:
            return None
        if s in self._connections:
            return s
        try:
            n = int(s)
        except (TypeError, ValueError):
            return None
        if n in self._connections:
            return n
        return None

    def _setup_client_handlers(
        self,
        gecko_client: Any,
        connection: GeckoMonitorConnection,
        monitor_id: str,
    ) -> None:
        """Set up zone update and connectivity handlers for a gecko client.

        This helper method extracts the common handler setup logic used when
        creating or reconnecting monitor connections, following the DRY principle.
        """

        # Set up zone update handler to distribute to all callbacks
        def on_zone_update(updated_zones):
            # Copy callbacks list to avoid race conditions during iteration
            callbacks = list(connection.update_callbacks)
            for callback in callbacks:
                try:
                    callback(updated_zones)
                except Exception as e:
                    _LOGGER.error(
                        "Error in zone update callback for monitor %s: %s",
                        monitor_id,
                        e,
                    )

        # Set up connectivity update handler
        def on_connectivity_update(connectivity_status):
            # Store connectivity status in connection for easy access
            connection.connectivity_status = connectivity_status

            # Update connection status based on connectivity
            if hasattr(connectivity_status, "vessel_status"):
                # If vessel is running but transporter is not connected, we may need to refresh token
                vessel_running = str(connectivity_status.vessel_status) == "RUNNING"
                if vessel_running and not connection.is_connected:
                    _LOGGER.warning(
                        "Vessel running but connection not established for %s",
                        monitor_id,
                    )

        gecko_client.on_zone_update(on_zone_update)
        gecko_client.on(EventChannel.CONNECTIVITY_UPDATE, on_connectivity_update)

    async def async_get_or_create_connection(
        self,
        monitor_id: str | int,
        websocket_url: str,
        vessel_name: str,
        update_callback: Callable[[dict], None] | None = None,
        refresh_token_callback: Callable[[str | None], str] | None = None,
    ) -> GeckoMonitorConnection:
        """Get existing connection or create a new one for a monitor."""
        async with self._connection_lock:
            # Check if we already have a connection for this monitor
            existing_key = self._resolved_connection_key(monitor_id)
            if existing_key is not None:
                connection = self._connections[existing_key]

                # Add the callback if provided
                if (
                    update_callback
                    and update_callback not in connection.update_callbacks
                ):
                    connection.update_callbacks.append(update_callback)

                return connection

            # Create new connection (always store under canonical string key)
            mid = self._canonical_monitor_id(monitor_id)

            try:
                # Create transporter and client
                transporter = MqttTransporter(
                    broker_url=websocket_url,
                    monitor_id=mid,
                    token_refresh_callback=refresh_token_callback,
                )
                gecko_client = GeckoIotClient(
                    mid,
                    transporter,
                    config_timeout=CONFIG_TIMEOUT,
                )

                # Create connection object with refresh callback for reconnection
                connection = GeckoMonitorConnection(
                    monitor_id=mid,
                    gecko_client=gecko_client,
                    websocket_url=websocket_url,
                    vessel_name=vessel_name,
                    refresh_token_callback=refresh_token_callback,
                )

                # Add callback if provided
                if update_callback:
                    connection.update_callbacks.append(update_callback)

                # Set up handlers using the helper method
                self._setup_client_handlers(gecko_client, connection, mid)

                # Connect using executor since connect() is synchronous
                await self.hass.async_add_executor_job(gecko_client.connect)
                connection.is_connected = True

                # Store the connection
                self._connections[mid] = connection

                _LOGGER.info("Connected to monitor %s", mid)
                return connection

            except Exception as e:
                _LOGGER.error(
                    "Failed to create connection for monitor %s: %s",
                    mid,
                    e,
                    exc_info=True,
                )
                raise

    def get_connection(self, monitor_id: str | int) -> GeckoMonitorConnection | None:
        """Get existing connection for a monitor."""
        key = self._resolved_connection_key(monitor_id)
        if key is None:
            return None
        return self._connections.get(key)

    async def async_remove_callback(
        self, monitor_id: str, callback: Callable[[dict], None]
    ) -> None:
        """Remove a callback from a monitor connection.

        Uses the connection lock to prevent race conditions with callback iteration.
        """
        async with self._connection_lock:
            key = self._resolved_connection_key(monitor_id)
            if key is not None:
                connection = self._connections[key]
                if callback in connection.update_callbacks:
                    connection.update_callbacks.remove(callback)

                    # If no more callbacks, we could optionally disconnect
                    # For now, keep connections alive as they may be reused

    async def async_disconnect_monitor(self, monitor_id: str) -> None:
        """Disconnect and remove a monitor connection."""
        async with self._connection_lock:
            key = self._resolved_connection_key(monitor_id)
            if key is not None:
                connection = self._connections[key]

                try:
                    if connection.is_connected and connection.gecko_client:
                        await self.hass.async_add_executor_job(
                            connection.gecko_client.disconnect
                        )
                        connection.is_connected = False
                except Exception as e:
                    _LOGGER.error("Error disconnecting monitor %s: %s", monitor_id, e)

                # Remove from connections
                del self._connections[key]

    async def async_reconnect_monitor(self, monitor_id: str) -> bool:
        """Reconnect a specific monitor connection.

        This method disconnects the existing connection (if any) and establishes
        a new connection using a fresh token from the refresh callback.

        Returns True if reconnection was successful, False otherwise.
        """
        mid = self._canonical_monitor_id(monitor_id)
        key = self._resolved_connection_key(monitor_id)
        if key is None:
            _LOGGER.warning(
                "No existing connection found for monitor %s to reconnect", mid
            )
            return False

        connection = self._connections[key]

        try:
            refresh_callback = connection.refresh_token_callback
            if not refresh_callback and hasattr(
                connection.gecko_client, "transporter"
            ) and hasattr(
                connection.gecko_client.transporter, "_token_refresh_callback"
            ):
                refresh_callback = (
                    connection.gecko_client.transporter._token_refresh_callback
                )

            if not refresh_callback or not callable(refresh_callback):
                _LOGGER.error(
                    "No token refresh callback available for monitor %s - cannot reconnect",
                    mid,
                )
                return False

            # Get fresh websocket URL with new token
            _LOGGER.debug("Getting fresh token for monitor %s", mid)
            new_url = await self.hass.async_add_executor_job(refresh_callback, mid)

            if not new_url or not isinstance(new_url, str):
                _LOGGER.error(
                    "Failed to get new websocket URL for monitor %s", monitor_id
                )
                return False

            # Disconnect existing connection
            async with self._connection_lock:
                if connection.is_connected and connection.gecko_client:
                    try:
                        await self.hass.async_add_executor_job(
                            connection.gecko_client.disconnect
                        )
                    except Exception as e:
                        _LOGGER.warning(
                            "Error disconnecting monitor %s during reconnect: %s",
                            monitor_id,
                            e,
                        )
                    connection.is_connected = False

                # Brief delay before reconnecting
                await asyncio.sleep(RECONNECT_DELAY)

                # Create new transporter and client with fresh URL
                transporter = MqttTransporter(
                    broker_url=new_url,
                    monitor_id=mid,
                    token_refresh_callback=refresh_callback,
                )

                gecko_client = GeckoIotClient(
                    mid, transporter, config_timeout=CONFIG_TIMEOUT
                )

                # Set up handlers using the helper method (DRY principle)
                self._setup_client_handlers(gecko_client, connection, mid)

                # Update connection object with new client and URL
                connection.gecko_client = gecko_client
                connection.websocket_url = new_url

                # Connect with fresh token
                await self.hass.async_add_executor_job(gecko_client.connect)
                connection.is_connected = True

                _LOGGER.info("Successfully reconnected monitor %s", monitor_id)
                return True

        except Exception as e:
            _LOGGER.error(
                "Failed to reconnect monitor %s: %s", monitor_id, e, exc_info=True
            )
            connection.is_connected = False
            return False

    async def _async_shutdown(self, _event: Event) -> None:
        """Shutdown all connections."""
        # Disconnect all monitors
        monitor_ids = list(self._connections.keys())
        for monitor_id in monitor_ids:
            await self.async_disconnect_monitor(monitor_id)

        # Call any shutdown callbacks
        for callback in self._shutdown_callbacks:
            try:
                callback()
            except Exception as e:
                _LOGGER.error("Error in shutdown callback: %s", e)

    async def async_refresh_connection_token(self, monitor_id: str) -> bool:
        """Refresh the token for a specific connection.

        This method acquires the connection lock at entry to prevent race conditions
        where the connection could be modified or removed by concurrent operations.
        """
        # Acquire lock at method entry to prevent race conditions
        async with self._connection_lock:
            mid = self._canonical_monitor_id(monitor_id)
            key = self._resolved_connection_key(monitor_id)
            if key is None:
                _LOGGER.error("No connection found for monitor %s to refresh", mid)
                return False

            connection = self._connections[key]

            try:
                # Disconnect current connection
                if connection.gecko_client and connection.is_connected:
                    await self.hass.async_add_executor_job(
                        connection.gecko_client.disconnect
                    )
                    connection.is_connected = False

                # Wait briefly before getting new token
                await asyncio.sleep(TOKEN_REFRESH_DELAY)

                # Use the stored refresh callback from connection object (avoids accessing private attributes)
                refresh_callback = connection.refresh_token_callback

                if not refresh_callback or not callable(refresh_callback):
                    _LOGGER.warning(
                        "No token refresh callback available for monitor %s", mid
                    )
                    return False

                # Run the callback in executor since it might be blocking
                new_url = await self.hass.async_add_executor_job(refresh_callback, mid)

                if not new_url or not isinstance(new_url, str):
                    _LOGGER.error("Failed to get new websocket URL for monitor %s", mid)
                    return False

                if new_url != connection.websocket_url:
                    connection.websocket_url = new_url

                # Re-instantiate transporter and gecko client with new token
                transporter = MqttTransporter(
                    broker_url=new_url,
                    monitor_id=mid,
                    token_refresh_callback=refresh_callback,
                )

                # Create new gecko client
                gecko_client = GeckoIotClient(
                    mid, transporter, config_timeout=CONFIG_TIMEOUT
                )

                # Set up handlers using the helper method (DRY principle)
                self._setup_client_handlers(gecko_client, connection, mid)

                # Update connection with new client
                connection.gecko_client = gecko_client

                # Wait briefly before reconnecting
                await asyncio.sleep(RECONNECT_DELAY)

                # Reconnect with fresh token
                await self.hass.async_add_executor_job(connection.gecko_client.connect)
                connection.is_connected = True

                _LOGGER.info("Refreshed and reconnected monitor %s", mid)
                return True

            except Exception as e:
                _LOGGER.error(
                    "Failed to refresh token for monitor %s: %s", mid, e, exc_info=True
                )
                # State is already set to False within the lock if disconnect succeeded
                # No need to set it again outside the lock (fixes race condition)
                return False

    def add_shutdown_callback(self, callback: Callable[[], None]) -> None:
        """Add a callback to be called during shutdown."""
        self._shutdown_callbacks.append(callback)

    def get_connection_status(self, monitor_id: str) -> dict[str, Any]:
        """Get connection status for a monitor."""
        key = self._resolved_connection_key(monitor_id)
        if key is None:
            return {
                "exists": False,
                "connected": False,
                "status": "No connection found",
            }

        connection = self._connections[key]

        try:
            # Check if gecko client has connectivity status
            status_info = {
                "exists": True,
                "connected": connection.is_connected,
                "vessel_name": connection.vessel_name,
                "websocket_url": connection.websocket_url,
            }

            if (
                hasattr(connection.gecko_client, "connectivity_status")
                and connection.gecko_client.connectivity_status
            ):
                status_info["connectivity_status"] = str(
                    connection.gecko_client.connectivity_status
                )

            return status_info

        except Exception as e:
            return {"exists": True, "connected": False, "error": str(e)}


@_singleton_cm
async def async_get_connection_manager(hass: HomeAssistant) -> GeckoConnectionManager:
    """Get or create the singleton Gecko connection manager."""
    return GeckoConnectionManager(hass)
