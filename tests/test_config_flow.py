"""Tests for the Gecko config flow and options flow.

These tests exercise the flows under the HA data-entry-flow harness so that
method-resolution bugs (like calling ``async_update_reload_and_abort`` on an
``OptionsFlowWithConfigEntry``) are caught at test time instead of in production.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientResponseError
from custom_components.gecko.config_flow import (
    AccountResolutionError,
    ConfigFlow,
    GeckoOptionsFlow,
    _decode_jwt_payload,
    _extract_code_from_callback,
)
from custom_components.gecko.const import (
    CONF_ALERTS_POLL_INTERVAL,
    CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
    CONF_CLOUD_REST_POLL_INTERVAL,
    CONF_ENERGY_POLL_INTERVAL,
    DEFAULT_ALERTS_POLL_INTERVAL,
    DEFAULT_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
    DEFAULT_CLOUD_REST_POLL_INTERVAL,
    DEFAULT_ENERGY_POLL_INTERVAL,
    DOMAIN,
)
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_config_entry(
    *, options: dict | None = None, **data_overrides
) -> MockConfigEntry:
    """Create a MockConfigEntry pre-loaded with typical Gecko data."""
    data = {
        "token": {"access_token": "fake"},
        "vessels": [],
        "account_id": "acct-1",
        "user_id": "uid-1",
    }
    data.update(data_overrides)
    return MockConfigEntry(
        domain=DOMAIN,
        data=data,
        options=options or {},
        version=2,
    )


def _default_user_input(**overrides) -> dict:
    """Build a valid options-form user_input dict with defaults."""
    base = {
        CONF_CLOUD_REST_POLL_INTERVAL: DEFAULT_CLOUD_REST_POLL_INTERVAL,
        CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN: DEFAULT_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
        CONF_ALERTS_POLL_INTERVAL: DEFAULT_ALERTS_POLL_INTERVAL,
        CONF_ENERGY_POLL_INTERVAL: DEFAULT_ENERGY_POLL_INTERVAL,
    }
    base.update(overrides)
    return base


def _create_options_flow(
    hass: HomeAssistant, entry: MockConfigEntry
) -> GeckoOptionsFlow:
    """Instantiate and wire up a ``GeckoOptionsFlow`` for testing."""
    entry.add_to_hass(hass)
    flow = GeckoOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    return flow


# ---------------------------------------------------------------------------
# Options flow — menu
# ---------------------------------------------------------------------------


async def test_options_flow_shows_menu(hass: HomeAssistant) -> None:
    """Opening options must show the init menu."""
    entry = _mock_config_entry()
    flow = _create_options_flow(hass, entry)

    result = await flow.async_step_init(user_input=None)

    assert result["type"] is FlowResultType.MENU
    assert "settings" in result["menu_options"]
    assert "link_energy" in result["menu_options"]
    assert "unlink_energy" in result["menu_options"]


# ---------------------------------------------------------------------------
# Options flow — settings form rendering
# ---------------------------------------------------------------------------


async def test_options_flow_settings_shows_form(hass: HomeAssistant) -> None:
    """Selecting settings from the menu must show the settings form."""
    entry = _mock_config_entry()
    flow = _create_options_flow(hass, entry)

    result = await flow.async_step_settings(user_input=None)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"
    schema_keys = {str(k) for k in result["data_schema"].schema}
    assert CONF_CLOUD_REST_POLL_INTERVAL in schema_keys
    assert CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN in schema_keys
    assert CONF_ALERTS_POLL_INTERVAL in schema_keys
    assert CONF_ENERGY_POLL_INTERVAL in schema_keys


async def test_options_flow_form_defaults_from_existing_options(
    hass: HomeAssistant,
) -> None:
    """The form should pre-fill with values from the existing entry options."""
    entry = _mock_config_entry(
        options={
            CONF_CLOUD_REST_POLL_INTERVAL: 120,
            CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN: False,
            CONF_ALERTS_POLL_INTERVAL: 300,
        }
    )
    flow = _create_options_flow(hass, entry)

    result = await flow.async_step_settings(user_input=None)
    assert result["type"] is FlowResultType.FORM


# ---------------------------------------------------------------------------
# Options flow — normal save (no reload needed)
# ---------------------------------------------------------------------------


async def test_options_flow_save_without_reload(hass: HomeAssistant) -> None:
    """When alerts_poll_interval stays at zero, no reload should be triggered."""
    entry = _mock_config_entry(
        options={
            CONF_CLOUD_REST_POLL_INTERVAL: 60,
            CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN: True,
            CONF_ALERTS_POLL_INTERVAL: 0,
        }
    )
    flow = _create_options_flow(hass, entry)

    result = await flow.async_step_settings(
        user_input={
            CONF_CLOUD_REST_POLL_INTERVAL: 120,
            CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN: False,
            CONF_ALERTS_POLL_INTERVAL: 0,
            CONF_ENERGY_POLL_INTERVAL: DEFAULT_ENERGY_POLL_INTERVAL,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CLOUD_REST_POLL_INTERVAL] == 120
    assert result["data"][CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN] is False
    assert result["data"][CONF_ALERTS_POLL_INTERVAL] == 0
    assert result["data"][CONF_ENERGY_POLL_INTERVAL] == DEFAULT_ENERGY_POLL_INTERVAL


async def test_options_flow_preserves_internal_migration_marker(
    hass: HomeAssistant,
) -> None:
    """Saving options must keep ``_options_defaults_migrated`` so setup does not re-run migration."""
    entry = _mock_config_entry(
        options={
            CONF_CLOUD_REST_POLL_INTERVAL: 0,
            CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN: True,
            CONF_ALERTS_POLL_INTERVAL: 0,
            "_options_defaults_migrated": True,
        }
    )
    flow = _create_options_flow(hass, entry)

    result = await flow.async_step_settings(
        user_input={
            CONF_CLOUD_REST_POLL_INTERVAL: 120,
            CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN: False,
            CONF_ALERTS_POLL_INTERVAL: 0,
            CONF_ENERGY_POLL_INTERVAL: DEFAULT_ENERGY_POLL_INTERVAL,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"].get("_options_defaults_migrated") is True
    assert result["data"][CONF_CLOUD_REST_POLL_INTERVAL] == 120


async def test_options_flow_save_nonzero_to_nonzero_no_reload(
    hass: HomeAssistant,
) -> None:
    """Changing alerts 300->600 (both nonzero) must NOT reload."""
    entry = _mock_config_entry(options={CONF_ALERTS_POLL_INTERVAL: 300})
    flow = _create_options_flow(hass, entry)

    result = await flow.async_step_settings(
        user_input=_default_user_input(**{CONF_ALERTS_POLL_INTERVAL: 600}),
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ALERTS_POLL_INTERVAL] == 600


# ---------------------------------------------------------------------------
# Options flow — reload path (alerts crosses zero boundary)
# ---------------------------------------------------------------------------


async def test_options_flow_reload_when_enabling_alerts(
    hass: HomeAssistant,
) -> None:
    """Enabling alerts (0 -> 300) must update entry, reload, then abort.

    This is the exact code path that previously raised ``AttributeError``
    because ``async_update_reload_and_abort`` was called on an OptionsFlow.
    """
    entry = _mock_config_entry(options={CONF_ALERTS_POLL_INTERVAL: 0})
    flow = _create_options_flow(hass, entry)

    with patch.object(
        hass.config_entries, "async_reload", new_callable=AsyncMock
    ) as mock_reload:
        result = await flow.async_step_settings(
            user_input=_default_user_input(**{CONF_ALERTS_POLL_INTERVAL: 300}),
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    mock_reload.assert_awaited_once_with(entry.entry_id)
    assert entry.options[CONF_ALERTS_POLL_INTERVAL] == 300


async def test_options_flow_reload_preserves_internal_migration_marker(
    hass: HomeAssistant,
) -> None:
    """Reload path (alerts crossing zero) must still persist internal options keys."""
    entry = _mock_config_entry(
        options={
            CONF_ALERTS_POLL_INTERVAL: 0,
            "_options_defaults_migrated": True,
        }
    )
    flow = _create_options_flow(hass, entry)

    with patch.object(hass.config_entries, "async_reload", new_callable=AsyncMock):
        result = await flow.async_step_settings(
            user_input=_default_user_input(**{CONF_ALERTS_POLL_INTERVAL: 300}),
        )

    assert result["type"] is FlowResultType.ABORT
    assert entry.options.get("_options_defaults_migrated") is True
    assert entry.options[CONF_ALERTS_POLL_INTERVAL] == 300


async def test_options_flow_reload_when_disabling_alerts(
    hass: HomeAssistant,
) -> None:
    """Disabling alerts (300 -> 0) must trigger the reload-and-abort path."""
    entry = _mock_config_entry(options={CONF_ALERTS_POLL_INTERVAL: 300})
    flow = _create_options_flow(hass, entry)

    with patch.object(
        hass.config_entries, "async_reload", new_callable=AsyncMock
    ) as mock_reload:
        result = await flow.async_step_settings(
            user_input=_default_user_input(**{CONF_ALERTS_POLL_INTERVAL: 0}),
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    mock_reload.assert_awaited_once_with(entry.entry_id)
    assert entry.options[CONF_ALERTS_POLL_INTERVAL] == 0


async def test_options_flow_no_reload_when_alerts_stays_off(
    hass: HomeAssistant,
) -> None:
    """Changing from 0 -> 0 (alerts off both times): no reload."""
    entry = _mock_config_entry(options={CONF_ALERTS_POLL_INTERVAL: 0})
    flow = _create_options_flow(hass, entry)

    result = await flow.async_step_settings(
        user_input=_default_user_input(**{CONF_ALERTS_POLL_INTERVAL: 0}),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


# ---------------------------------------------------------------------------
# Options flow — regression guard: no ConfigFlow-only methods
# ---------------------------------------------------------------------------


async def test_options_flow_does_not_call_update_reload_and_abort() -> None:
    """GeckoOptionsFlow must NOT call async_update_reload_and_abort.

    That method only exists on ConfigFlow, not OptionsFlow.
    This is the direct regression test for the original production bug.
    """
    assert not hasattr(GeckoOptionsFlow, "async_update_reload_and_abort"), (
        "GeckoOptionsFlow should not define async_update_reload_and_abort"
    )

    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(GeckoOptionsFlow))
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != "async_update_reload_and_abort", (
                f"GeckoOptionsFlow calls async_update_reload_and_abort at line {node.lineno}; "
                "that method only exists on ConfigFlow"
            )


# ---------------------------------------------------------------------------
# Options flow — energy link / unlink
# ---------------------------------------------------------------------------


async def test_options_flow_link_energy_shows_form(hass: HomeAssistant) -> None:
    """link_energy step must show a form with the authorize URL."""
    entry = _mock_config_entry()
    flow = _create_options_flow(hass, entry)

    result = await flow.async_step_link_energy(user_input=None)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "link_energy"
    assert "authorize_url" in result["description_placeholders"]
    assert (
        "gecko-prod.us.auth0.com" in result["description_placeholders"]["authorize_url"]
    )


async def test_options_flow_link_energy_rejects_invalid_url(
    hass: HomeAssistant,
) -> None:
    """link_energy step must reject input without a code parameter."""
    entry = _mock_config_entry()
    flow = _create_options_flow(hass, entry)

    await flow.async_step_link_energy(user_input=None)
    result = await flow.async_step_link_energy(
        user_input={"callback_url": "not-a-valid-url"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["callback_url"] == "invalid_callback_url"


async def test_options_flow_unlink_energy_aborts_when_not_linked(
    hass: HomeAssistant,
) -> None:
    """unlink_energy must abort immediately if no app_token exists."""
    entry = _mock_config_entry()
    flow = _create_options_flow(hass, entry)

    result = await flow.async_step_unlink_energy(user_input=None)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "energy_not_linked"


async def test_options_flow_unlink_energy_shows_confirm(hass: HomeAssistant) -> None:
    """unlink_energy must show a confirmation form when app_token exists."""
    entry = _mock_config_entry(
        app_token={"access_token": "app-fake", "refresh_token": "r", "expires_at": 0}
    )
    flow = _create_options_flow(hass, entry)

    result = await flow.async_step_unlink_energy(user_input=None)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "unlink_energy"


async def test_options_flow_unlink_energy_removes_token(hass: HomeAssistant) -> None:
    """Confirming unlink must remove app_token and reload."""
    entry = _mock_config_entry(
        app_token={"access_token": "app-fake", "refresh_token": "r", "expires_at": 0}
    )
    flow = _create_options_flow(hass, entry)

    with patch.object(
        hass.config_entries, "async_reload", new_callable=AsyncMock
    ) as mock_reload:
        result = await flow.async_step_unlink_energy(
            user_input={"confirm_unlink": True}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "energy_unlinked"
    assert "app_token" not in entry.data
    mock_reload.assert_awaited_once_with(entry.entry_id)


async def test_options_flow_unlink_energy_requires_checkbox(
    hass: HomeAssistant,
) -> None:
    """Submitting unlink without confirming must show an error, not remove the token."""
    entry = _mock_config_entry(
        app_token={"access_token": "app-fake", "refresh_token": "r", "expires_at": 0}
    )
    flow = _create_options_flow(hass, entry)

    await flow.async_step_unlink_energy(user_input=None)
    result = await flow.async_step_unlink_energy(user_input={"confirm_unlink": False})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "must_confirm_unlink"
    assert "app_token" in entry.data


# ---------------------------------------------------------------------------
# ConfigFlow — basic plumbing
# ---------------------------------------------------------------------------


def test_config_flow_get_options_flow_returns_gecko_options() -> None:
    """``async_get_options_flow`` must return a ``GeckoOptionsFlow``."""
    entry = _mock_config_entry()
    flow = ConfigFlow.async_get_options_flow(entry)
    assert isinstance(flow, GeckoOptionsFlow)


def test_config_flow_domain_and_version() -> None:
    """Verify the ConfigFlow meta-class properties."""
    assert ConfigFlow.DOMAIN == DOMAIN
    assert ConfigFlow.VERSION == 2


def test_options_flow_inherits_from_options_flow() -> None:
    """GeckoOptionsFlow must subclass OptionsFlow, not the deprecated
    OptionsFlowWithConfigEntry or ConfigFlow."""
    assert issubclass(GeckoOptionsFlow, config_entries.OptionsFlow)
    assert not issubclass(GeckoOptionsFlow, config_entries.ConfigFlow)


# ---------------------------------------------------------------------------
# _extract_code_from_callback — URL parsing
# ---------------------------------------------------------------------------

_NATIVE_CALLBACK = (
    "com.geckoportal.gecko://gecko-prod.us.auth0.com"
    "/capacitor/com.geckoportal.gecko/callback"
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (f"{_NATIVE_CALLBACK}?code=abc123&state=xyz", "abc123"),
        (f"{_NATIVE_CALLBACK}?code=abc123", "abc123"),
        ("  " + f"{_NATIVE_CALLBACK}?code=abc123&state=xyz  ", "abc123"),
        ("code=mycode123", "mycode123"),
        ("?code=mycode123&state=s", "mycode123"),
        ("https://example.com/cb?code=httpsCode&state=s", "httpsCode"),
        ("", None),
        ("   ", None),
        ("no-code-here", None),
        (f"{_NATIVE_CALLBACK}?state=xyz", None),
    ],
    ids=[
        "full_native_url",
        "native_no_state",
        "whitespace_padded",
        "bare_code_param",
        "bare_query_string",
        "https_url",
        "empty",
        "whitespace_only",
        "no_code_param",
        "native_url_missing_code",
    ],
)
def test_extract_code_from_callback(raw: str, expected: str | None) -> None:
    """_extract_code_from_callback must handle various paste formats."""
    assert _extract_code_from_callback(raw) == expected


# ---------------------------------------------------------------------------
# JWT fallback — account resolution
# ---------------------------------------------------------------------------


def _oauth_data_with_jwt_payload(claims: dict) -> dict:
    """Build minimal OAuth ``data`` with a synthetic JWT access token."""
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    token = f"hdr.{body}.sig"
    return {"token": {"access_token": token}}


def test_decode_jwt_payload_rejects_non_dict_json() -> None:
    """JSON payloads that are not objects must not be treated as claims."""
    body = base64.urlsafe_b64encode(json.dumps(["x"]).encode()).decode().rstrip("=")
    assert _decode_jwt_payload(f"h.{body}.s") is None


async def test_resolve_user_jwt_fallback_after_user_profile_404(
    hass: HomeAssistant,
) -> None:
    """404 on /v2/user must fall back to JWT claims when sub and account exist."""
    flow = ConfigFlow()
    flow.hass = hass
    api = AsyncMock()
    api.async_get_user_id = AsyncMock(return_value="auth0|abc")
    api.async_get_user_info = AsyncMock(
        side_effect=ClientResponseError(MagicMock(), (), status=404)
    )
    data = _oauth_data_with_jwt_payload({"sub": "auth0|abc", "org_id": "org-from-jwt"})

    user_id, account_data, account_id = await flow._resolve_user_and_account(data, api)

    assert user_id == "auth0|abc"
    assert account_data == {"name": "Account"}
    assert account_id == "org-from-jwt"


async def test_resolve_user_jwt_fallback_requires_sub_when_no_user_id(
    hass: HomeAssistant,
) -> None:
    """JWT fallback must not return without a usable subject when userinfo failed."""
    flow = ConfigFlow()
    flow.hass = hass
    api = AsyncMock()
    api.async_get_user_id = AsyncMock(return_value=None)
    data = _oauth_data_with_jwt_payload({"org_id": "only-org"})

    with pytest.raises(AccountResolutionError):
        await flow._resolve_user_and_account(data, api)


async def test_oauth_create_entry_aborts_on_account_resolution_failure(
    hass: HomeAssistant,
) -> None:
    """ConnectionError from account resolution must surface a dedicated abort reason."""
    flow = ConfigFlow()
    flow.hass = hass
    data = {"token": {"access_token": "x"}}
    with (
        patch(
            "custom_components.gecko.api.ConfigFlowGeckoApi",
            return_value=MagicMock(),
        ),
        patch.object(
            flow,
            "_resolve_user_and_account",
            AsyncMock(side_effect=AccountResolutionError("No account.")),
        ),
    ):
        result = await flow.async_oauth_create_entry(data)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "account_resolution_failed"
