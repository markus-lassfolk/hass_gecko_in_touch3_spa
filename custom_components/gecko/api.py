"""Gecko REST helpers layered on ``gecko_iot_client`` (OAuth session, HA HTTP client)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Protocol

from gecko_iot_client import GeckoApiClient
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import API_BASE_URL, AUTH0_URL_BASE

_LOGGER = logging.getLogger(__name__)


class OAuth2SessionProtocol(Protocol):
    """Protocol for OAuth2 session objects (OAuth2Session and AppTokenSession)."""

    @property
    def token(self) -> dict:
        """Return the token dict."""
        ...

    async def async_ensure_token_valid(self) -> None:
        """Ensure the token is valid, refreshing if needed."""
        ...


CLOCK_OUT_OF_SYNC_MAX_SEC = 20


class AppTokenSession:
    """Token session for the optional app-client OAuth token.

    Mirrors the interface of ``config_entry_oauth2_flow.OAuth2Session``
    (``token``, ``async_ensure_token_valid``) but reads/writes
    ``config_entry.data["app_token"]`` instead of ``data["token"]``.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        implementation: config_entry_oauth2_flow.AbstractOAuth2Implementation,
    ) -> None:
        """Initialize the app-token session."""
        self.hass = hass
        self.config_entry = config_entry
        self._implementation = implementation
        self._token_lock = asyncio.Lock()

    @property
    def token(self) -> dict:
        """Return the app token dict (empty mapping if key missing or malformed)."""
        tok = self.config_entry.data.get("app_token")
        return tok if isinstance(tok, dict) else {}

    @property
    def valid_token(self) -> bool:
        """Return whether the app token is still valid."""
        expires_at = self.token.get("expires_at", 0)
        return float(expires_at) > time.time() + CLOCK_OUT_OF_SYNC_MAX_SEC

    async def async_ensure_token_valid(self) -> None:
        """Refresh the app token if expired."""
        async with self._token_lock:
            if self.valid_token:
                return
            new_token = await self._implementation.async_refresh_token(self.token)
            if not isinstance(new_token, dict):
                return
            if (
                "expires_at" not in new_token
                and new_token.get("expires_in") is not None
            ):
                new_token = {
                    **new_token,
                    "expires_at": time.time() + int(new_token["expires_in"]),
                }
            data = {**self.config_entry.data, "app_token": new_token}
            self.hass.config_entries.async_update_entry(self.config_entry, data=data)


class GeckoSpaApiMixin:
    """REST helpers not yet on the published ``gecko_iot_client`` wheel."""

    async def async_get_spa_configuration(
        self, account_id: str, monitor_id: str
    ) -> dict[str, Any]:
        """Return spa metadata for a monitor (Gecko cloud REST)."""
        return await self.async_request(
            "GET",
            f"/v4/accounts/{account_id}/monitors/{monitor_id}/spa/configuration",
        )

    async def async_get_messages_unread(self, account_id: str) -> Any:
        """Unread account messages (may be 403 for some consumer tokens)."""
        return await self.async_request(
            "GET",
            f"/v1/accounts/{account_id}/messages/unread",
        )

    async def async_get_vessel_actions_v2(self, account_id: str, vessel_id: str) -> Any:
        """Vessel-scoped actions (often used for prompts / alerts in the app)."""
        return await self.async_request(
            "GET",
            f"/v2/accounts/{account_id}/vessels/{vessel_id}/actions",
        )

    async def async_get_vessel_detail(
        self, account_id: str, vessel_id: str
    ) -> dict[str, Any]:
        """V6 vessel detail with full readings (pH, ORP, alkalinity, chlorine, etc.)."""
        return await self.async_request(
            "GET",
            f"/v6/accounts/{account_id}/vessels/{vessel_id}?customActionsVersion=0",
        )

    # -- Premium (app-token) endpoints -------------------------------------

    async def async_get_energy_consumption(
        self, account_id: str, vessel_id: str
    ) -> Any:
        """Energy consumption data (requires app-client token)."""
        return await self.async_request(
            "GET",
            f"/v1/accounts/{account_id}/vessels/{vessel_id}/energy-consumption",
        )

    async def async_get_energy_score(self, account_id: str, vessel_id: str) -> Any:
        """Energy efficiency score (requires app-client token)."""
        return await self.async_request(
            "GET",
            f"/v1/accounts/{account_id}/vessels/{vessel_id}/energy/score",
        )

    async def async_get_energy_cost(self, account_id: str, vessel_id: str) -> Any:
        """Energy cost data (requires app-client token)."""
        return await self.async_request(
            "GET",
            f"/v1/accounts/{account_id}/vessels/{vessel_id}/energyCost",
        )


class OAuthGeckoApi(GeckoSpaApiMixin, GeckoApiClient):
    """Provide gecko authentication tied to an OAuth2 based config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        oauth_session: OAuth2SessionProtocol,
    ) -> None:
        """Initialize OAuthGeckoApi."""
        websession = async_get_clientsession(hass)
        super().__init__(websession, api_url=API_BASE_URL, auth0_url=AUTH0_URL_BASE)
        self._oauth_session = oauth_session

    async def async_get_access_token(self) -> str:
        """Return a valid access token for the Gecko API."""
        await self._oauth_session.async_ensure_token_valid()
        return self._oauth_session.token["access_token"]


class ConfigFlowGeckoApi(GeckoSpaApiMixin, GeckoApiClient):
    """Profile gecko authentication before a ConfigEntry exists.

    This implementation directly provides the token without supporting refresh.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        token: str,
    ) -> None:
        """Initialize ConfigFlowGeckoApi."""
        websession = async_get_clientsession(hass)
        super().__init__(websession, api_url=API_BASE_URL, auth0_url=AUTH0_URL_BASE)
        self._token = token

    async def async_get_access_token(self) -> str:
        """Return the access token for the Gecko API."""
        return self._token
