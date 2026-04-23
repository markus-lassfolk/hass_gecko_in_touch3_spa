"""Config flow for Gecko.

Uses the Gecko mobile-app OAuth client which requires a native Capacitor
redirect URI.  Because Home Assistant's standard OAuth popup cannot follow a
``com.geckoportal.gecko://`` redirect, the flow asks the user to open the
authorize URL manually, complete login, and paste the resulting callback URL
back into a text field.  This is a one-time step; subsequent token refreshes
are automatic.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import voluptuous as vol
from aiohttp import ClientError, ClientResponseError
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers import config_validation as cv
from yarl import URL

from .const import (
    CONF_ALERTS_POLL_INTERVAL,
    CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
    CONF_CLOUD_REST_POLL_INTERVAL,
    DEFAULT_ALERTS_POLL_INTERVAL,
    DEFAULT_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
    DEFAULT_CLOUD_REST_POLL_INTERVAL,
    DOMAIN,
    OAUTH2_APP_CLIENT_ID,
    OAUTH2_APP_REDIRECT_URI,
    OAUTH2_AUTHORIZE,
    OAUTH2_TOKEN,
)
from .oauth_implementation import GeckoPKCEOAuth2Implementation

_LOGGER = logging.getLogger(__name__)


def _extract_code_from_callback(raw: str) -> str | None:
    """Extract the ``code`` query parameter from a pasted callback URL.

    Handles the full native-scheme URL, bare ``code=…`` fragments, and
    various copy-paste artifacts.
    """
    raw = raw.strip()
    if not raw:
        return None

    if raw.startswith("code=") or raw.startswith("?code="):
        raw = f"https://placeholder/{raw}" if raw.startswith("?") else f"https://placeholder/?{raw}"

    try:
        parsed = urlparse(raw)
        qs = parse_qs(parsed.query)
        codes = qs.get("code")
        if codes:
            return codes[0]
    except Exception:  # noqa: BLE001
        pass

    return None


class ConfigFlow(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Config flow to handle Gecko OAuth2 authentication.

    Bypasses the standard HA OAuth external-step popup because the mobile-app
    client requires a native redirect URI.  Instead, the user pastes the
    callback URL manually after completing login in their browser.
    """

    DOMAIN = DOMAIN
    VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        super().__init__()
        self._code_verifier: str | None = None
        self._authorize_url: str | None = None

    async def async_step_user(self, user_input=None):
        """Handle a flow initialized by the user."""
        await self._async_ensure_implementation()
        return await self.async_step_authorize()

    async def _async_ensure_implementation(self) -> None:
        """Register the OAuth implementation and set ``flow_impl``."""
        implementations = await config_entry_oauth2_flow.async_get_implementations(
            self.hass, DOMAIN
        )
        if DOMAIN not in implementations:
            config_entry_oauth2_flow.async_register_implementation(
                self.hass,
                DOMAIN,
                GeckoPKCEOAuth2Implementation(
                    self.hass,
                    DOMAIN,
                    client_id=OAUTH2_APP_CLIENT_ID,
                    authorize_url=OAUTH2_AUTHORIZE,
                    token_url=OAUTH2_TOKEN,
                ),
            )
            implementations = await config_entry_oauth2_flow.async_get_implementations(
                self.hass, DOMAIN
            )
        self.flow_impl = implementations[DOMAIN]

    async def async_step_authorize(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show the authorize URL and accept the pasted callback."""
        errors: dict[str, str] = {}

        if user_input is not None:
            code = _extract_code_from_callback(user_input.get("callback_url", ""))
            if code is None:
                errors["callback_url"] = "invalid_callback_url"
            else:
                token = await self._async_exchange_code(code)
                if token is None:
                    errors["base"] = "token_exchange_failed"
                else:
                    token["expires_in"] = int(token["expires_in"])
                    token["expires_at"] = time.time() + token["expires_in"]
                    return await self.async_oauth_create_entry(
                        {"auth_implementation": DOMAIN, "token": token}
                    )

        if self._authorize_url is None:
            self._code_verifier = GeckoPKCEOAuth2Implementation.generate_code_verifier()
            challenge = GeckoPKCEOAuth2Implementation.compute_code_challenge(
                self._code_verifier
            )
            self._authorize_url = str(
                URL(OAUTH2_AUTHORIZE).with_query(
                    {
                        "response_type": "code",
                        "client_id": OAUTH2_APP_CLIENT_ID,
                        "redirect_uri": OAUTH2_APP_REDIRECT_URI,
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "scope": "openid profile email offline_access",
                        "audience": "https://api.geckowatermonitor.com",
                    }
                )
            )

        return self.async_show_form(
            step_id="authorize",
            data_schema=vol.Schema(
                {vol.Required("callback_url"): str}
            ),
            description_placeholders={"authorize_url": self._authorize_url},
            errors=errors,
        )

    async def _async_exchange_code(self, code: str) -> dict | None:
        """Exchange an authorization code for tokens using the stored PKCE verifier."""
        if self._code_verifier is None or self.flow_impl is None:
            return None
        try:
            return await self.flow_impl._token_request(  # noqa: SLF001
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": OAUTH2_APP_REDIRECT_URI,
                    "code_verifier": self._code_verifier,
                    "client_id": OAUTH2_APP_CLIENT_ID,
                }
            )
        except (ClientResponseError, ClientError) as err:
            _LOGGER.error("Token exchange failed: %s", err)
            return None

    async def async_oauth_create_entry(self, data: dict):
        """Create an entry after OAuth authentication."""
        # Get available vessels from the cloud API
        try:
            # Create a simple API client using just the access token for initial API calls
            from .api import ConfigFlowGeckoApi

            api_client = ConfigFlowGeckoApi(self.hass, data["token"]["access_token"])

            # Get user ID and account information
            user_id, account_data, account_id = await self._resolve_user_and_account(
                data, api_client
            )

            # Get vessels for the account
            vessels = await api_client.async_get_vessels(account_id)

            if not vessels:
                self.logger.warning("No vessels found for account %s", account_id)
                return self.async_create_entry(
                    title=f"Gecko - {account_data.get('name', 'Account')}",
                    data={
                        **data,
                        "vessels": [],
                        "account_id": account_id,
                        "user_id": user_id,
                        "account_info": account_data,
                    },
                )

            # Fetch spa configuration for each vessel
            vessels_with_config = []
            for vessel in vessels:
                try:
                    monitor_id = vessel.get("monitorId") or vessel.get("vesselId")
                    if monitor_id:
                        spa_config = await api_client.async_get_spa_configuration(
                            account_id, str(monitor_id)
                        )
                        vessel_with_config = {**vessel, "spa_configuration": spa_config}
                        vessels_with_config.append(vessel_with_config)
                    else:
                        _LOGGER.warning(
                            "No monitor ID found for vessel %s", vessel.get("name")
                        )
                        vessels_with_config.append(vessel)  # Add without config
                except Exception as config_err:
                    _LOGGER.warning(
                        "Failed to get spa config for vessel %s: %s",
                        vessel.get("name"),
                        config_err,
                    )
                    vessels_with_config.append(vessel)  # Add without config

            # Create one main entry for the account with all vessels and their configurations
            return self.async_create_entry(
                title=f"Gecko - {account_data.get('name', 'Account')} ({len(vessels_with_config)} vessels)",
                data={
                    **data,
                    "vessels": vessels_with_config,
                    "account_id": account_id,
                    "user_id": user_id,
                    "account_info": account_data,
                },
            )
        except Exception as err:
            self.logger.error("Failed to get vessels from Gecko API: %s", err)
            return self.async_abort(reason="api_error")

    async def _resolve_user_and_account(
        self, data: dict, api_client
    ) -> tuple[str, dict, str]:
        """Resolve user ID and account information."""
        try:
            # Step 1: Get user ID from Auth0 userinfo endpoint
            user_id = await api_client.async_get_user_id()

            # Step 2: Call our own API's /v2/user/:userId endpoint to get account information
            user_data = await api_client.async_get_user_info(user_id)

            account_data = user_data.get("account", {})
            account_id = str(account_data.get("accountId", ""))

            if not account_id:
                raise ValueError("No account ID found in user data")

            return user_id, account_data, account_id

        except Exception as err:
            raise ConnectionError(f"Failed to resolve user and account: {err}") from err

    async def _get_user_id_from_api(self, api_client) -> str | None:
        """Try to get user ID from API calls."""
        # First, try to extract user ID directly from the JWT token
        user_id = api_client.extract_user_id_from_token()
        if user_id:
            return user_id

        # If token extraction fails, try OAuth userinfo endpoint
        try:
            userinfo = await api_client.async_get_oauth_userinfo()
            return userinfo.get("sub")
        except Exception:
            return None

    def _extract_user_id_from_token(self, token: dict[str, Any]) -> str | None:
        """Extract user ID from the OAuth token."""
        # Try direct fields in token first
        for field in ["user_id", "userId", "uid", "id", "sub"]:
            if field in token:
                return str(token[field])

        # Check user_info nested object
        user_info = token.get("user_info", {})
        if isinstance(user_info, dict):
            for field in ["user_id", "id", "userId", "uid"]:
                if field in user_info:
                    return str(user_info[field])

        return None

    @property
    def logger(self) -> logging.Logger:
        """Return logger."""
        return logging.getLogger(__name__)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Options for optional REST enrichment."""
        return GeckoOptionsFlow()


class GeckoOptionsFlow(config_entries.OptionsFlow):
    """Integration options (REST poll for app-style tiles when MQTT is quiet)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Configure optional cloud REST polling."""
        opts = dict(self.config_entry.options)

        if user_input is not None:
            # Merge onto existing options so internal keys (e.g. one-time migration
            # stamps from ``_migrate_options_defaults``) are not dropped.
            merged_options = dict(self.config_entry.options)
            merged_options.update(user_input)

            old_alerts = int(
                opts.get(CONF_ALERTS_POLL_INTERVAL, DEFAULT_ALERTS_POLL_INTERVAL)
            )
            new_alerts = int(
                user_input.get(CONF_ALERTS_POLL_INTERVAL, DEFAULT_ALERTS_POLL_INTERVAL)
            )
            if (old_alerts > 0) != (new_alerts > 0):
                # Crossing zero changes which platforms register REST alert entities.
                # Reload here — do not also register an entry update listener that
                # reloads on the same toggle (double reload / duplicate MQTT setup).
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    options=merged_options,
                )
                await self.hass.config_entries.async_reload(self.config_entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")
            return self.async_create_entry(title="", data=merged_options)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_CLOUD_REST_POLL_INTERVAL,
                    default=opts.get(
                        CONF_CLOUD_REST_POLL_INTERVAL,
                        DEFAULT_CLOUD_REST_POLL_INTERVAL,
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=86400)),
                vol.Optional(
                    CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
                    default=opts.get(
                        CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
                        DEFAULT_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
                    ),
                ): cv.boolean,
                vol.Optional(
                    CONF_ALERTS_POLL_INTERVAL,
                    default=opts.get(
                        CONF_ALERTS_POLL_INTERVAL,
                        DEFAULT_ALERTS_POLL_INTERVAL,
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=86400)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
