# Release notes — Gecko Full Community **2.4.0**

*User-oriented summary. Technical detail lives in [`CHANGELOG.md`](CHANGELOG.md).*

---

## At a glance

This release focuses on **reliability**, **supportability**, and **being gentle on Gecko’s cloud APIs**, while keeping **real-time spa control over MQTT** the same as you are used to.

---

## Please read: polling defaults (breaking behaviour change)

We have changed the **default how often the integration calls Gecko’s HTTP (REST) APIs** for optional extras — **not** how often your spa’s live data is refreshed.

- **Live data (water temperature, pumps, lights, climate, most sensors)** still comes from **AWS IoT / MQTT** and still refreshes on the **normal Home Assistant coordinator interval** (about **every 30 seconds** when the integration is active). **Nothing in this release slows that down.**
- What **did** change are the **defaults for optional cloud REST polling**:
  - **Cloud REST poll interval** (tiles, vessel list, chemistry-style enrichment from the cloud): was **every 5 minutes**, now **once per day** (**86 400 seconds**).
  - **Premium energy poll interval** (only if you linked the app token): was **every hour**, now **once per day**.

**Why:** Aggressive REST polling can trigger **rate limits or blocks** from Gecko’s servers. MQTT remains the supported path for frequent updates; REST is best treated as **slow enrichment**.

**If you need faster REST again:**  
**Settings → Devices & services → Gecko → Configure** → set **Cloud REST poll interval** and/or **Energy poll interval** to the interval you want (up to once per day in the UI cap, or **0** to turn REST tile polling off entirely). Alerts polling stays **off** until you enable it.

**If you already saved options** (for example **300** seconds for cloud REST): those values are **left as-is** so an intentional 5‑minute schedule is not overwritten. Switch to **86 400** in **Configure** only if you want the new daily default.

---

## What’s new and improved

### Safer water temperature numbers

Measured **current** water temperature (climate entity and spa temperature sensor) is now **filtered** so impossible or placeholder values (like **0 °C**, **NaN**, or values **far outside a normal spa range**) become **“unknown”** instead of polluting graphs and the recorder. **Setpoints are not altered.**

### Richer diagnostics for support

**Download diagnostics** now includes a **`spa_configuration_summary`** per vessel: a compact map of **which pump / light / blower / waterfall IDs** belong to **which zone IDs** according to the spa configuration from when the vessel was linked. That makes it easier to match **REST layout** to **MQTT shadow paths** when troubleshooting.

### More robust reconnects

When the integration **reconnects** or **refreshes tokens**, failed attempts are cleaned up more carefully so **orphan MQTT clients** are less likely. We avoid **disconnecting a working session** when the failure happens **before** reconnect has started tearing down the old link, and we **release the connection lock** before running a potentially slow **MQTT disconnect** in cleanup paths so other operations are not blocked as long.

### Small quality touches

- Heating-related **binary sensors** use the **heat** device class where it fits.
- **Bug reports**: template and README now nudge you toward **diagnostics**, **versions**, and **redacted logs** so issues are faster to diagnose.

---

## Upgrade path

1. **Read the polling section above** and decide whether you want faster REST than “once per day”.
2. **Update** the custom component (HACS or manual copy) to **2.4.0**.
3. **Restart Home Assistant.**
4. Optionally open **Configure** on the Gecko integration and set **Cloud REST poll interval** / **Energy poll interval** to your preferred values.

---

## Thanks

Thanks to everyone reporting edge cases (temperature spikes, cloud limits, reconnect quirks) — those reports directly shaped this release.
