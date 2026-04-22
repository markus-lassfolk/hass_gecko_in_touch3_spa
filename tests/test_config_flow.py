"""Tests for the Gecko config flow and options flow.

These tests exercise the flows under the HA data-entry-flow harness so that
method-resolution bugs (like calling ``async_update_reload_and_abort`` on an
``OptionsFlowWithConfigEntry``) are caught at test time instead of in production.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from custom_components.gecko.config_flow import ConfigFlow, GeckoOptionsFlow
from custom_components.gecko.const import (
    CONF_ALERTS_POLL_INTERVAL,
    CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
    CONF_CLOUD_REST_POLL_INTERVAL,
    DEFAULT_ALERTS_POLL_INTERVAL,
    DEFAULT_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
    DEFAULT_CLOUD_REST_POLL_INTERVAL,
    DOMAIN,
)
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_config_entry(*, options: dict | None = None) -> MockConfigEntry:
    """Create a MockConfigEntry pre-loaded with typical Gecko data."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "token": {"access_token": "fake"},
            "vessels": [],
            "account_id": "acct-1",
            "user_id": "uid-1",
        },
        options=options or {},
        version=2,
    )


def _default_user_input(**overrides) -> dict:
    """Build a valid options-form user_input dict with defaults."""
    base = {
        CONF_CLOUD_REST_POLL_INTERVAL: DEFAULT_CLOUD_REST_POLL_INTERVAL,
        CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN: DEFAULT_CLOUD_REST_ONLY_WHEN_MQTT_DOWN,
        CONF_ALERTS_POLL_INTERVAL: DEFAULT_ALERTS_POLL_INTERVAL,
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
# Options flow — form rendering
# ---------------------------------------------------------------------------


async def test_options_flow_shows_form(hass: HomeAssistant) -> None:
    """Opening options without input must show the init form."""
    entry = _mock_config_entry()
    flow = _create_options_flow(hass, entry)

    result = await flow.async_step_init(user_input=None)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    schema_keys = {str(k) for k in result["data_schema"].schema}
    assert CONF_CLOUD_REST_POLL_INTERVAL in schema_keys
    assert CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN in schema_keys
    assert CONF_ALERTS_POLL_INTERVAL in schema_keys


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

    result = await flow.async_step_init(user_input=None)
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

    result = await flow.async_step_init(
        user_input={
            CONF_CLOUD_REST_POLL_INTERVAL: 120,
            CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN: False,
            CONF_ALERTS_POLL_INTERVAL: 0,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CLOUD_REST_POLL_INTERVAL] == 120
    assert result["data"][CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN] is False
    assert result["data"][CONF_ALERTS_POLL_INTERVAL] == 0


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

    result = await flow.async_step_init(
        user_input={
            CONF_CLOUD_REST_POLL_INTERVAL: 120,
            CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN: False,
            CONF_ALERTS_POLL_INTERVAL: 0,
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

    result = await flow.async_step_init(
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
        result = await flow.async_step_init(
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
        result = await flow.async_step_init(
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
        result = await flow.async_step_init(
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

    result = await flow.async_step_init(
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


def test_options_flow_no_custom_constructor() -> None:
    """Modern OptionsFlow should not override __init__ (HA 2025.1+)."""
    assert "__init__" not in GeckoOptionsFlow.__dict__, (
        "GeckoOptionsFlow should not define its own __init__. "
        "HA 2025.1+ passes config_entry via property, not constructor."
    )
