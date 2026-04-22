# Archived pull request patches

## PR #1 (fork) — `feat: expose all additional API data from gecko_iot_client`

- **Original PR:** https://github.com/markus-lassfolk/ha-gecko-integration/pull/1  
- **Saved patch:** [pr1-feat-expose-additional-api-data.patch](pr1-feat-expose-additional-api-data.patch) (downloaded from `pull/1.patch`)

This work is **separate from shadow / Waterlab sensors**: it only surfaces data already modeled in `gecko_iot_client` (lights, climate status, fans, operation mode, energy-saving flag). It does **not** parse unknown shadow branches.

### Useful pieces to cherry-pick later (after reviewing)

| Area | Change |
| ---- | ------ |
| `select.py` | Listen for `EventChannel.OPERATION_MODE_UPDATE` so watercare mode updates immediately instead of waiting for the coordinator poll. |
| `climate.py` | Map all `TemperatureControlZoneStatus` values to `HVACAction` (heat pump cooling, defrost, etc.); `extra_state_attributes` for `detailed_status` and `eco_mode`. |
| `light.py` | RGB + brightness on `async_turn_on`; `ColorMode.RGB` when `rgbi` present; effect in attributes. **Note:** the PR patch contains a duplicated `elif callable(activate_method):` branch; fix if you apply it. |
| `fan.py` | `extra_state_attributes` with pump `initiators`. |
| `binary_sensor.py` | New `is_energy_saving` entity from `OperationModeController.is_energy_saving`; clearer early-return structure in `_update_state`. |

### How to re-apply

From repo root (on a branch with the pre-PR1 baseline, or resolve conflicts on `main`):

```bash
git apply --3way patches/pr1-feat-expose-additional-api-data.patch
```

Or cherry-pick the merge commit from the PR branch if you still have it locally:

```bash
git cherry-pick 20a7f4de95f6713381173b470685d45bd25ce2e0
```

(Confirm the SHA on GitHub if the PR branch was rewritten.)
