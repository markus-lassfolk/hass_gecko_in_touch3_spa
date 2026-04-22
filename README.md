<div align="center">

# Gecko Full Community Integration for Home Assistant

**Community fork:** in.touch 3 / Gecko IoT in Home Assistant — MQTT-first spa control, optional Gecko Cloud REST enrichment, dynamic shadow entities, and contributor-friendly exports.

[![GitHub Release](https://img.shields.io/github/release/markus-lassfolk/hass_gecko_in_touch3_spa.svg?style=for-the-badge)](https://github.com/markus-lassfolk/hass_gecko_in_touch3_spa/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg?style=for-the-badge)](LICENSE)

</div>

---

## About this fork

This repository ([**markus-lassfolk/hass_gecko_in_touch3_spa**](https://github.com/markus-lassfolk/hass_gecko_in_touch3_spa)) extends the upstream [**geckoal/ha-gecko-integration**](https://github.com/geckoal/ha-gecko-integration) line and the [**gecko-iot-client**](https://github.com/geckoal/gecko-iot-client) library. It keeps the Home Assistant integration **`domain`: `gecko`** (folder `custom_components/gecko/`) so OAuth, devices, and services stay compatible with existing installs — the manifest **display name** is **Gecko Full Community Integration for Home Assistant**.

---

## Highlights

| Area | What you get |
|------|----------------|
| **Live control (MQTT)** | Climate, lights, fans/pumps, watercare **select**, connectivity and energy-saving **binary sensors** (gateway, transport, spa running, overall link, energy saving). |
| **Shadow extensions** | **Sensors** for numeric leaves, **string** sensors, and **binary sensors** for boolean leaves discovered under unmodeled `zones.*` and relevant `features.*` (for example Waterlab-style chemistry when the device shadow publishes it). Likely chemistry-style paths are **enabled by default**; other numeric paths are often added as **disabled diagnostics** you can enable in the entity registry. |
| **Unknown-zone setpoints** | **Number** entities for supported setpoint paths in unknown zone types so you can write **desired** shadow fragments consistent with the Gecko app / library. |
| **Optional Gecko Cloud REST** | Integration **options** (see below) can poll account/vessel REST data for **summary tile** metrics and strings, merged under paths prefixed with `cloud.rest.*` where **MQTT shadow values take precedence** when both exist. A separate optional poll surfaces **account unread messages** and **per-vessel actions** as **sensor + binary sensor** (counts/previews, not full message history). |
| **Actions (services)** | Publish validated **`state.desired`** JSON for **`zones`** and/or **`features`**, and export a **JSON shadow snapshot** (sanitized by default) for sharing with maintainers. |
| **Diagnostics** | **Download diagnostics** on the config entry includes **shadow topology** summaries (structure-oriented) to inspect what the spa exposes without embedding full live values. |

---

## Feature overview

<table>
<tr>
<td width="33%" valign="top">

### Climate

- Target and current temperature
- Heat-pump-aware `hvac_action` and extra status where available

</td>
<td width="33%" valign="top">

### Lighting & pumps

- Multi-zone LED (including RGB/brightness when supported)
- Pump and blower **fan** entities with speed control

</td>
<td width="33%" valign="top">

### Watercare & connectivity

- **Select** for watercare mode (tracks live mode changes)
- **Binary sensors** for gateway, transport, spa running, overall connection, energy saving

</td>
</tr>
<tr>
<td width="33%" valign="top">

### Shadow-derived entities

- Dynamic **sensors**, **strings**, **booleans**, and **numbers** from the device shadow outside the stock zone models

</td>
<td width="33%" valign="top">

### Cloud gap-filling (optional)

- REST **tile** metrics/strings when MQTT is quiet or disconnected (configurable)
- REST **alerts** preview (unread scope + vessel actions) when enabled

</td>
<td width="33%" valign="top">

### Developer & support tooling

- **Diagnostics** download with topology hints
- **`gecko.dump_shadow_snapshot`** with default **PII-oriented sanitization**

</td>
</tr>
</table>

---

## Installation

### Method 1: HACS (recommended)

1. Open **HACS** in Home Assistant → **Integrations**
2. **⋮** (top right) → **Custom repositories**
3. Repository: `https://github.com/markus-lassfolk/hass_gecko_in_touch3_spa` — category **Integration**
4. Open the new entry → **Download** (pick a release or branch as you prefer)
5. **Restart Home Assistant**

### Method 2: Manual

1. Download a [release](https://github.com/markus-lassfolk/hass_gecko_in_touch3_spa/releases)
2. Copy the `custom_components/gecko` folder into your HA `config/custom_components/` directory
3. **Restart Home Assistant**

Expected layout:

```
config/
└── custom_components/
    └── gecko/
        ├── __init__.py
        ├── manifest.json
        └── …
```

---

## Setup

1. **Settings** → **Devices & services** → **Add integration**
2. Search for **Gecko** (manifest name: **Gecko Full Community Integration for Home Assistant**)
3. Complete **OAuth2** in the browser (Gecko / Auth0 account)
4. Vessels are discovered from your account; each spa gets a device and entities once MQTT connects

### Integration options

On the Gecko card → **Configure**:

| Option | Meaning |
|--------|---------|
| **Cloud REST poll interval** | Seconds between optional polls of the account **vessels** list for summary **tile** numbers/strings (`0` = disabled). Default is off. |
| **Only poll when MQTT is disconnected** | When enabled, REST tile polling runs only if the live MQTT link for that monitor is down — useful to avoid duplicating data while MQTT is healthy. |
| **Alerts poll interval** | Seconds between optional REST calls for **unread-messages** (account-scoped) and **vessel actions** (`0` = disabled). Feeds the **REST active alerts** sensor/binary pair. |

---

## Entities (summary)

| Platform | Role |
|----------|------|
| **Climate** | Main spa temperature control |
| **Light** | Per lighting zone |
| **Fan** | Pumps / blowers |
| **Select** | Watercare operation mode |
| **Binary sensor** | Connection stack, energy saving, dynamic shadow booleans, **REST active alerts** (on when there is something to review, per snapshot rules) |
| **Sensor** | RF strength/channel, gateway/spa status text where modeled, **dynamic shadow numerics**, **dynamic shadow strings**, **REST active alerts** (counts / short previews) |
| **Number** | Writable unknown-zone **setpoints** (shadow **desired**), where paths match supported setpoint shapes |

**Shadow extension sensors:** values under `cloud.rest.*` can remain available from REST when MQTT is offline; pure MQTT paths follow normal entity availability.

**Diagnostics:** **Download diagnostics** on the integration entry includes `shadow_topology` (and related client summaries) for mapping work.

---

## Actions (Services)

All actions live under the **`gecko`** domain (Developer tools → **Actions**).

| Action | Purpose |
|--------|---------|
| **`gecko.publish_zone_desired`** | Publish `{"zones": { zone_type: { zone_id: updates } } }` over the active MQTT connection. |
| **`gecko.publish_feature_desired`** | Publish `{"features": … }` over MQTT. |
| **`gecko.publish_desired_state`** | Publish a **validated** object as shadow **desired** — only top-level keys **`zones`** and/or **`features`** (size-limited). |
| **`gecko.dump_shadow_snapshot`** | Write JSON under **`config/gecko_shadow_dumps/`**. By default **sanitizes** for public share (fingerprints, key-based redaction, string scrub for emails/JWT-like/MAC/private IP/UUID/long hex). Disable **Sanitize for public share** only for private debugging. Requires an active Gecko MQTT client for that monitor (connect the integration once). |

---

## Share data for new hardware support

Call **`gecko.dump_shadow_snapshot`** with defaults for a **community-safe** export, then attach the file to a GitHub issue (after a quick manual skim). See **Actions** above for options (`include_configuration`, `include_derived`, `filename`, `sanitize_for_public_share`).

---

## Troubleshooting

### Integration not appearing

- Restart Home Assistant after copying `custom_components/gecko`
- Check logs for import errors

### OAuth fails

- Confirm Gecko account credentials in the browser
- Retry after clearing site data for Auth0 / Gecko if needed

### No devices or entities

- Confirm the spa is online in the **Gecko mobile app**
- Allow time for MQTT session and zone configuration from **`gecko_iot_client`**

### Entities not updating

- Inspect RF / connectivity **binary sensors**
- Reload the integration entry, or restart HA if MQTT is stuck

### REST options seem to do nothing

- Ensure poll intervals are **greater than zero**
- For tiles, confirm **Only poll when MQTT is disconnected** is not blocking polls while MQTT is up
- Many Gecko Cloud routes return **403** for consumer tokens; unsupported routes fail quietly in logs

---

## Architecture notes

**MQTT first, REST as backup:** live **AWS IoT device shadow** over MQTT is the source of truth for control and high-frequency state. Optional REST polling reuses the same OAuth session to fill gaps (summary tiles, alerts) when permitted by the API.

**Auth:** Broker URL and short-lived MQTT credentials come from Gecko’s **`thirdPartySession`** style APIs; there is **no** supported path that avoids Gecko cloud login for this stack.

---

## Roadmap & limits

**Already in this fork:** shadow-derived **sensor / binary_sensor / string / number** entities, REST **tile** merge, REST **alerts** snapshot entities, MQTT **desired** services, **sanitized shadow export**, richer **diagnostics**, and configurable REST intervals.

**Still constrained by Gecko’s API:** many app-only REST surfaces return **HTTP 403** for normal third-party OAuth tokens. Full parity with every Gecko app screen may require **policy changes from Gecko Alliance**, not only more Home Assistant code.

**Privacy:** do not commit real account, vessel, monitor, token, or **`.secrets/`** data to git. Use placeholders in issues and PRs.

---

## Support

- **Issues:** [github.com/markus-lassfolk/hass_gecko_in_touch3_spa/issues](https://github.com/markus-lassfolk/hass_gecko_in_touch3_spa/issues)
- **Discussions:** [github.com/markus-lassfolk/hass_gecko_in_touch3_spa/discussions](https://github.com/markus-lassfolk/hass_gecko_in_touch3_spa/discussions)

---

## Credits

- **[gecko-iot-client](https://github.com/geckoal/gecko-iot-client)** — MQTT / shadow client
- **Upstream integration:** [geckoal/ha-gecko-integration](https://github.com/geckoal/ha-gecko-integration)
- **Trademarks:** Gecko and related marks belong to **Gecko Alliance**; see [NOTICE](NOTICE)

---

## License

Copyright © 2025-2026 Gecko Alliance

Licensed under the Apache License 2.0 — see [LICENSE](LICENSE).

> **Important:** Intended for personal use with Gecko Alliance equipment through Home Assistant. Commercial use may require separate authorization from Gecko Alliance. See [NOTICE](NOTICE) for trademark and usage details.
