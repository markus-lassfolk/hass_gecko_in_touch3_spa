# Release notes — Gecko Full Community **2.5.1**

*User-oriented summary. Technical detail lives in [`CHANGELOG.md`](CHANGELOG.md).*

---

## At a glance

Patch release: fixes **REST-backed sensors** going **unavailable** when **“Cloud REST only when MQTT is down”** is enabled and MQTT stays connected.

---

## What changed

If you use **Configure → Cloud REST only when MQTT is down**, the integration used to **wipe cached REST tile data** on every update while MQTT was up. Many water-quality and tile fields exist only under **`cloud.rest.*`**, so those entities showed **unavailable** for long periods even though you only wanted to **skip new REST calls**, not discard the last values.

**2.5.1** keeps the **last successful REST snapshot** merged until the next time a REST poll actually runs (for example after a short MQTT dropout with that option, or if you turn the option off so your poll interval can refresh while MQTT is up). Live **MQTT / shadow** data still **wins on overlapping keys** when merged.

---

## Upgrade path

1. Update the custom component to **2.5.1** (HACS or manual copy).
2. **Restart Home Assistant.**

---

## Thanks

Thanks for the report that led to this fix.
