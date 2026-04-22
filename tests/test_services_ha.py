"""Home Assistant–scoped tests for Gecko services."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from custom_components.gecko import services as gecko_services
from custom_components.gecko.const import DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.fixture(autouse=True)
async def cleanup_gecko_services(hass: HomeAssistant) -> None:
    """Ensure Gecko services do not leak into other tests."""
    yield
    await gecko_services.async_remove_services(hass)


async def test_async_setup_and_remove_services(hass: HomeAssistant) -> None:
    await gecko_services.async_setup_services(hass)
    for name in (
        gecko_services.SERVICE_PUBLISH_ZONE_DESIRED,
        gecko_services.SERVICE_PUBLISH_FEATURE_DESIRED,
        gecko_services.SERVICE_PUBLISH_DESIRED_STATE,
        gecko_services.SERVICE_DUMP_SHADOW_SNAPSHOT,
    ):
        assert hass.services.has_service(DOMAIN, name)

    await gecko_services.async_remove_services(hass)
    for name in (
        gecko_services.SERVICE_PUBLISH_ZONE_DESIRED,
        gecko_services.SERVICE_PUBLISH_FEATURE_DESIRED,
        gecko_services.SERVICE_PUBLISH_DESIRED_STATE,
        gecko_services.SERVICE_DUMP_SHADOW_SNAPSHOT,
    ):
        assert not hass.services.has_service(DOMAIN, name)


async def test_async_setup_services_idempotent(hass: HomeAssistant) -> None:
    await gecko_services.async_setup_services(hass)
    await gecko_services.async_setup_services(hass)
    assert hass.services.has_service(
        DOMAIN, gecko_services.SERVICE_PUBLISH_ZONE_DESIRED
    )


async def test_bind_service_handler_passes_hass(hass: HomeAssistant) -> None:
    captured: dict[str, object] = {}

    async def handler(h: HomeAssistant, call: ServiceCall) -> None:
        captured["h"] = h
        captured["call"] = call

    wrapped = gecko_services._bind_service_handler(hass, handler)
    call = ServiceCall(hass, DOMAIN, "test", {})
    await wrapped(call)
    assert captured["h"] is hass
    assert captured["call"] is call


def _gecko_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Spa",
        data={
            "vessels": [
                {"monitorId": "m1", "vesselId": "v1"},
            ]
        },
        state=ConfigEntryState.LOADED,
    )
    entry.add_to_hass(hass)
    return entry


async def test_validate_config_entry_rejects_bad_monitor(hass: HomeAssistant) -> None:
    entry = _gecko_entry(hass)
    call = ServiceCall(
        hass,
        DOMAIN,
        "test",
        {
            gecko_services.ATTR_CONFIG_ENTRY_ID: entry.entry_id,
            gecko_services.ATTR_MONITOR_ID: "not-m1",
        },
    )
    with pytest.raises(HomeAssistantError, match="not part"):
        gecko_services._validate_config_entry(hass, call)


async def test_validate_config_entry_accepts_known_monitor(
    hass: HomeAssistant,
) -> None:
    entry = _gecko_entry(hass)
    call = ServiceCall(
        hass,
        DOMAIN,
        "test",
        {
            gecko_services.ATTR_CONFIG_ENTRY_ID: entry.entry_id,
            gecko_services.ATTR_MONITOR_ID: "m1",
        },
    )
    gecko_services._validate_config_entry(hass, call)


async def test_async_handle_publish_zone_desired_builds_payload(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _gecko_entry(hass)
    published: list[dict] = []

    def publish(desired: dict) -> None:
        published.append(desired)

    client = SimpleNamespace(transporter=SimpleNamespace(publish_desired_state=publish))

    async def _fake_client(_h: HomeAssistant, _c: ServiceCall):
        return client

    monkeypatch.setattr(
        gecko_services,
        "_async_client_for_monitor_from_call",
        _fake_client,
    )

    call = ServiceCall(
        hass,
        DOMAIN,
        gecko_services.SERVICE_PUBLISH_ZONE_DESIRED,
        {
            gecko_services.ATTR_CONFIG_ENTRY_ID: entry.entry_id,
            gecko_services.ATTR_MONITOR_ID: "m1",
            gecko_services.ATTR_ZONE_TYPE: "watercare",
            gecko_services.ATTR_ZONE_ID: "z1",
            gecko_services.ATTR_UPDATES: {"on": True},
        },
    )
    await gecko_services.async_handle_publish_zone_desired(hass, call)
    assert published == [{"zones": {"watercare": {"z1": {"on": True}}}}]


async def test_async_handle_publish_feature_desired(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _gecko_entry(hass)
    published: list[dict] = []

    client = SimpleNamespace(
        transporter=SimpleNamespace(publish_desired_state=lambda d: published.append(d))
    )

    async def _fake_client(_h: HomeAssistant, _c: ServiceCall):
        return client

    monkeypatch.setattr(
        gecko_services,
        "_async_client_for_monitor_from_call",
        _fake_client,
    )
    call = ServiceCall(
        hass,
        DOMAIN,
        gecko_services.SERVICE_PUBLISH_FEATURE_DESIRED,
        {
            gecko_services.ATTR_CONFIG_ENTRY_ID: entry.entry_id,
            gecko_services.ATTR_MONITOR_ID: "m1",
            gecko_services.ATTR_UPDATES: {"waterlab": {"x": 1}},
        },
    )
    await gecko_services.async_handle_publish_feature_desired(hass, call)
    assert published == [{"features": {"waterlab": {"x": 1}}}]


async def test_async_handle_publish_desired_state(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _gecko_entry(hass)
    published: list[dict] = []

    client = SimpleNamespace(
        transporter=SimpleNamespace(publish_desired_state=lambda d: published.append(d))
    )

    async def _fake_client(_h: HomeAssistant, _c: ServiceCall):
        return client

    monkeypatch.setattr(
        gecko_services,
        "_async_client_for_monitor_from_call",
        _fake_client,
    )
    fragment = {"zones": {"pump": {"run": True}}}
    call = ServiceCall(
        hass,
        DOMAIN,
        gecko_services.SERVICE_PUBLISH_DESIRED_STATE,
        {
            gecko_services.ATTR_CONFIG_ENTRY_ID: entry.entry_id,
            gecko_services.ATTR_MONITOR_ID: "m1",
            gecko_services.ATTR_DESIRED_FRAGMENT: fragment,
        },
    )
    await gecko_services.async_handle_publish_desired_state(hass, call)
    assert published == [fragment]


async def test_async_handle_dump_shadow_snapshot_writes_file(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    entry = _gecko_entry(hass)
    monkeypatch.setattr(hass.config, "config_dir", str(tmp_path))

    client = SimpleNamespace(_state={"state": {"reported": {}}})

    conn = SimpleNamespace(gecko_client=client, is_connected=True)

    class _Mgr:
        def get_connection(self, monitor_id: str):
            if monitor_id == "m1":
                return conn
            return None

    with (
        patch.object(
            gecko_services,
            "async_get_connection_manager",
            new_callable=AsyncMock,
            return_value=_Mgr(),
        ),
        patch(
            "custom_components.gecko.services.persistent_notification.async_create",
            MagicMock(),
        ),
    ):
        call = ServiceCall(
            hass,
            DOMAIN,
            gecko_services.SERVICE_DUMP_SHADOW_SNAPSHOT,
            {
                gecko_services.ATTR_CONFIG_ENTRY_ID: entry.entry_id,
                gecko_services.ATTR_MONITOR_ID: "m1",
                gecko_services.ATTR_INCLUDE_CONFIGURATION: False,
                gecko_services.ATTR_INCLUDE_DERIVED: False,
                gecko_services.ATTR_SANITIZE_FOR_PUBLIC_SHARE: True,
            },
        )
        await gecko_services.async_handle_dump_shadow_snapshot(hass, call)

    dump_dir = tmp_path / "gecko_shadow_dumps"
    assert dump_dir.is_dir()
    files = list(dump_dir.glob("*.json"))
    assert len(files) == 1
    assert "gecko" in files[0].read_text(encoding="utf-8").lower()


async def test_validate_config_entry_rejects_wrong_domain(
    hass: HomeAssistant,
) -> None:
    other = MockConfigEntry(domain="not_gecko", data={}, state=ConfigEntryState.LOADED)
    other.add_to_hass(hass)
    call = ServiceCall(
        hass,
        DOMAIN,
        "test",
        {
            gecko_services.ATTR_CONFIG_ENTRY_ID: other.entry_id,
            gecko_services.ATTR_MONITOR_ID: "m1",
        },
    )
    with pytest.raises(HomeAssistantError, match="Invalid"):
        gecko_services._validate_config_entry(hass, call)


async def test_async_client_for_monitor_raises_when_disconnected(
    hass: HomeAssistant,
) -> None:
    class _Conn:
        is_connected = False
        gecko_client = object()

    class _Mgr:
        def get_connection(self, _mid: str):
            return _Conn()

    with patch.object(
        gecko_services,
        "async_get_connection_manager",
        new_callable=AsyncMock,
        return_value=_Mgr(),
    ):
        with pytest.raises(HomeAssistantError, match="No active Gecko MQTT"):
            await gecko_services._async_client_for_monitor(hass, "m1")


async def test_dump_shadow_raises_without_gecko_client(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    entry = _gecko_entry(hass)
    monkeypatch.setattr(hass.config, "config_dir", str(tmp_path))

    class _Conn:
        gecko_client = None

    class _Mgr:
        def get_connection(self, _mid: str):
            return _Conn()

    with patch.object(
        gecko_services,
        "async_get_connection_manager",
        new_callable=AsyncMock,
        return_value=_Mgr(),
    ):
        call = ServiceCall(
            hass,
            DOMAIN,
            gecko_services.SERVICE_DUMP_SHADOW_SNAPSHOT,
            {
                gecko_services.ATTR_CONFIG_ENTRY_ID: entry.entry_id,
                gecko_services.ATTR_MONITOR_ID: "m1",
            },
        )
        with pytest.raises(HomeAssistantError, match="No Gecko client"):
            await gecko_services.async_handle_dump_shadow_snapshot(hass, call)


async def test_dump_shadow_rejects_path_outside_dump_dir(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    entry = _gecko_entry(hass)
    monkeypatch.setattr(hass.config, "config_dir", str(tmp_path))
    conn = SimpleNamespace(
        gecko_client=SimpleNamespace(_state=None, transporter=None), is_connected=True
    )

    class _Mgr:
        def get_connection(self, _mid: str):
            return conn

    with (
        patch.object(
            gecko_services,
            "async_get_connection_manager",
            new_callable=AsyncMock,
            return_value=_Mgr(),
        ),
        patch.object(
            gecko_services,
            "safe_export_filename",
            return_value="../../outside.json",
        ),
    ):
        call = ServiceCall(
            hass,
            DOMAIN,
            gecko_services.SERVICE_DUMP_SHADOW_SNAPSHOT,
            {
                gecko_services.ATTR_CONFIG_ENTRY_ID: entry.entry_id,
                gecko_services.ATTR_MONITOR_ID: "m1",
            },
        )
        with pytest.raises(HomeAssistantError, match="Invalid export path"):
            await gecko_services.async_handle_dump_shadow_snapshot(hass, call)
