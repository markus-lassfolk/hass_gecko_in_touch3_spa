<div align="center">

# 🦎 Gecko Integration for Home Assistant

**Control your spa, hot tub, or pool equipment directly from Home Assistant**

[![GitHub Release](https://img.shields.io/github/release/geckoal/ha-gecko-integration.svg?style=for-the-badge)](https://github.com/geckoal/ha-gecko-integration/releases)
[![HACS](https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg?style=for-the-badge)](LICENSE)

</div>

---

## ✨ Features

<table>
<tr>
<td width="33%" valign="top">

### 🌡️ Climate Control
- Set target temperature
- Monitor current temperature
- Track heating status

</td>
<td width="33%" valign="top">

### 💡 Lighting
- Multi-zone LED control
- On/off control

</td>
<td width="33%" valign="top">

### 💨 Pumps & Fans
- Blower control
- Multiple pump zones

</td>
</tr>
<tr>
<td width="33%" valign="top">

### 🔄 Watercare Modes
- Away mode
- Standard mode
- Energy savings mode
- Weekender mode

</td>
<td width="33%" valign="top">

### 📊 Monitoring
- Gateway status
- Connection health

</td>
<td width="33%" valign="top">

### ☁️ Cloud Integration
- Secure OAuth2 login
- AWS IoT backend
- Automatic discovery
- Push notifications

</td>
</tr>
</table>

---

## 📦 Installation

### Method 1: HACS (Recommended)

1. Open **HACS** in Home Assistant
2. Navigate to **Integrations**
3. Click the **⋮** menu (top right) → **Custom repositories**
4. Add repository URL: `https://github.com/geckoal/ha-gecko-integration`
5. Select category: **Integration**
6. Click **Download** on the Gecko integration
7. **Restart Home Assistant**

### Method 2: Manual Installation

1. Download the [latest release](https://github.com/geckoal/ha-gecko-integration/releases)
2. Extract and copy the `custom_components/gecko` folder to your Home Assistant `custom_components` directory
3. Your directory structure should look like:
   ```
   config/
   └── custom_components/
       └── gecko/
           ├── __init__.py
           ├── manifest.json
           └── ...
   ```
4. **Restart Home Assistant**

---

## ⚙️ Setup & Configuration

### Initial Setup

1. Go to **Settings** → **Devices & Services**
2. Click **+ Add Integration**
3. Search for **"Gecko"**
4. Click on the Gecko integration

### Authentication

The integration will guide you through OAuth2 authentication:

1. Click **Continue** to begin the OAuth flow
2. You'll be redirected to the Gecko login page
3. Enter your **Gecko spa account credentials**
4. Grant permission to Home Assistant
5. You'll be redirected back automatically

### Automatic Discovery

Once authenticated:
- Your spa(s) will be **automatically discovered**
- All available entities will be created
- Devices appear under **Settings** → **Devices & Services** → **Gecko**

---

## 🎛️ Available Entities

The integration creates multiple entity types for comprehensive spa control:

| Entity Type | Description | Example |
|------------|-------------|---------|
| **Climate** | Temperature control and monitoring (heat pump aware `hvac_action`, extra status) | Set spa to 104°F (40°C) |
| **Light** | LED on/off plus RGB and brightness when the zone supports it | Adjust ambient lighting |
| **Fan** | Pump and blower speed control; optional initiator hints in attributes | Set pump to High speed |
| **Binary Sensor** | Gateway/spa/connection status plus energy-saving mode flag | Gateway connected status |
| **Select** | Watercare mode (updates when the spa changes mode, not only on poll) | Switch to Energy Savings mode |
| **Sensor** | Numeric values from the cloud device shadow outside modeled zones (e.g. Waterlab / chemistry paths) | pH, ORP, or other metrics when the spa publishes them |

### Entity Examples

**Climate Entity:**
- `climate.spa_name` - Main temperature control

**Light Entities:**
- `light.spa_name_zone_1` - Primary lighting zone
- `light.spa_name_zone_2` - Secondary lighting zone

**Fan Entities:**
- `fan.spa_name_pump_1` - Main circulation pump
- `fan.spa_name_pump_2` - Jet pump

**Sensors (shadow extensions):**
- Entities are created dynamically from the Gecko IoT **device shadow** for numeric fields under unknown `zones.*` branches (such as Waterlab) and for `features.*` other than watercare operation mode.
- Likely water-chemistry paths (names containing `ph`, `orp`, `chlorine`, etc.) are **enabled by default**; other numeric leaves are added as **disabled** diagnostic sensors you can enable in the entity registry.
- Download diagnostics (**Settings → Devices & Services → Gecko → Download diagnostics**) to inspect redacted shadow topology (`shadow_topology`) without raw values.

---

## 🆘 Troubleshooting

### Common Issues

**Integration not appearing:**
- Ensure you've restarted Home Assistant after installation
- Check that the `custom_components/gecko` folder exists
- Review Home Assistant logs for errors

**OAuth authentication fails:**
- Verify your Gecko account credentials
- Check internet connectivity
- Try clearing browser cache and retry

**No devices discovered:**
- Confirm your spa is online in the Gecko mobile app
- Wait 1-2 minutes for discovery to complete
- Check that your spa uses Gecko in.touch 3 / in.touch 3+ or compatible system

**Entities not updating:**
- Check RF signal strength sensor (low signal affects updates)
- Verify gateway connectivity in the Gecko app
- Restart the integration: **Settings** → **Devices & Services** → **Gecko** → **⋮** → **Reload**

---

## Roadmap: parity with the Gecko app

Today this integration focuses on **what the gateway exposes over AWS IoT** (zones: climate, lights, pumps/fans, watercare **select**, connectivity **binary_sensor**, plus **shadow-derived sensors** for paths the stock client does not model). That matches most **control and live state** users expect from a spa controller.

The Gecko **mobile app** also calls **Gecko Cloud REST** for account/vessel summaries, messages, billing, routines, some monitor tools, and similar screens. Your local API maps (from `scripts/verify_shadow_live.py`) show many of those routes return **HTTP 403** for a normal consumer OAuth token: the route exists, but the **token is not allowed** to use it. Full parity for those screens may require **Gecko to extend third-party API access**, not only more Home Assistant code.

**MQTT first, REST as backup**

Prefer **live AWS IoT (MQTT) shadow** for control and most sensors: it is the lowest-latency path and matches what the gateway actually runs. Use **Gecko Cloud REST** only to **fill gaps**—for example vessel list **summary** tiles when MQTT is not connected yet, or fields that appear in the app’s REST payload but are not (yet) mapped on the shadow. REST calls still use the same **OAuth-linked session**; they do not replace MQTT for steering the spa.

**Can it work with no Auth0 / login at all?** **Not** with the official Gecko cloud stack this integration uses. The MQTT broker URL and embedded credentials come from **`GET …/iot/thirdPartySession`**, which requires a **valid Gecko API bearer token** (from OAuth). Those credentials are **short-lived**; refreshing them calls the API again. There is **no supported anonymous or purely local-LAN MQTT** mode here unless Gecko ships a different protocol or exposes an official local API.

**Practical phases**

1. **Align “summary” data** — Optionally poll `GET /v4/accounts/{account_id}/vessels` (and newer detail routes such as `GET /v6/.../vessels/{vessel_id}?customActionsVersion=0` when needed) so HA can mirror **app-style summary tiles** (e.g. pH/ORP/temp/warnings) when MQTT is sparse; keep MQTT + `gecko_iot_client` as the source of truth for **commands** and **high-frequency** state.
2. **Close control gaps** — Audit each app control path against zones and services already exposed; add **number**/ **button**/ **services** only where the client library and shadow support writes.
3. **Add REST-only features where allowed** — For routes that return **200** with the same OAuth token (discovered via your snapshots), add entities or `notify`/`todo`/`button` flows; skip or document anything stuck at **403** until Gecko changes policy.
4. **Diagnostics** — Surface capability flags (topology, REST reachability summaries) **without** embedding real IDs in git-tracked source; users redact their own downloaded diagnostics before sharing.

### Privacy & portable source code

Do **not** commit real **account**, **vessel**, or **monitor** IDs, **Auth0 `sub`**, **email**, **addresses**, **tokens**, or files under **`.secrets/`** into this repository. Use placeholders in examples and tests so anyone can use the integration with their own data. See `custom_components/gecko/const.py` for the same rule in code comments.

---

## 💬 Support & Community

- 🐛 **Report Issues:** [GitHub Issues](https://github.com/geckoal/ha-gecko-integration/issues)
- 💡 **Feature Requests:** [GitHub Discussions](https://github.com/geckoal/ha-gecko-integration/discussions)
- 📖 **Documentation:** [Full Docs](https://github.com/geckoal/ha-gecko-integration)

---

## 🙏 Credits

This integration is built with the [gecko-iot-client](https://github.com/geckoal/gecko-iot-client) library.

---

## 📄 License

Copyright © 2025-2026 Gecko Alliance

Licensed under the Apache License 2.0 - see [LICENSE](LICENSE) for details.

> **⚠️ Important:** This software is intended for personal use with Gecko Alliance equipment through Home Assistant. Commercial use or use outside the intended scope may require authorization from Gecko Alliance. See [NOTICE](NOTICE) for trademark information and usage restrictions.
