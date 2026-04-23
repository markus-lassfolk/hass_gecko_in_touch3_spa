"""Config flow for Gecko.

Initial setup uses HA's standard OAuth popup (community client) for a seamless
experience.  Power users can optionally link a second token from the Gecko
mobile-app client via the Options flow to unlock energy, charts, activities,
routines, and other premium REST endpoints.
"""

from __future__ import annotations

import base64
import json
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

# Try importing OAuth2TokenRequestError for HA 2026+
try:
    from homeassistant.helpers.config_entry_oauth2_flow import (
        OAuth2TokenRequestError,
    )
except ImportError:
    OAuth2TokenRequestError = None

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
    OAUTH2_CLIENT_ID,
    OAUTH2_TOKEN,
)
from .oauth_implementation import GeckoPKCEOAuth2Implementation

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _extract_code_from_callback(raw: str) -> str | None:
    """Extract the ``code`` query parameter from a pasted callback URL.

    Handles the full native-scheme URL, bare ``code=…`` fragments, and
    various copy-paste artifacts.
    """
    raw = raw.strip()
    if not raw:
        return None

    if raw.startswith("code=") or raw.startswith("?code="):
        raw = (
            f"https://placeholder/{raw}"
            if raw.startswith("?")
            else f"https://placeholder/?{raw}"
        )

    try:
        if "?" in raw:
            query_string = raw.split("?", 1)[1]
        else:
            parsed = urlparse(raw)
            query_string = parsed.query
        qs = parse_qs(query_string)
        codes = qs.get("code")
        if codes:
            return codes[0]
    except Exception:  # noqa: BLE001
        pass

    return None


# ---------------------------------------------------------------------------
# ConfigFlow — standard HA OAuth popup (community client)
# ---------------------------------------------------------------------------


class ConfigFlow(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Config flow to handle Gecko OAuth2 authentication."""

    DOMAIN = DOMAIN
    VERSION = 2

    async def async_step_user(self, user_input=None):
        """Handle a flow initialized by the user."""
        await self._async_ensure_implementation()
        return await super().async_step_user(user_input)

    async def _async_ensure_implementation(self) -> None:
        """Register the community-client OAuth implementation if needed."""
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

    async def async_oauth_create_entry(self, data: dict):
        """Create an entry after OAuth authentication."""
        try:
            from .api import ConfigFlowGeckoApi

            api_client = ConfigFlowGeckoApi(self.hass, data["token"]["access_token"])

            user_id, account_data, account_id = await self._resolve_user_and_account(
                data, api_client
            )

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
                        vessels_with_config.append(vessel)
                except Exception as config_err:
                    _LOGGER.warning(
                        "Failed to get spa config for vessel %s: %s",
                        vessel.get("name"),
                        config_err,
                    )
                    vessels_with_config.append(vessel)

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

        Primary path: Auth0 userinfo -> /v2/user/{userId}.
        Fallback: decode the JWT access token for org_id / account claims
        when the user-profile endpoint returns 404 (common for newer or
        social-login accounts that haven't been fully provisioned yet).
        """
        user_id: str | None = None
        try:
            user_id = await api_client.async_get_user_id()
        except Exception as err:
            _LOGGER.warning("Could not fetch Auth0 userinfo: %s", err)

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


# ---------------------------------------------------------------------------
# OptionsFlow — settings + optional energy data link
# ---------------------------------------------------------------------------


class GeckoOptionsFlow(config_entries.OptionsFlow):
    """Integration options (REST poll settings and optional energy-data link)."""

    def __init__(self) -> None:
        """Initialize the options flow."""
        super().__init__()
        self._code_verifier: str | None = None
        self._authorize_url: str | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Show a menu with settings and energy link actions."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["settings", "link_energy", "unlink_energy"],
        )

    async def async_step_settings(self, user_input: dict[str, Any] | None = None):
        """Configure optional cloud REST polling."""
        opts = dict(self.config_entry.options)

        if user_input is not None:
            merged_options = dict(self.config_entry.options)
            merged_options.update(user_input)

            old_alerts = int(
                opts.get(CONF_ALERTS_POLL_INTERVAL, DEFAULT_ALERTS_POLL_INTERVAL)
            )
            new_alerts = int(
                user_input.get(CONF_ALERTS_POLL_INTERVAL, DEFAULT_ALERTS_POLL_INTERVAL)
            )
            if (old_alerts > 0) != (new_alerts > 0):
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
        return self.async_show_form(step_id="settings", data_schema=schema)

    # -- Energy link (app-client paste flow) --------------------------------

    async def async_step_link_energy(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Link a Gecko mobile-app token for energy/premium data."""
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
                    expires_in = int(token.get("expires_in", 3600))
                    token["expires_in"] = expires_in
                    token["expires_at"] = time.time() + expires_in
                    data = {**self.config_entry.data, "app_token": token}
                    self.hass.config_entries.async_update_entry(
                        self.config_entry, data=data
                    )
                    await self.hass.config_entries.async_reload(
                        self.config_entry.entry_id
                    )
                    return self.async_abort(reason="energy_linked")

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
            step_id="link_energy",
            data_schema=vol.Schema({vol.Required("callback_url"): str}),
            description_placeholders={"authorize_url": self._authorize_url},
            errors=errors,
        )

    async def _async_exchange_code(self, code: str) -> dict | None:
        """Exchange an authorization code for app-client tokens."""
        if self._code_verifier is None:
            return None
        impl = GeckoPKCEOAuth2Implementation(
            self.hass,
            DOMAIN,
            client_id=OAUTH2_APP_CLIENT_ID,
            authorize_url=OAUTH2_AUTHORIZE,
            token_url=OAUTH2_TOKEN,
        )

        # Build exception tuple based on what's available
        exceptions = (ClientResponseError, ClientError)
        if OAuth2TokenRequestError is not None:
            exceptions = (ClientResponseError, ClientError, OAuth2TokenRequestError)

        try:
            return await impl.async_exchange_authorization_code(
                code=code,
                redirect_uri=OAUTH2_APP_REDIRECT_URI,
                code_verifier=self._code_verifier,
            )
        except exceptions as err:
            _LOGGER.error("App-client token exchange failed: %s", err)
            return None

    # -- Energy unlink ------------------------------------------------------

    async def async_step_unlink_energy(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Remove the linked app-client token."""
        if not self.config_entry.data.get("app_token"):
            return self.async_abort(reason="energy_not_linked")

        if user_input is not None:
            data = {k: v for k, v in self.config_entry.data.items() if k != "app_token"}
            self.hass.config_entries.async_update_entry(self.config_entry, data=data)
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_abort(reason="energy_unlinked")

        return self.async_show_form(
            step_id="unlink_energy",
            data_schema=vol.Schema({}),
        )
