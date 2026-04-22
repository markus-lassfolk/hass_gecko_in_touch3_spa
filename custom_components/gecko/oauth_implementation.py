"""OAuth2 implementation for the Gecko integration.

This module provides a PKCE-based OAuth2 implementation with a hardcoded
public Client ID. PKCE (Proof Key for Code Exchange) uses cryptographic
code challenges instead of a static client secret, making it secure even
with a public Client ID.

No Application Credentials setup is required - the integration works out of the box!
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import secrets
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

from .const import DOMAIN

# Verifiers keyed by OAuth config-flow id; survives the shared implementation instance.
_DATA_KEY_PKCE_VERIFIERS = f"{DOMAIN}_oauth_pkce_verifiers_by_flow"
_active_pkce_verifier: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "gecko_pkce_verifier", default=None
)


class GeckoPKCEOAuth2Implementation(config_entry_oauth2_flow.LocalOAuth2Implementation):
    """Gecko OAuth2 implementation with PKCE (no client secret required)."""

    def __init__(
        self,
        hass: HomeAssistant,
        domain: str,
        client_id: str,
        authorize_url: str,
        token_url: str,
        *,
        client_secret: str | None = None,
        code_verifier_length: int = 128,
    ) -> None:
        """Initialize Gecko OAuth with PKCE."""
        super().__init__(
            hass, domain, client_id, client_secret, authorize_url, token_url
        )
        self._code_verifier_length = code_verifier_length
        # Default verifier for property access outside an active authorize URL build.
        self.code_verifier = self.generate_code_verifier(code_verifier_length)

    @staticmethod
    def generate_code_verifier(code_verifier_length: int = 128) -> str:
        """Generate a PKCE code verifier (43–128 characters)."""
        if not 43 <= code_verifier_length <= 128:
            msg = (
                "Parameter `code_verifier_length` must validate "
                "`43 <= code_verifier_length <= 128`."
            )
            raise ValueError(msg)
        return secrets.token_urlsafe(96)[:code_verifier_length]

    @staticmethod
    def compute_code_challenge(code_verifier: str) -> str:
        """Compute the S256 code challenge for a verifier."""
        if not 43 <= len(code_verifier) <= 128:
            msg = (
                "Parameter `code_verifier` must validate "
                "`43 <= len(code_verifier) <= 128`."
            )
            raise ValueError(msg)
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    @property
    def extra_authorize_data(self) -> dict:
        """Extra data for the authorize URL."""
        verifier = _active_pkce_verifier.get() or self.code_verifier
        return {
            "code_challenge": self.compute_code_challenge(verifier),
            "code_challenge_method": "S256",
            # offline_access is REQUIRED to receive a refresh_token from Auth0
            "scope": "openid profile email offline_access",
            "audience": "https://api.geckowatermonitor.com",
        }

    async def async_generate_authorize_url(self, flow_id: str) -> str:
        """Generate authorize URL with a fresh verifier for this OAuth flow."""
        verifier = self.generate_code_verifier(self._code_verifier_length)
        self.hass.data.setdefault(_DATA_KEY_PKCE_VERIFIERS, {})[flow_id] = verifier
        token = _active_pkce_verifier.set(verifier)
        try:
            return await super().async_generate_authorize_url(flow_id)
        finally:
            _active_pkce_verifier.reset(token)

    async def async_resolve_external_data(self, external_data: Any) -> dict:
        """Exchange the authorization code for tokens (includes PKCE verifier)."""
        state = external_data.get("state") or {}
        flow_id = state.get("flow_id")
        verifier: str | None = None
        if flow_id:
            store = self.hass.data.get(_DATA_KEY_PKCE_VERIFIERS)
            if isinstance(store, dict):
                verifier = store.pop(flow_id, None)
        if verifier is None:
            verifier = self.code_verifier
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": external_data["code"],
                "redirect_uri": state["redirect_uri"],
                "code_verifier": verifier,
                "client_id": self.client_id,
            }
        )
