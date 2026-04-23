"""Entity registry helpers for premium energy sensors."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntryDisabler


def reenable_integration_disabled_energy_cost_score_entities(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Turn on energy cost/score sensors that were integration-disabled by default.

    Older releases registered these with ``entity_registry_enabled_default=False``.
    Re-enable only ``disabled_by=INTEGRATION`` so users who turned them off stay off.
    """
    if not entry.data.get("app_token"):
        return
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.domain != "sensor":
            continue
        uid = reg_entry.unique_id or ""
        if not (uid.endswith("_energy_cost") or uid.endswith("_energy_score")):
            continue
        if reg_entry.disabled_by != RegistryEntryDisabler.INTEGRATION:
            continue
        registry.async_update_entity(reg_entry.entity_id, disabled_by=None)
