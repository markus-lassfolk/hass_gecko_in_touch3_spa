"""Data update coordinator for Gecko."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from datetime import timedelta
from typing import Any

from aiohttp import ClientError

# Import from geckoIotClient
from gecko_iot_client.models.zone_types import (
    AbstractZone,
    ZoneType,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .cloud_tiles import (
    extract_cloud_tile_booleans,
    extract_cloud_tile_metrics,
    extract_cloud_tile_strings,
    extract_vessel_action_metrics,
    extract_vessel_action_strings,
    extract_vessel_disc_strings,
    extract_vessel_readings_metrics,
    extract_vessel_readings_strings,
    find_vessel_record,
)
from .connection_manager import (
    GeckoMonitorConnection,
    async_get_connection_manager,
)
from .const import (
    CONF_ALERTS_POLL_INTERVAL,
    CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
    CONF_CLOUD_REST_POLL_INTERVAL,
    CONF_ENERGY_POLL_INTERVAL,
    DEFAULT_ALERTS_POLL_INTERVAL,
    DEFAULT_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
    DEFAULT_CLOUD_REST_POLL_INTERVAL,
    DEFAULT_ENERGY_POLL_INTERVAL,
    DOMAIN,
    clamp_sensor_native_str,
)
from .rest_alerts import build_alerts_snapshot
from .shadow_metrics import (
    extract_extension_booleans,
    extract_extension_metrics,
    extract_extension_strings,
    path_reserved_for_number_control,
)

_LOGGER = logging.getLogger(__name__)

# Constants
UPDATE_INTERVAL_SECONDS = 30  # seconds between coordinator updates
MAX_CONSECUTIVE_FAILURES = 2  # max failures before attempting reconnect
RECONNECT_DELAY = 1  # seconds to wait before reconnecting
INITIAL_ZONE_TIMEOUT = 60.0  # seconds to wait for initial zone data
# Rate-limit shadow metric extraction on noisy zone MQTT updates
_ZONE_SHADOW_REFRESH_MIN_INTERVAL = 1.5


class GeckoVesselCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for a single Gecko vessel/spa following Home Assistant best practices."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
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
        self.vessel_id = vessel_id
        self.monitor_id = monitor_id
        self.vessel_name = vessel_name

        # Store zones for this vessel only (no monitor_id dictionary needed)
        self._zones: dict[ZoneType, list[AbstractZone]] = {}

        # Store real-time state data for this vessel
        self._spa_state: dict[str, Any] = {}

        # Track if this vessel has received initial zone data
        self._has_initial_zones = False

        # Event to signal when initial zone data is loaded
        self._initial_zones_loaded_event = asyncio.Event()

        # Callbacks for zone updates (for dynamic entity creation)
        self._zone_update_callbacks: list = []

        # Callbacks for shadow metric discovery (for dynamic entity creation)
        self._shadow_metric_callbacks: list = []

        # Simple connection tracking
        self._consecutive_failures = 0

        # Extension metrics from device shadow (Waterlab, unknown zone types, extra features)
        self._shadow_metric_values: dict[str, float | int] = {}
        self._registered_shadow_metric_paths: set[str] = set()
        self._pending_new_metric_paths: set[str] = set()

        self._shadow_bool_values: dict[str, bool] = {}
        self._registered_bool_paths: set[str] = set()
        self._pending_bool_paths: set[str] = set()

        self._shadow_string_values: dict[str, str] = {}
        self._registered_string_paths: set[str] = set()
        self._pending_string_paths: set[str] = set()

        self._registered_number_paths: set[str] = set()
        self._pending_number_paths: set[str] = set()

        # Track if initial setup has been completed to avoid redundant refresh during platform setup
        self._initial_setup_done = False
        self._initial_setup_lock = asyncio.Lock()
        # Optional REST tile metrics (merged under ``cloud.rest.*``; shadow wins on overlap)
        self._cloud_tile_metrics: dict[str, float | int] = {}
        self._cloud_string_metrics: dict[str, str] = {}
        self._cloud_bool_metrics: dict[str, bool] = {}
        self._last_cloud_poll_monotonic: float | None = None

        # REST: unread messages (scoped) + vessel actions — not history.
        self._rest_alerts_snapshot: dict[str, Any] = {
            "total": 0,
            "messages": [],
            "actions": [],
            "updated_at": None,
            "error": None,
        }
        self._last_alerts_poll_monotonic: float | None = None
        self._last_zone_shadow_refresh_mono: float | None = None
        self._account_id_resolve_attempted = False

        # Premium energy data (populated by _async_poll_energy_if_due)
        self._energy_data: dict[str, Any] = {}
        self._last_energy_poll_monotonic: float | None = None

    def register_zone_update_callback(self, callback):
        """Register a callback to be called when zone data updates."""
        self._zone_update_callbacks.append(callback)

    def register_shadow_metric_callback(self, callback):
        """Register a callback to be called when new shadow metrics are discovered."""
        self._shadow_metric_callbacks.append(callback)

    async def _async_handle_zone_update(self, data: dict[str, Any]) -> None:
        """Handle zone update in the event loop."""
        now = time.monotonic()
        if (
            self._last_zone_shadow_refresh_mono is None
            or (now - self._last_zone_shadow_refresh_mono)
            >= _ZONE_SHADOW_REFRESH_MIN_INTERVAL
        ):
            self._last_zone_shadow_refresh_mono = now
            client = await self.get_gecko_client()
            self.sync_refresh_shadow_metrics(client)

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
                _LOGGER.warning(
                    "Error in zone update callback for vessel %s: %s",
                    self.vessel_name,
                    ex,
                    exc_info=True,
                )

    def _entry_options(self) -> dict[str, Any]:
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if not entry:
            return {}
        return dict(entry.options)

    def _config_account_id(self) -> str:
        """Account id from config entry (always current; not a copy from setup time)."""
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if not entry:
            return ""
        return str(entry.data.get("account_id", "")).strip()

    def _get_community_api_client(self):
        """Return the community-token API client (tiles, alerts, account resolution).

        Premium REST uses :meth:`_get_premium_api_client` so a failing app token
        refresh does not break basic cloud polling.
        """
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if not entry or not getattr(entry, "runtime_data", None):
            return None
        return getattr(entry.runtime_data, "api_client", None)

    async def _async_lazy_resolve_account_id(self) -> str:
        """Resolve and persist ``account_id`` when missing (retries after transient failures).

        Only sets ``_account_id_resolve_attempted`` after a successful persist so a
        token/network/API error on an earlier poll does not block later attempts.
        """
        if self._account_id_resolve_attempted:
            return ""
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if not entry or not getattr(entry, "runtime_data", None):
            return ""
        api = self._get_community_api_client()
        if not api:
            return ""
        try:
            user_id = await api.async_get_user_id()
            user_data = await api.async_get_user_info(user_id)
            acct = (user_data.get("account") or {}).get("accountId")
            account_id = str(acct).strip() if acct else ""
            if account_id:
                data = dict(entry.data)
                data["account_id"] = account_id
                self.hass.config_entries.async_update_entry(entry, data=data)
                self._account_id_resolve_attempted = True
                _LOGGER.info(
                    "Lazy-resolved account_id for %s (vessel %s)",
                    self.vessel_name,
                    self.vessel_id,
                )
                return account_id
        except Exception as err:
            _LOGGER.debug(
                "Lazy account_id resolution failed for %s: %s",
                self.vessel_name,
                err,
            )
        return ""

    async def _async_poll_cloud_tiles_if_due(
        self,
        connection: GeckoMonitorConnection | None,
    ) -> None:
        """Optionally refresh tile metrics from Gecko REST using config account/vessel IDs."""
        opts = self._entry_options()
        interval = int(
            opts.get(CONF_CLOUD_REST_POLL_INTERVAL, DEFAULT_CLOUD_REST_POLL_INTERVAL)
        )
        account_id = self._config_account_id()
        if not account_id:
            account_id = await self._async_lazy_resolve_account_id()
        if interval <= 0 or not account_id:
            _LOGGER.debug(
                "Cloud REST poll skipped for %s: interval=%d, has_account_id=%s",
                self.vessel_name,
                interval,
                bool(account_id),
            )
            return

        only_when_mqtt_down = bool(
            opts.get(
                CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
                DEFAULT_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
            )
        )
        mqtt_up = bool(connection and connection.is_connected)
        if only_when_mqtt_down and mqtt_up:
            # MQTT is healthy again: drop stale REST tile caches so cloud.rest.* does not
            # keep outage-era values merged into shadow metrics.
            if (
                self._cloud_tile_metrics
                or self._cloud_string_metrics
                or self._cloud_bool_metrics
            ):
                self._cloud_tile_metrics = {}
                self._cloud_string_metrics = {}
                self._cloud_bool_metrics = {}
                client = await self.get_gecko_client()
                self.sync_refresh_shadow_metrics(client)
            return

        now = time.monotonic()
        if (
            self._last_cloud_poll_monotonic is not None
            and (now - self._last_cloud_poll_monotonic) < interval
        ):
            return

        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if not entry or not getattr(entry, "runtime_data", None):
            return
        api = self._get_community_api_client()
        if not api:
            return
        rd = entry.runtime_data
        vessels: list[Any] | None = None
        try:
            async with rd.rest_vessels_cache_lock:
                if (
                    rd.rest_vessels_response_cache is not None
                    and rd.rest_vessels_response_cache_mono is not None
                    and rd.rest_vessels_cache_account_id == account_id
                    and (now - rd.rest_vessels_response_cache_mono) < interval
                ):
                    vessels = rd.rest_vessels_response_cache
                else:
                    vessels = await api.async_get_vessels(str(account_id))
                    rd.rest_vessels_response_cache = vessels
                    rd.rest_vessels_response_cache_mono = now
                    rd.rest_vessels_cache_account_id = account_id
        except (ClientError, TimeoutError, OSError) as err:
            _LOGGER.debug(
                "Cloud tile REST poll skipped for %s: %s", self.vessel_name, err
            )
            return

        self._last_cloud_poll_monotonic = now

        if not isinstance(vessels, list):
            return

        vessel_rec = find_vessel_record(vessels, self.vessel_id)
        if not vessel_rec:
            _LOGGER.debug(
                "Cloud tile REST: no vessel row for vessel_id=%s", self.vessel_id
            )
            return

        tile_metrics = extract_cloud_tile_metrics(vessel_rec)
        tile_strings = extract_cloud_tile_strings(vessel_rec)
        tile_bools = extract_cloud_tile_booleans(vessel_rec)

        # v6 vessel detail has a richer ``readings`` object with pH, ORP,
        # alkalinity, chlorine, etc. — data not available in the v4 list.
        vid = str(self.vessel_id)
        detail: dict | None = None
        try:
            async with rd.rest_vessel_detail_lock:
                cached_mono = rd.rest_vessel_detail_mono.get(vid)
                if (
                    cached_mono is not None
                    and (now - cached_mono) < interval
                    and vid in rd.rest_vessel_detail_cache
                ):
                    detail = rd.rest_vessel_detail_cache[vid]
            if detail is None:
                detail = await api.async_get_vessel_detail(str(account_id), vid)
                async with rd.rest_vessel_detail_lock:
                    rd.rest_vessel_detail_cache[vid] = detail
                    rd.rest_vessel_detail_mono[vid] = now
            if isinstance(detail, dict):
                readings_metrics = extract_vessel_readings_metrics(detail)
                readings_strings = extract_vessel_readings_strings(detail)
                action_metrics = extract_vessel_action_metrics(detail)
                action_strings = extract_vessel_action_strings(detail)
                disc_strings = extract_vessel_disc_strings(detail)
                tile_metrics.update(readings_metrics)
                tile_metrics.update(action_metrics)
                tile_strings.update(readings_strings)
                tile_strings.update(action_strings)
                tile_strings.update(disc_strings)
        except (ClientError, TimeoutError, OSError) as err:
            _LOGGER.debug(
                "v6 vessel detail poll skipped for %s: %s", self.vessel_name, err
            )

        self._cloud_tile_metrics = tile_metrics
        self._cloud_string_metrics = tile_strings
        self._cloud_bool_metrics = tile_bools

    async def _async_poll_alerts_if_due(self) -> None:
        """Poll Gecko REST for new/active alerts (messages + vessel actions)."""
        opts = self._entry_options()
        interval = int(
            opts.get(CONF_ALERTS_POLL_INTERVAL, DEFAULT_ALERTS_POLL_INTERVAL)
        )
        account_id = self._config_account_id()
        if interval <= 0 or not account_id:
            return

        now = time.monotonic()
        if (
            self._last_alerts_poll_monotonic is not None
            and (now - self._last_alerts_poll_monotonic) < interval
        ):
            return

        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if not entry or not getattr(entry, "runtime_data", None):
            return
        api = self._get_community_api_client()
        if not api:
            return
        rd = entry.runtime_data
        messages_payload: Any | None = None
        actions_payload: Any | None = None
        err: str | None = None
        try:
            async with rd.rest_alerts_messages_lock:
                if (
                    rd.rest_alerts_messages_payload is not None
                    and rd.rest_alerts_messages_mono is not None
                    and rd.rest_alerts_messages_account_id == account_id
                    and (now - rd.rest_alerts_messages_mono) < interval
                ):
                    messages_payload = rd.rest_alerts_messages_payload
                else:
                    messages_payload = await api.async_get_messages_unread(
                        str(account_id)
                    )
                    rd.rest_alerts_messages_payload = messages_payload
                    rd.rest_alerts_messages_mono = now
                    rd.rest_alerts_messages_account_id = account_id
        except (ClientError, TimeoutError, OSError) as exc:
            err = f"messages_unread:{type(exc).__name__}"
            _LOGGER.debug(
                "Alerts poll: messages/unread failed for %s: %s",
                self.vessel_name,
                exc,
            )
        try:
            cache_key = (str(account_id), str(self.vessel_id))
            async with rd.rest_alerts_actions_lock:
                cached = rd.rest_alerts_actions_cache.get(cache_key)
                if (
                    cached is not None
                    and cached.get("mono") is not None
                    and (now - cached["mono"]) < interval
                ):
                    actions_payload = cached.get("payload")
                else:
                    actions_payload = await api.async_get_vessel_actions_v2(
                        str(account_id), str(self.vessel_id)
                    )
                    rd.rest_alerts_actions_cache[cache_key] = {
                        "payload": actions_payload,
                        "mono": now,
                    }
        except (ClientError, TimeoutError, OSError) as exc:
            aerr = f"actions:{type(exc).__name__}"
            err = f"{err};{aerr}" if err else aerr
            _LOGGER.debug(
                "Alerts poll: vessel actions failed for %s: %s",
                self.vessel_name,
                exc,
            )

        self._last_alerts_poll_monotonic = now
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

    def _has_premium_api(self) -> bool:
        """Return True if the premium (app-token) API client is available."""
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if not entry or not getattr(entry, "runtime_data", None):
            return False
        return entry.runtime_data.app_api_client is not None

    def _get_premium_api_client(self):
        """Return the premium (app-token) API client or None."""
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if not entry or not getattr(entry, "runtime_data", None):
            return None
        return entry.runtime_data.app_api_client

    async def _async_poll_energy_if_due(self) -> None:
        """Poll premium energy endpoints if the app token is linked and interval elapsed."""
        if not self._has_premium_api():
            return

        account_id = self._config_account_id()
        if not account_id:
            account_id = await self._async_lazy_resolve_account_id()
        if not account_id:
            return

        opts = self._entry_options()
        interval = int(
            opts.get(CONF_ENERGY_POLL_INTERVAL, DEFAULT_ENERGY_POLL_INTERVAL)
        )
        if interval <= 0:
            return

        now = time.monotonic()
        if (
            self._last_energy_poll_monotonic is not None
            and (now - self._last_energy_poll_monotonic) < interval
        ):
            return

        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if not entry or not getattr(entry, "runtime_data", None):
            return
        rd = entry.runtime_data
        vid = str(self.vessel_id)
        aid = str(account_id)

        async with rd.energy_data_lock:
            cached_mono = rd.energy_data_mono.get(vid)
            if cached_mono is not None and (now - cached_mono) < interval:
                cached = rd.energy_data_cache.get(vid)
                if cached is not None:
                    self._energy_data = cached
                    self._last_energy_poll_monotonic = now
                    return

        api = self._get_premium_api_client()
        if not api:
            return

        energy: dict[str, Any] = {}
        for key, fetcher in (
            ("consumption", api.async_get_energy_consumption),
            ("score", api.async_get_energy_score),
            ("cost", api.async_get_energy_cost),
        ):
            try:
                energy[key] = await fetcher(aid, vid)
            except (ClientError, TimeoutError, OSError) as err:
                _LOGGER.debug(
                    "Energy %s poll failed for %s: %s", key, self.vessel_name, err
                )
                energy[key] = None

        self._energy_data = energy
        self._last_energy_poll_monotonic = now

        async with rd.energy_data_lock:
            rd.energy_data_cache[vid] = energy
            rd.energy_data_mono[vid] = now

        _LOGGER.debug(
            "Energy data refreshed for %s: keys=%s",
            self.vessel_name,
            [k for k, v in energy.items() if v is not None],
        )

    def get_energy_data(self) -> dict[str, Any]:
        """Return the latest energy data dict (consumption, score, cost)."""
        return dict(self._energy_data)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Gecko API."""
        try:
            connection_manager = await async_get_connection_manager(self.hass)
            connection = connection_manager.get_connection(self.monitor_id)

            try:
                await self._async_poll_cloud_tiles_if_due(connection)
            except (ClientError, TimeoutError, OSError) as err:
                _LOGGER.debug(
                    "Cloud tile poll failed for %s: %s", self.vessel_name, err
                )
            try:
                await self._async_poll_alerts_if_due()
            except (ClientError, TimeoutError, OSError) as err:
                _LOGGER.debug("Alerts poll failed for %s: %s", self.vessel_name, err)
            try:
                await self._async_poll_energy_if_due()
            except (ClientError, TimeoutError, OSError) as err:
                _LOGGER.debug("Energy poll failed for %s: %s", self.vessel_name, err)

            if not connection or not connection.is_connected:
                self._consecutive_failures += 1
                _LOGGER.debug(
                    "Update cycle: %s disconnected (failure %d/%d, "
                    "connection_exists=%s)",
                    self.vessel_name,
                    self._consecutive_failures,
                    MAX_CONSECUTIVE_FAILURES,
                    connection is not None,
                )

                if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    _LOGGER.warning(
                        "Connection lost for %s (monitor %s), attempting reconnect "
                        "after %d consecutive failures",
                        self.vessel_name,
                        self.monitor_id,
                        self._consecutive_failures,
                    )
                    await self._simple_reconnect()
                    self._consecutive_failures = 0
                    connection = connection_manager.get_connection(self.monitor_id)
                    if connection and connection.is_connected:
                        client = await self.get_gecko_client()
                        self.sync_refresh_shadow_metrics(client)
                        return {"status": "active", "vessel_id": self.vessel_id}

                self.sync_refresh_shadow_metrics(None)
                return {"status": "disconnected", "vessel_id": self.vessel_id}

            self._consecutive_failures = 0

            client = await self.get_gecko_client()
            self.sync_refresh_shadow_metrics(client)

            return {"status": "active", "vessel_id": self.vessel_id}
        except Exception as exception:
            _LOGGER.debug(
                "Update cycle exception for %s (monitor %s): %s",
                self.vessel_name,
                self.monitor_id,
                exception,
            )
            raise UpdateFailed(
                f"Error communicating with Gecko API for vessel {self.vessel_name}: {exception}"
            ) from exception

    def get_zones_by_type(self, zone_type: ZoneType) -> list[AbstractZone]:
        """Get zones of a specific type for this vessel (no monitor_id needed)."""
        return self._zones.get(zone_type, [])

    def get_all_zones(self) -> dict[ZoneType, list[AbstractZone]]:
        """Get all zones for this vessel."""
        return self._zones

    def sync_refresh_shadow_metrics(self, gecko_client: Any | None) -> None:
        """Parse extension metrics from shadow; merge optional REST tile metrics (MQTT wins)."""
        # Ensure we're running on the event loop since callbacks may call async_add_entities
        if self.hass.loop != asyncio.get_running_loop():
            raise RuntimeError(
                "sync_refresh_shadow_metrics must be called from the event loop"
            )
        state = getattr(gecko_client, "_state", None) if gecko_client else None
        mqtt_metrics = extract_extension_metrics(state) if state else {}
        merged: dict[str, float | int] = dict(self._cloud_tile_metrics)
        merged.update(mqtt_metrics)
        self._shadow_metric_values = merged

        reserved_numbers = {
            p for p in self._shadow_metric_values if path_reserved_for_number_control(p)
        }
        new_metric_paths = (
            set(self._shadow_metric_values)
            - reserved_numbers
            - self._registered_shadow_metric_paths
        )
        self._pending_new_metric_paths |= new_metric_paths
        new_number_paths = reserved_numbers - self._registered_number_paths
        self._pending_number_paths |= new_number_paths

        mqtt_bools = extract_extension_booleans(state) if state else {}
        merged_bools: dict[str, bool] = dict(self._cloud_bool_metrics)
        merged_bools.update(mqtt_bools)
        self._shadow_bool_values = merged_bools
        new_bool_paths = set(self._shadow_bool_values) - self._registered_bool_paths
        self._pending_bool_paths |= new_bool_paths

        mqtt_strings = extract_extension_strings(state) if state else {}
        merged_strings: dict[str, str] = dict(self._cloud_string_metrics)
        merged_strings.update(mqtt_strings)
        self._shadow_string_values = {
            k: clamp_sensor_native_str(v) for k, v in merged_strings.items()
        }
        new_string_paths = (
            set(self._shadow_string_values) - self._registered_string_paths
        )
        self._pending_string_paths |= new_string_paths

        if new_metric_paths or new_number_paths or new_bool_paths or new_string_paths:
            for callback in self._shadow_metric_callbacks:
                try:
                    if callable(callback):
                        callback()
                except Exception as ex:
                    _LOGGER.error(
                        "Error in shadow metric callback for vessel %s: %s",
                        self.vessel_name,
                        ex,
                        exc_info=True,
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
        """Paths not yet bound to binary_sensor entities; marks them registered."""
        out = sorted(self._pending_bool_paths)
        self._registered_bool_paths.update(out)
        self._pending_bool_paths.clear()
        return out

    def take_pending_string_paths(self) -> list[str]:
        """Paths not yet bound to sensor (string) entities; marks them registered."""
        out = sorted(self._pending_string_paths)
        self._registered_string_paths.update(out)
        self._pending_string_paths.clear()
        return out

    def get_shadow_bool_value(self, path: str) -> bool | None:
        """Return a bool leaf from the last shadow refresh, or None if absent."""
        if path not in self._shadow_bool_values:
            return None
        return self._shadow_bool_values[path]

    def get_shadow_string_value(self, path: str) -> str | None:
        """Return a string leaf from the last shadow refresh, or None if absent."""
        if path not in self._shadow_string_values:
            return None
        return self._shadow_string_values[path]

    async def async_ensure_initial_setup(self) -> None:
        """Ensure initial async refresh and shadow metrics refresh happen only once."""
        if self._initial_setup_done:
            return
        async with self._initial_setup_lock:
            if self._initial_setup_done:
                return
            _LOGGER.debug(
                "Starting initial setup for %s (monitor %s)",
                self.vessel_name,
                self.monitor_id,
            )
            _t0 = time.monotonic()
            await self.async_refresh()
            client = await self.get_gecko_client()
            if client:
                await self.async_wait_for_initial_zone_data(timeout=15.0)
            else:
                _LOGGER.debug(
                    "Skipping initial zone wait for %s (no MQTT client yet); "
                    "REST/shadow entities still initialize from refresh",
                    self.vessel_name,
                )
            self._initial_setup_done = True
            _LOGGER.debug(
                "Initial setup for %s completed in %.1fs (has_client=%s, has_zones=%s)",
                self.vessel_name,
                time.monotonic() - _t0,
                client is not None,
                self._has_initial_zones,
            )

    async def _simple_reconnect(self) -> None:
        """Simple reconnection - let geckoIotClient handle token refresh."""
        try:
            _LOGGER.debug(
                "Initiating reconnect for %s (monitor %s)",
                self.vessel_name,
                self.monitor_id,
            )
            _t0 = time.monotonic()
            connection_manager = await async_get_connection_manager(self.hass)
            success = await connection_manager.async_reconnect_monitor(self.monitor_id)

            if success:
                _LOGGER.info(
                    "Reconnected %s (monitor %s) in %.1fs",
                    self.vessel_name,
                    self.monitor_id,
                    time.monotonic() - _t0,
                )
            else:
                _LOGGER.error(
                    "Failed to reconnect %s (monitor %s) after %.1fs",
                    self.vessel_name,
                    self.monitor_id,
                    time.monotonic() - _t0,
                )

        except Exception as e:
            _LOGGER.error(
                "Failed to reconnect %s (monitor %s): %s",
                self.vessel_name,
                self.monitor_id,
                e,
                exc_info=True,
            )

    async def get_gecko_client(self):
        """Get the gecko client for this vessel's monitor."""
        try:
            connection_manager = await async_get_connection_manager(self.hass)
            connection = connection_manager.get_connection(self.monitor_id)

            if connection and connection.is_connected:
                return connection.gecko_client

            if connection is not None:
                _LOGGER.debug(
                    "Gecko client not ready for %s (monitor %s); "
                    "connection exists, MQTT still establishing or reconnecting",
                    self.vessel_name,
                    self.monitor_id,
                )
            else:
                _LOGGER.debug(
                    "No connection registered yet for vessel %s (monitor %s); "
                    "setup may still be in progress or retrying after timeout",
                    self.vessel_name,
                    self.monitor_id,
                )
            return None

        except Exception as e:
            _LOGGER.error(
                "Failed to get gecko client for vessel %s: %s",
                self.vessel_name,
                e,
                exc_info=True,
            )
            return None

    def _create_refresh_token_callback(self, websocket_url: str):
        """Create a refresh token callback for this vessel's monitor.

        This callback is invoked by the geckoIotClient when websocket tokens expire
        or are about to expire. It fetches a fresh websocket URL with new JWT tokens
        from the Gecko API using the OAuth2-managed access token.
        """

        def refresh_token_callback(
            monitor_id: str | None = None,
        ) -> str:
            """Handle token refresh by getting a new websocket URL.

            This is a synchronous callback invoked from background threads by the
            geckoIotClient library. We use run_coroutine_threadsafe to safely
            execute the async API call on Home Assistant's event loop.

            Args:
                monitor_id: Monitor ID to refresh; defaults to this coordinator's
                    ``monitor_id`` when omitted.

            Returns:
                New websocket URL with fresh JWT token, or original URL on failure
            """
            # Use provided monitor_id or fall back to coordinator's monitor_id
            target_monitor_id = monitor_id or self.monitor_id

            try:
                _LOGGER.debug(
                    "Token refresh callback invoked for %s (monitor %s)",
                    self.vessel_name,
                    target_monitor_id,
                )
                entry = self.hass.config_entries.async_get_entry(self.entry_id)
                if not entry:
                    _LOGGER.error(
                        "Config entry %s not found for vessel %s - cannot refresh token",
                        self.entry_id,
                        self.vessel_name,
                    )
                    return websocket_url

                # Get API client from runtime data
                if not hasattr(entry, "runtime_data") or not entry.runtime_data:
                    _LOGGER.error(
                        "No runtime_data found for vessel %s - cannot refresh token",
                        self.vessel_name,
                    )
                    return websocket_url

                api_client = entry.runtime_data.api_client
                if not api_client:
                    _LOGGER.error(
                        "No API client found for vessel %s - cannot refresh token",
                        self.vessel_name,
                    )
                    return websocket_url

                # Fetch new livestream URL with fresh JWT token
                # This is a sync callback from background thread, so use run_coroutine_threadsafe
                future = asyncio.run_coroutine_threadsafe(
                    api_client.async_get_monitor_livestream(target_monitor_id),
                    self.hass.loop,
                )

                # Wait for the API call to complete (with timeout)
                livestream_data = future.result(timeout=30.0)

                new_url = livestream_data.get("brokerUrl")
                if new_url:
                    _LOGGER.debug(
                        "Token refresh succeeded for %s (url_changed=%s)",
                        self.vessel_name,
                        new_url != websocket_url,
                    )
                    return new_url

                _LOGGER.error(
                    "No brokerUrl in livestream response for vessel %s",
                    self.vessel_name,
                )
                return websocket_url

            except TimeoutError:
                _LOGGER.error(
                    "Timeout fetching new websocket URL for vessel %s - API call took too long",
                    self.vessel_name,
                )
                return websocket_url
            except Exception as e:
                _LOGGER.error(
                    "Failed to refresh token for vessel %s: %s",
                    self.vessel_name,
                    e,
                    exc_info=True,
                )
                return websocket_url

        return refresh_token_callback

    async def async_setup_monitor_connection(self, websocket_url: str) -> None:
        """Set up a connection to this vessel's monitor using the singleton connection manager.

        Raises on failure (callers should catch and handle accordingly).
        """
        try:
            connection_manager = await async_get_connection_manager(self.hass)

            def on_zone_update(updated_zones):
                self._zones = updated_zones

                if not self._has_initial_zones:
                    self._has_initial_zones = True
                    if not self._initial_zones_loaded_event.is_set():
                        self._initial_zones_loaded_event.set()

                asyncio.run_coroutine_threadsafe(
                    self._async_handle_zone_update({"last_update": "zone_update"}),
                    self.hass.loop,
                )

            refresh_token_callback = self._create_refresh_token_callback(websocket_url)

            await connection_manager.async_get_or_create_connection(
                monitor_id=self.monitor_id,
                websocket_url=websocket_url,
                vessel_name=self.vessel_name,
                update_callback=on_zone_update,
                refresh_token_callback=refresh_token_callback,
            )

        except Exception as e:
            _LOGGER.debug(
                "Failed to set up connection for vessel %s: %s",
                self.vessel_name,
                e,
            )
            raise

    async def async_get_operation_mode_status(self):
        """Get operation mode status for this vessel's monitor."""
        gecko_client = await self.get_gecko_client()
        if gecko_client:
            return gecko_client.operation_mode_status
        return None

    def update_spa_state(self, state_data: dict[str, Any]) -> None:
        """Update spa state data and trigger coordinator update."""
        self._spa_state = state_data

        # Schedule the async call to run on the event loop from background thread
        asyncio.run_coroutine_threadsafe(
            self._async_handle_zone_update({"last_update": state_data}), self.hass.loop
        )

    async def async_wait_for_initial_zone_data(
        self, timeout: float = INITIAL_ZONE_TIMEOUT
    ) -> bool:
        """Wait for this vessel to receive its initial zone data."""
        _LOGGER.debug(
            "Waiting up to %.1fs for initial zone data for %s",
            timeout,
            self.vessel_name,
        )
        _t0 = time.monotonic()
        try:
            await asyncio.wait_for(
                self._initial_zones_loaded_event.wait(), timeout=timeout
            )
            _LOGGER.debug(
                "Initial zone data loaded for %s in %.1fs",
                self.vessel_name,
                time.monotonic() - _t0,
            )
            return True
        except TimeoutError:
            _LOGGER.warning(
                "Timeout waiting for initial zone data for %s "
                "(waited %.1fs, limit %.1fs)",
                self.vessel_name,
                time.monotonic() - _t0,
                timeout,
            )
            return False

    def get_spa_state(self) -> dict[str, Any] | None:
        """Get spa state data for this vessel."""
        return self._spa_state

    async def async_shutdown(self) -> None:
        """Shutdown coordinator and cleanup resources."""
        _LOGGER.debug(
            "Shutting down coordinator for vessel %s (entry %s)",
            self.vessel_name,
            self.entry_id,
        )

        # Connection manager will handle cleanup during Home Assistant shutdown
        # We don't disconnect here as the connection may be shared
        _LOGGER.debug(
            "Coordinator releasing vessel %s (monitor %s)",
            self.vessel_name,
            self.monitor_id,
        )

        self._zones.clear()
        self._spa_state.clear()
        self._zone_update_callbacks.clear()
        self._shadow_metric_callbacks.clear()
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
        self._cloud_bool_metrics.clear()
        self._last_cloud_poll_monotonic = None
        self._energy_data.clear()
        self._last_energy_poll_monotonic = None
        self._rest_alerts_snapshot = {
            "total": 0,
            "messages": [],
            "actions": [],
            "updated_at": None,
            "error": None,
        }
        self._last_alerts_poll_monotonic = None
