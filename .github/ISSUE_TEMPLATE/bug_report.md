---
name: Bug Report
about: Report a problem with Gecko Full Community Integration for Home Assistant
title: "[BUG] "
labels: bug
assignees: ''

---

## Describe the bug
A clear and concise description of what went wrong.

## To reproduce
Steps to reproduce the behavior:
1. Go to '...'
2. Click on '....'
3. Scroll down to '....'
4. See error

## Expected behavior
What you expected to happen.

## Screenshots
If applicable, add screenshots.

## Environment
- **Home Assistant:** [e.g. 2026.4.0]
- **Integration:** Gecko Full Community (`custom_components/gecko` — check **manifest.json** `version`, e.g. 2.1.6)
- **Spa / gateway:** [e.g. in.touch 3+, monitor firmware if known]
- **Relevant options:** [e.g. cloud REST poll interval, alerts poll interval, “only when MQTT down” on/off]

## Logs
Enable debug logging if possible:

```yaml
logger:
  default: info
  logs:
    custom_components.gecko: debug
```

Then paste the relevant section (redact tokens / URLs with embedded JWTs if you paste raw lines).

## Diagnostics and attachments (strongly recommended)

Please attach as much as you safely can:

1. **Download diagnostics** — **Settings → Devices & services → Gecko → ⋮ → Download diagnostics**. Includes connection state, coordinator health, shadow topology hints, **`spa_configuration_summary`** (accessory ↔ zone IDs from link-time REST), and entity snapshots. Skim the file before uploading.
2. **Relevant logs** — With `custom_components.gecko: debug`, paste a focused excerpt (redact tokens and any URL containing a JWT).
3. **Optional:** run **`scripts/verify_shadow_live.py`** from a Python venv (see the script’s header comment for `pip install` and env files) and attach **redacted** JSON or console output if the problem involves OAuth, REST, or MQTT outside HA.

## Additional context
MQTT vs REST behavior, automations using `gecko.*` actions, or anything else that helps reproduce the issue.
