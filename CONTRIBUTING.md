# Contributing

## Where to open pull requests

**All development for this community fork happens in:**

**https://github.com/markus-lassfolk/hass_gecko_in_touch3_spa**

When you create a PR (GitHub web UI, GitHub CLI, or Cursor), set the **base repository** to **`markus-lassfolk/hass_gecko_in_touch3_spa`**, not `geckoal/ha-gecko-integration`.

The upstream repo [**geckoal/ha-gecko-integration**](https://github.com/geckoal/ha-gecko-integration) is the historical Gecko Alliance integration. Opening feature work there by mistake creates noise for upstream maintainers. Only open PRs there if you are **deliberately** contributing back to Gecko Alliance with their agreement.

### If a PR was opened against upstream by mistake

1. Close the mistaken PR on GitHub (or ask upstream to close it).
2. Open the same branch against **`markus-lassfolk/hass_gecko_in_touch3_spa`** instead.

### Git remotes (optional)

Many clones use:

- **`origin`** → your fork (`markus-lassfolk/hass_gecko_in_touch3_spa`) — **push and PR targets here**.
- **`upstream`** → `geckoal/ha-gecko-integration` — **fetch only** for syncing; do not assume PR base is this remote.

---

## Tests

From the repo root (with dev dependencies installed):

```bash
pytest tests/
```

---

## Style

`ruff check` and `ruff format` are used in CI; match existing patterns in `custom_components/gecko/`.
