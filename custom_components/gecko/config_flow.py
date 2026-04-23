"""Config flow for Gecko."""

import base64
import json
import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from aiohttp import ClientResponseError
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import (
    config_entry_oauth2_flow,
)
from homeassistant.helpers import (
    config_validation as cv,
)

from .const import (
    CONF_ALERTS_POLL_INTERVAL,
    CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
    CONF_CLOUD_REST_POLL_INTERVAL,
    DEFAULT_ALERTS_POLL_INTERVAL,
    DEFAULT_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
    DEFAULT_CLOUD_REST_POLL_INTERVAL,
    DOMAIN,
    OAUTH2_AUTHORIZE,
    OAUTH2_CLIENT_ID,
    OAUTH2_TOKEN,
)
from .oauth_implementation import GeckoPKCEOAuth2Implementation

_LOGGER = logging.getLogger(__name__)


def _decode_jwt_payload(token: str) -> dict | None:
    """Decode the payload of a JWT without signature verification.

    Used only to extract claims (org_id, sub, etc.) as a fallback when the
    /v2/user endpoint returns 404.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        return json.loads(payload_bytes)
    except Exception:  # noqa: BLE001
        return None


class ConfigFlow(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Config flow to handle Gecko OAuth2 authentication."""

    DOMAIN = DOMAIN
    VERSION = 2

    _reauth_entry: config_entries.ConfigEntry | None = None

    async def async_step_user(self, user_input=None):
        """Handle a flow initialized by the user."""
        # Register the hardcoded OAuth implementation if not already registered
        await self.async_register_implementation()
        return await super().async_step_user(user_input)

    async def async_register_implementation(self):
        """Register the OAuth implementation."""
        # Check if already registered to avoid duplicates
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
                    client_id=OAUTH2_CLIENT_ID,
                    authorize_url=OAUTH2_AUTHORIZE,
                    token_url=OAUTH2_TOKEN,
                ),
            )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Handle re-authentication when the token can no longer be refreshed."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Confirm re-authentication with the user."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        await self.async_register_implementation()
        return await super().async_step_user(user_input)

    async def async_oauth_create_entry(self, data: dict):
        """Create an entry after OAuth authentication, or update on reauth."""
        # Reauth path: update only the token, preserve all other stored data.
        if self._reauth_entry is not None:
            new_data = {**self._reauth_entry.data, "token": data["token"]}
            self.hass.config_entries.async_update_entry(
                self._reauth_entry, data=new_data
            )
            await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        # Normal first-time setup path.
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
        """Resolve user ID and account information.

        Primary path: Auth0 userinfo → /v2/user/{userId}.
        Fallback: decode the JWT access token for org_id / account claims
        when the user-profile endpoint returns 404 (common for newer or
        social-login accounts that haven't been fully provisioned yet).
        """
        user_id: str | None = None
        try:
            user_id = await api_client.async_get_user_id()
        except Exception as err:
            _LOGGER.warning("Could not fetch Auth0 userinfo: %s", err)

        # Primary: /v2/user endpoint
        if user_id:
            try:
                user_data = await api_client.async_get_user_info(user_id)
                account_data = user_data.get("account", {})
                account_id = str(account_data.get("accountId", ""))
                if account_id:
                    return user_id, account_data, account_id
            except ClientResponseError as err:
                if err.status == 404:
                    _LOGGER.warning(
                        "Gecko /v2/user endpoint returned 404 for %s — "
                        "trying JWT fallback",
                        user_id,
                    )
                else:
                    raise ConnectionError(
                        f"Failed to resolve user and account: {err}"
                    ) from err
            except Exception as err:
                _LOGGER.warning("User info API call failed: %s", err)

        # Fallback: decode JWT claims from the access token
        access_token = data.get("token", {}).get("access_token", "")
        jwt_claims = _decode_jwt_payload(access_token)

        if jwt_claims:
            org_id = jwt_claims.get("org_id", "")
            jwt_account = (
                jwt_claims.get("https://geckoal.com/account_id", "")
                or jwt_claims.get("account_id", "")
                or org_id
            )
            jwt_sub = jwt_claims.get("sub", "") or (user_id or "")

            if jwt_account:
                _LOGGER.info(
                    "Resolved account via JWT fallback (org_id=%s)", jwt_account
                )
                return jwt_sub, {"name": "Account"}, str(jwt_account)

        raise ConnectionError(
            f"Could not resolve Gecko account for user {user_id}. "
            "The Gecko API returned 404 for your user profile. This can "
            "happen with newer accounts — please ensure you have at least "
            "one vessel/spa linked in the Gecko mobile app, then try again."
        )

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
