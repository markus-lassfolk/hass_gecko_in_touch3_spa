# Release notes — Gecko Full Community **2.5.2**

*User-oriented summary. Technical detail lives in [`CHANGELOG.md`](CHANGELOG.md).*

---

## At a glance

Patch after **2.5.1**: if you use **“Cloud REST only when MQTT is down”** and restart Home Assistant while the spa stays on MQTT, **2.5.1** still never ran a first REST poll, so **`cloud.rest.*` stayed empty** in diagnostics until MQTT dropped. **2.5.2** runs **one** REST tile fetch on startup to seed chemistry-style fields, then keeps **2.5.1** behaviour (no further REST polls while MQTT is up; last snapshot retained).

---

## Upgrade path

1. Update to **2.5.2** and restart Home Assistant.

---

## Thanks

Thanks for the follow-up diagnostics that highlighted the cold-start gap.
