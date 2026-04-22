import logging
from typing import Any

from gecko_iot_client import GeckoApiClient
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import API_BASE_URL, AUTH0_URL_BASE

_LOGGER = logging.getLogger(__name__)


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


class OAuthGeckoApi(GeckoSpaApiMixin, GeckoApiClient):
    """Provide gecko authentication tied to an OAuth2 based config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        oauth_session: config_entry_oauth2_flow.OAuth2Session,
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
