"""Tests for ``custom_components.gecko.oauth_implementation`` (PKCE helpers)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.gecko.const import DOMAIN
from custom_components.gecko.oauth_implementation import (
    _DATA_KEY_PKCE_VERIFIERS,
    GeckoPKCEOAuth2Implementation,
)


def _make_impl() -> GeckoPKCEOAuth2Implementation:
    hass = SimpleNamespace(data={})
    return GeckoPKCEOAuth2Implementation(
        hass,
        DOMAIN,
        "public-client-id",
        "https://example.com/authorize",
        "https://example.com/token",
    )


def test_generate_code_verifier_length_bounds() -> None:
    with pytest.raises(ValueError, match="43"):
        GeckoPKCEOAuth2Implementation.generate_code_verifier(42)
    with pytest.raises(ValueError, match="128"):
        GeckoPKCEOAuth2Implementation.generate_code_verifier(129)
    v = GeckoPKCEOAuth2Implementation.generate_code_verifier(64)
    assert len(v) == 64


def test_compute_code_challenge_length_bounds() -> None:
    with pytest.raises(ValueError, match="43"):
        GeckoPKCEOAuth2Implementation.compute_code_challenge("x" * 42)
    ch = GeckoPKCEOAuth2Implementation.compute_code_challenge("x" * 43)
    assert isinstance(ch, str)
    assert "=" not in ch


def test_compute_code_challenge_deterministic() -> None:
    v = "a" * 50
    a = GeckoPKCEOAuth2Implementation.compute_code_challenge(v)
    b = GeckoPKCEOAuth2Implementation.compute_code_challenge(v)
    assert a == b


def test_extra_authorize_data_contains_pkce_and_scopes() -> None:
    impl = _make_impl()
    extra = impl.extra_authorize_data
    assert extra["code_challenge_method"] == "S256"
    assert "code_challenge" in extra
    assert "offline_access" in extra["scope"]
    assert extra["audience"] == "https://api.geckowatermonitor.com"


async def test_async_resolve_external_data_pops_stored_verifier() -> None:
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
    assert flow_id not in (impl.hass.data.get(_DATA_KEY_PKCE_VERIFIERS) or {})
