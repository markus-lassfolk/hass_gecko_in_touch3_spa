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

**Pull requests:** open them **only** against [**markus-lassfolk/hass_gecko_in_touch3_spa**](https://github.com/markus-lassfolk/hass_gecko_in_touch3_spa), not against `geckoal/ha-gecko-integration`, unless you intentionally contribute upstream. See [**CONTRIBUTING.md**](CONTRIBUTING.md).

---

## Highlights

| Area | What you get |
|------|----------------|
| **Live control (MQTT)** | Climate, lights, fans/pumps, watercare **select**, connectivity and energy-saving **binary sensors** (gateway, transport, spa running, overall link, energy saving). |
| **Premium energy data** | Optional second login (app-client token) unlocks **energy consumption (kWh)**, **energy cost**, and **energy score** sensors — compatible with the **HA Energy Dashboard**. See [Link energy data](#link-energy-data-optional) below. |
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
<tr>
<td colspan="3" valign="top">

### Premium energy data (optional)

Link the Gecko mobile-app client via **Configure → Link energy data** to unlock **energy consumption**, **energy cost**, and **energy score** sensors. The consumption sensor is compatible with the **HA Energy Dashboard** out of the box. See [setup instructions](#link-energy-data-optional) below.

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

### Link energy data (optional)

The default Gecko OAuth login uses a community client that covers MQTT control and basic REST data. However, Gecko's API **blocks energy, chart, activity, and routine endpoints** for this client (HTTP 403). The Gecko mobile app uses a different client ID that has access to these premium endpoints.

This integration lets you optionally link that second client to unlock additional sensors — no changes to your primary login are needed.

#### What it unlocks

| Entity | Device class | Enabled by default | HA Energy Dashboard |
|--------|-------------|-------------------|-------------------|
| **Energy consumption** | `energy` (kWh, total increasing) | Yes | Yes — add it as an individual device consumption source |
| **Energy cost** | `monetary` (total) | Yes | Yes — can track alongside consumption |
| **Energy score** | measurement (unit from API) | Yes | No |

#### How to link

1. Go to **Settings → Devices & services → Gecko** and click **Configure**
2. Select **Link energy data (premium)** from the menu
3. The integration generates an authorization URL — **open it in your browser**
4. **Log in** with your normal Gecko account credentials
5. After login, your browser will try to open a link starting with `com.geckoportal.gecko://…` — **this will fail** (that is expected, it is the mobile app's deep link)
6. **Copy the full URL** from your browser's address bar (it contains a `code=…` parameter)
7. **Paste** the full URL into the input field in Home Assistant and submit

The integration exchanges the code for a token, stores it alongside your existing login, and reloads. Energy sensors appear automatically.

> **Tip:** The energy consumption sensor uses `state_class: total_increasing` and `device_class: energy`, which means you can add your spa to the **HA Energy Dashboard** under **Settings → Dashboards → Energy → Add consumption → Individual devices**.

#### How to unlink

Go to **Configure → Unlink energy data** and confirm. The energy token is removed, the integration reloads, and the energy sensors disappear.

---

## Entities (summary)

| Platform | Role |
|----------|------|
| **Climate** | Main spa temperature control |
| **Light** | Per lighting zone |
| **Fan** | Pumps / blowers |
| **Select** | Watercare operation mode |
| **Binary sensor** | Connection stack, energy saving, dynamic shadow booleans, **REST active alerts** (on when there is something to review, per snapshot rules) |
| **Sensor** | **Spa target / current temperature** (°C, per thermostat zone — for cards that only accept `sensor`), **dynamic shadow numerics**, **dynamic shadow strings**, optional **REST active alerts** count + previews, optional **energy consumption / cost / score** (premium) |
| **Number** | Writable unknown-zone **setpoints** (shadow **desired**), where paths match supported setpoint shapes |

**Shadow extension sensors:** values under `cloud.rest.*` can remain available from REST when MQTT is offline; pure MQTT paths follow normal entity availability.

**Diagnostics:** **Download diagnostics** on the integration entry includes `shadow_topology` (and related client summaries) for mapping work.

---

## Supported entities and values

Everything below is **per vessel / spa device** unless noted. Zone counts and shadow leaves depend on your pack hardware and firmware.

### Climate (`climate`)

| Item | Supported values / behavior |
|------|-----------------------------|
| **Entities** | One **thermostat** per **temperature control** zone from `gecko_iot_client` (zone id in the translated name). |
| **HVAC mode** | **`heat` only** — other modes are rejected by the service layer. |
| **Temperature** | **Current** and **target** in **°C**; target step **0.5**; min/max setpoints come from the zone configuration. |
| **`hvac_action`** | Derived from spa status: **`idle`**, **`heating`**, **`cooling`**, **`defrosting`** (heat-pump defrost), or idle fallbacks for invalid / error-like library states. |
| **Attributes** | When present: **`detailed_status`** — raw status enum name from the library; **`eco_mode`** — eco flag from the zone mode object. |

### Light (`light`)

| Item | Supported values / behavior |
|------|-----------------------------|
| **Entities** | One entity per **lighting** zone. |
| **Color modes** | **`onoff`** only, or **`rgb`** when the zone supports `set_color` (RGB zones do not also expose a separate on/off-only mode). |
| **State** | **`on` / `off`**; for RGB zones: **`rgb_color`** `(R,G,B)` and **`brightness`** when reported. |
| **Attributes** | Optional **`effect`** string when the zone exposes an effect name. |

### Fan (`fan`)

| Item | Supported values / behavior |
|------|-----------------------------|
| **Entities** | One per **flow** zone (pump, blower, waterfall-style types from the library). |
| **Features** | **On/off**; **`set_speed`** when the zone reports preset-based speed capability. |
| **Preset / speed labels** | Internal speed strings **`off`**, **`low`**, **`medium`**, **`high`** mapped to the library’s discrete speed levels. |
| **Percentage** | 0–100 from the zone; for display bands: **0–33 → low**, **34–66 → medium**, **67–100 → high** when mapping to those labels. |
| **Preset list** | When supported, **`preset_mode`** / speed list uses the **preset names** from the device (not a fixed HA list). |
| **Attributes** | Optional **`initiators`** — list of demand initiator strings when the spa reports them. |

### Select — Watercare (`select`)

| HA option value (state) | Meaning (sent to spa as library mode name) |
|-------------------------|-------------------------------------------|
| `away` | Away |
| `standard` | Standard |
| `savings` | Savings |
| `super_savings` | Super Savings |
| `weekender` | Weekender |
| `other` | Other |

Unknown library names are normalized to a snake_case option when possible.

### Binary sensor (`binary_sensor`)

**Fixed connectivity / mode (one set per vessel)**

| Entity (translation key) | Device class (typical) | **On** when |
|--------------------------|------------------------|-------------|
| **Gateway Status** | `connectivity` | Library gateway status string is **`connected`** (case-insensitive). |
| **Spa Status** | `running` | Library vessel status string is **`running`** (case-insensitive). |
| **Transport Connection** | `connectivity` | Cloud **transport** link boolean is true. |
| **Overall Connection** | `connectivity` | Library reports **fully connected**. |
| **Energy Saving Mode** | _(none)_ | Operation mode controller reports **energy saving** active. |

**Optional REST alerts** (created only if **Alerts poll interval** is greater than `0`)

| Entity | Device class | **On** when |
|--------|--------------|-------------|
| **Active Alerts** | `problem` | REST snapshot **`total`** (unread scoped messages + active actions) is greater than zero. |

**Dynamic shadow booleans**

| Aspect | Detail |
|--------|--------|
| **Attributes** | Same as numeric shadow sensors: **`shadow_path`**, **`gecko_diagnostic_group`**. |
| **Source** | Boolean leaves under **unknown `zones.*` types**, under **`features.*`**, under **`connectivity*`** trees in **reported** shadow (and merged **`cloud.rest.*`** bools). |
| **Heuristic device classes** | Paths may get classes such as **`moisture`** (leak/flood), **`connectivity`**, **`running`**, **`heat`** / **`cold`**, **`lock`**, **`motion`**, **`problem`**, **`power`** when path tokens match (see code `infer_binary_sensor_device_class`). |
| **Default visibility** | Paths suggesting **alarm / fault / leak / warning** tend to be **enabled by default**; connectivity-like and RF-diagnostic paths stay **disabled diagnostics** until you enable them. |

### Sensor (`sensor`)

**Spa thermostat mirror sensors** (one **temperature control** zone each)

| Entity | Meaning |
|--------|---------|
| **Target temperature {zone_id}** | Same value as the **`climate`** thermostat **setpoint** (°C). |
| **Current temperature {zone_id}** | Same value as the thermostat **measured** spa temperature (°C). |

These use `device_class: temperature` and `state_class: measurement` so pool/spa dashboard cards that only list **`sensor`** entities (for example some **Pool Monitor** cards) can bind to them instead of `climate`.

**Optional REST active alerts** (alerts poll interval greater than `0`)

| State | Type | Meaning |
|-------|------|---------|
| **`state`** | integer | **`total`** = count of **scoped unread messages** plus **active vessel actions** (capped lists in attributes). |
| **`messages`** attribute | list | Up to **16** entries: `id`, `title`, `preview` (short body). |
| **`actions`** attribute | list | Up to **16** entries: `id`, `title`, `status`. |
| **`updated_at`** | ISO timestamp | When the snapshot was built. |
| **`error`** | string or empty | Set when the REST merge reports an error string. |

**Premium energy sensors** (created only when [energy data is linked](#link-energy-data-optional))

| Entity | Device class | State class | Unit | Default |
|--------|-------------|------------|------|---------|
| **Energy consumption** | `energy` | `total_increasing` | kWh | **Enabled** — eligible for HA Energy Dashboard |
| **Energy cost** | `monetary` | `total` | currency from API | **Enabled** |
| **Energy score** | _(none)_ | `measurement` | from API (often %) | **Enabled** |

The **energy consumption** sensor reports total kWh consumed by the spa. Because it uses `total_increasing`, Home Assistant automatically calculates hourly/daily/monthly usage — no template sensors or utility meters needed. Add it to the Energy Dashboard under **Individual devices**.

Energy data is polled hourly by default (configurable via `energy_poll_interval` in options). The raw API response is available as an `raw_response` attribute on each entity for debugging.

**Dynamic shadow numeric sensors**

| Aspect | Detail |
|--------|--------|
| **Attributes** | Each entity exposes **`shadow_path`** (dotted path) and **`gecko_diagnostic_group`** (`calibration_model`, `rf`, `connectivity`, `chemistry_live`, `chemistry_other`, `other`) for automations and support. |
| **Sources** | Numeric leaves under **`zones.<type>.<id>`** for zone types **other than** `flow`, `lighting`, `temperatureControl`; under **`features.<feature>`**; under top-level **`connectivity`** / **`connectivity…`** keys in **reported**. |
| **Caps** | Rough safety limits: up to **192** numeric paths, **128** booleans, **128** strings (deeply nested shadow is truncated by depth/limits). |
| **Setpoints** | Single-leaf unknown-zone paths whose last segment looks like a **setpoint** (`setpoint`, `targetTemp`, `goal`, …) become **`number`** entities instead of sensors. |
| **Units / device classes** | Inferred from path text (examples): **pH** → `ph`; **ORP / redox** → `voltage` in **mV**; **temperature** → **°C**; humidity/moisture **%**; **V / A / W / kWh / Hz**; pressure **psi** or **bar**; flow **gal/min** or **L/min**; conductivity **µS/cm**; **TDS**; various chemistry-related **ppm**; duration when names include seconds/minutes. |
| **Default vs diagnostic** | Likely **live chemistry** paths (pH/ORP/chlorine/salinity/TDS/etc., and selected **`cloud.rest.readings.*`** keys) are often **enabled by default**. **Calibration/model** parameters (e.g. offset/slope mV, thermistor **R0/T0/beta**), **RF diagnostics**, and generic **connectivity** numerics are usually **disabled diagnostics**. |

**Dynamic shadow string sensors**

| Aspect | Detail |
|--------|--------|
| **Attributes** | **`shadow_path`**, **`gecko_diagnostic_group`** (same buckets as numerics). |
| **Sources** | Same extension trees as booleans; strings must be **non-empty**, **≤ 255** characters, not JWT-like, not on sensitive path tokens. **`features.operationMode`** strings are skipped (watercare is already the **select**). |
| **REST merge** | **`cloud.rest.*`** strings from vessel summary / readings / actions (see below). |
| **Default visibility** | Paths containing tokens like **water, status, message, mode, tile, summary, actions** under REST, or **alarm / message / status / fault** in the shadow, are more likely enabled by default; others are often diagnostics. |

### Number (`number`)

| Aspect | Detail |
|--------|--------|
| **Role** | Writable **single-leaf** setpoints for **unknown** zone types (`zones.{type}.{zoneId}.{leaf}`) where **type** is not `flow`, `lighting`, or `temperatureControl`, and **leaf** matches setpoint-like names (`setpoint`, `targetTemp`, `goal`, …). |
| **Attributes** | **`shadow_path`**, **`zone_type`**, **`zone_id`**, **`field_key`** (same fragment keys used when publishing **desired**). |
| **UI mode** | **Box** entry in HA. |
| **Limits (heuristic)** | **pH-like paths:** 0–14 step **0.1**; **ORP-like:** 0–1000 step **1**; **temperature-like:** **4–42** °C step **0.5**; otherwise **0–100** step **1**. |
| **Write path** | Publishes MQTT **`state.desired`** fragment `{ "zones": { "<type>": { "<id>": { "<leaf>": <value> } } } }` via the active Gecko client. |

### Optional REST tile paths (`cloud.rest.*`)

When **Cloud REST poll interval** is enabled, metrics are merged under **`cloud.rest.*`**. If MQTT later publishes the **same dotted path**, the **shadow (MQTT) value wins**.

**Stable / documented numeric paths**

| Path | Meaning |
|------|---------|
| `cloud.rest.disc_elements.temp_c` | Water / spa temperature in **°C** from disc or status-style fields. |
| `cloud.rest.summary.ph` | **pH** from `phStatus` / `ph_status` style objects. |
| `cloud.rest.summary.orp_mv` | **ORP** in millivolts from `orpStatus` / `orp_status` style objects. |
| `cloud.rest.readings.<key>` | Numeric **`.value`** from each entry under REST **`readings`**, **`monitorReadings`**, **`reportReadings`**, or **`computedReadings`** (including when nested under **`status`**). Keys are **API-defined** (examples seen in the wild: `waterTemp`, `ph`, `orp`, `freeChlorine`, `totalAlkalinity`, `totalHardness`, `cyanuricAcid`, `calciumHardness`, `adjustedTotalAlkalinity`, `lsi`, `phStc20`, …). |
| `cloud.rest.actions.count` | Number of **`status.actions`** list entries. |

**String paths (representative)**

| Pattern | Meaning |
|---------|---------|
| `cloud.rest.disc.*` / `cloud.rest.status.*` | Short strings from **`waterStatus`**, **`flowStatus`**, **`statusText`**, **`message`**, **`text`**, etc., including nested `text` / `message` / `value` leaves when the API uses objects. |
| `cloud.rest.readings.<key>.status` / `.title` / `.unit` / `.abbreviation` / `.source` | Metadata strings beside each readings entry. |
| `cloud.rest.actions.<actionType>` | Action **title**. |
| `cloud.rest.actions.<actionType>.instructions` | Joined instruction text (length-clamped for HA). |
| `cloud.rest.disc.waterStatusColor` | Tile color hint string when present. |
| `cloud.rest.disc.lastUpdatedText` | Human “last updated” text when present. |

**Boolean paths**

| Pattern | Meaning |
|---------|---------|
| `cloud.rest.status.<key>` | Shallow boolean fields on the REST **status** object. |
| `cloud.rest.disc_elements.<key>` | Booleans under **`discElements`** / **`disc_elements`**, including one level of nested bools. |

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

- Inspect connectivity **binary sensors** (gateway, transport, overall link, spa running)
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

**Already in this fork:** shadow-derived **sensor / binary_sensor / string / number** entities, REST **tile** merge, REST **alerts** snapshot entities, MQTT **desired** services, **sanitized shadow export**, richer **diagnostics**, configurable REST intervals, and **premium energy data** via optional app-client token link.

**Energy data unlocked:** the optional [Link energy data](#link-energy-data-optional) step uses the Gecko mobile-app client to bypass the HTTP 403 restriction on energy, chart, activity, and routine endpoints. This is the same client the official Gecko app uses.

**Still constrained by Gecko’s API:** some app-only REST surfaces may still return **HTTP 403** for the community OAuth token. The optional energy link covers the known premium endpoints; full parity with every Gecko app screen may require **further reverse engineering or policy changes from Gecko Alliance**.

**Privacy:** do not commit real account, vessel, monitor, token, or **`.secrets/`** data to git. Use placeholders in issues and PRs.

---

## Support

- **Issues:** [github.com/markus-lassfolk/hass_gecko_in_touch3_spa/issues](https://github.com/markus-lassfolk/hass_gecko_in_touch3_spa/issues)
- **Discussions:** [github.com/markus-lassfolk/hass_gecko_in_touch3_spa/discussions](https://github.com/markus-lassfolk/hass_gecko_in_touch3_spa/discussions)

---

## Credits

- **[gecko-iot-client](https://github.com/geckoal/gecko-iot-client)** — MQTT / shadow client
- **Upstream reference (do not use as PR target for this fork):** [geckoal/ha-gecko-integration](https://github.com/geckoal/ha-gecko-integration)
- **Trademarks:** Gecko and related marks belong to **Gecko Alliance**; see [NOTICE](NOTICE)

---

## License

Copyright © 2025-2026 Gecko Alliance

Licensed under the Apache License 2.0 — see [LICENSE](LICENSE).

> **Important:** Intended for personal use with Gecko Alliance equipment through Home Assistant. Commercial use may require separate authorization from Gecko Alliance. See [NOTICE](NOTICE) for trademark and usage details.
