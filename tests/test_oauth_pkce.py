"""Tests for ``custom_components.gecko.oauth_implementation`` (PKCE helpers)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientError
from custom_components.gecko.const import DOMAIN
from custom_components.gecko.oauth_implementation import (
    _DATA_KEY_PKCE_VERIFIERS,
    GeckoPKCEOAuth2Implementation,
)


def _make_impl() -> GeckoPKCEOAuth2Implementation:
    """Return a PKCE OAuth implementation backed by a minimal hass stub."""
    hass = SimpleNamespace(data={})
    return GeckoPKCEOAuth2Implementation(
        hass,
        DOMAIN,
        "public-client-id",
        "https://example.com/authorize",
        "https://example.com/token",
    )


def test_generate_code_verifier_length_bounds() -> None:
    """Verifier length must be between 43 and 128 inclusive."""
    with pytest.raises(ValueError, match="43"):
        GeckoPKCEOAuth2Implementation.generate_code_verifier(42)
    with pytest.raises(ValueError, match="128"):
        GeckoPKCEOAuth2Implementation.generate_code_verifier(129)
    v = GeckoPKCEOAuth2Implementation.generate_code_verifier(64)
    assert len(v) == 64


def test_compute_code_challenge_length_bounds() -> None:
    """Challenge input length must be between 43 and 128 inclusive."""
    with pytest.raises(ValueError, match="43"):
        GeckoPKCEOAuth2Implementation.compute_code_challenge("x" * 42)
    ch = GeckoPKCEOAuth2Implementation.compute_code_challenge("x" * 43)
    assert isinstance(ch, str)
    assert "=" not in ch


def test_compute_code_challenge_deterministic() -> None:
    """S256 challenge is stable for a fixed verifier."""
    v = "a" * 50
    a = GeckoPKCEOAuth2Implementation.compute_code_challenge(v)
    b = GeckoPKCEOAuth2Implementation.compute_code_challenge(v)
    assert a == b


def test_extra_authorize_data_contains_pkce_and_scopes() -> None:
    """Authorize payload includes PKCE, scopes, and API audience."""
    impl = _make_impl()
    extra = impl.extra_authorize_data
    assert extra["code_challenge_method"] == "S256"
    assert "code_challenge" in extra
    assert "offline_access" in extra["scope"]
    assert extra["audience"] == "https://api.geckowatermonitor.com"


async def test_async_resolve_external_data_pops_stored_verifier() -> None:
    """Token exchange uses the stored verifier and removes it from hass.data."""
    impl = _make_impl()
    flow_id = "flow-test-1"
    verifier = "b" * 43
    impl.hass.data[_DATA_KEY_PKCE_VERIFIERS] = {flow_id: verifier}

    with patch.object(
        impl,
        "_token_request",
        new_callable=AsyncMock,
        return_value={"access_token": "t"},
    ) as tr:
        result = await impl.async_resolve_external_data(
            {
                "code": "auth-code",
                "state": {
                    "flow_id": flow_id,
                    "redirect_uri": "https://hass/callback",
                },
            }
        )

    assert result == {"access_token": "t"}
    tr.assert_awaited_once()
    payload = tr.await_args.args[0]
    assert payload["code_verifier"] == verifier
    assert payload["client_id"] == "public-client-id"
    assert flow_id not in (impl.hass.data.get(_DATA_KEY_PKCE_VERIFIERS) or {})


async def test_async_resolve_external_data_requires_redirect_uri() -> None:
    """Missing redirect_uri in state raises ClientError."""
    impl = _make_impl()
    with pytest.raises(ClientError):
        await impl.async_resolve_external_data(
            {"code": "x", "state": {"flow_id": "f1"}}
        )


async def test_async_resolve_external_data_missing_stored_verifier() -> None:
    """When flow_id is set but no verifier is stored, token exchange fails."""
    impl = _make_impl()
    with pytest.raises(ClientError):
        await impl.async_resolve_external_data(
            {
                "code": "auth-code",
                "state": {
                    "flow_id": "missing-from-store",
                    "redirect_uri": "https://hass/callback",
                },
            }
        )
