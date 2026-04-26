# Changelog

All notable changes to **Gecko Full Community Integration for Home Assistant** are documented in this file.

The format is inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Version numbers follow the integration manifest in `custom_components/gecko/manifest.json`.

---

## [2.4.0] — 2026-04-26

### Breaking changes

#### Gecko Cloud REST and premium energy — default poll intervals

To reduce load on Gecko cloud APIs and avoid account throttling or bans, **default polling intervals for optional HTTP features are now much slower**. **Live spa state is unchanged**: it still comes from **MQTT / the device shadow** on the same coordinator schedule as before (~30 seconds when entities are subscribed).

| Option | Previous default | New default | What still updates quickly |
|--------|------------------|-------------|-----------------------------|
| **Cloud REST poll interval** | 300 seconds (5 minutes) | **86 400 seconds (24 hours)** | Temperatures, pumps, lights, shadow sensors, climate — all **MQTT** |
| **Premium energy poll interval** (when energy data is linked) | 3 600 seconds (1 hour) | **86 400 seconds (24 hours)** | Not applicable — energy is **REST-only**; values refresh on each successful poll |

**What this means for you**

- **Automations or dashboards that relied on REST-only chemistry or tile fields updating every few minutes** will see those fields update **at most once per day** unless you change the option.
- **Anything driven by MQTT** (typical spa operation, climate current temp when sane, pumps, etc.) behaves as before.
- **New installs** (no saved integration options) use the new defaults from the manifest.
- **Existing saved options** keep whatever **Cloud REST poll interval** and **Energy poll interval** you already stored (for example **300** seconds stays **300**). To adopt the gentler **daily** schedule, open **Settings → Devices & services → Gecko → Configure** and set **86 400** (or any value up to **86 400**; **0** disables REST tile polling).
- **Very old configs** that still have **0** (“disabled”) for cloud REST from pre‑v2.2 flows are updated once by `_migrate_options_defaults` to the current default (see `custom_components/gecko/__init__.py`).

**Alerts REST polling** is unchanged: default remains **0** (disabled) until you enable it.

---

### Added

- **`spa_configuration_summary`** in **Download diagnostics**: compact pump / waterfall / blower / light **ID → zone ID** maps from link-time REST `spa_configuration`, to compare hardware layout with shadow paths.
- **Issue reporting** guidance in the README and GitHub bug template: diagnostics, versions, debug logs, optional `scripts/verify_shadow_live.py`.
- **Tests** for `spa_configuration_summary` selection when two vessels share a `vesselId` but differ by `monitorId`.

### Changed

- **Water temperature reporting (current / measured only):** new `temperature_sanity.coerce_spa_water_temperature_c()` drops **0**, **non-numeric**, **out-of-range (outside ~4–45 °C)**, and **non-finite** values so climate and spa temperature sensors show **unknown** instead of bogus numbers (helps Grafana and long-term statistics).
- **Premium heating binary sensors** now declare **`device_class: heat`** where appropriate.
- **`spa_config_summary`:** flow-zone iteration no longer stops early when only the pump map hits its cap — waterfall and blower maps can fill up to their own limits.
- **Reconnect / token refresh:** safer disposal of short-lived Gecko clients when handler setup fails; **reconnect** only clears the live client **after** disconnect has started, so a **refresh callback error before disconnect** does not tear down a still-healthy MQTT session. On failure paths, the client is **detached under `_connection_lock`** and **executor disconnect** runs **after** the lock is released, avoiding long lock holds during slow MQTT teardown (review feedback).

### Fixed

- **NaN / infinity** no longer pass through water temperature coercion (would otherwise confuse comparisons and statistics).
- **Ruff import order** in `sensor.py` after adding temperature coercion import.
- **Diagnostics tests** updated for entries that include `data["vessels"]` and `spa_configuration_summary`.

---

## Earlier versions

Releases **before 2.4.0** did not ship a root-level changelog in this repository; use [GitHub Releases](https://github.com/markus-lassfolk/hass_gecko_in_touch3_spa/releases) and commit history for older notes.
