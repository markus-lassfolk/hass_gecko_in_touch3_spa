"""OAuth2 implementation for the Gecko integration.

This module provides a PKCE-based OAuth2 implementation with a hardcoded
public Client ID. PKCE (Proof Key for Code Exchange) uses cryptographic
code challenges instead of a static client secret, making it secure even
with a public Client ID.

No Application Credentials setup is required - the integration works out of the box!
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow


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
        client_secret: str = "",
        code_verifier_length: int = 128,
    ) -> None:
        """Initialize Gecko OAuth with PKCE."""
        super().__init__(
            hass, domain, client_id, client_secret, authorize_url, token_url
        )
        self._code_verifier_length = code_verifier_length
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
        return {
            "code_challenge": self.compute_code_challenge(self.code_verifier),
            "code_challenge_method": "S256",
            # offline_access is REQUIRED to receive a refresh_token from Auth0
            "scope": "openid profile email offline_access",
            "audience": "https://api.geckowatermonitor.com",
        }

    async def async_generate_authorize_url(self, flow_id: str) -> str:
        """Generate authorize URL with a fresh verifier for this attempt."""
        self.code_verifier = self.generate_code_verifier(self._code_verifier_length)
        return await super().async_generate_authorize_url(flow_id)

    async def async_resolve_external_data(self, external_data: Any) -> dict:
        """Exchange the authorization code for tokens (includes PKCE verifier)."""
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": external_data["code"],
                "redirect_uri": external_data["state"]["redirect_uri"],
                "code_verifier": self.code_verifier,
            }
        )
