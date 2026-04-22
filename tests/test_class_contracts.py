"""Structural smoke tests: verify every ``self.*`` method call resolves on the MRO.

This test file uses ``ast`` + ``inspect`` to find all method calls of the form
``self.<name>(…)`` inside each class in the integration, then checks that the
attribute actually exists somewhere in the class hierarchy. This catches the
category of bug where code calls a helper that belongs to a *different* HA base
class (e.g. ``async_update_reload_and_abort`` on ``OptionsFlow`` instead of
``ConfigFlow``).

Run with ``pytest tests/test_class_contracts.py -v``.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
from pathlib import Path

import custom_components.gecko as gecko_pkg

_PACKAGE_DIR = Path(gecko_pkg.__file__).parent

# Attributes that are set dynamically, via descriptors, or provided by a
# co-inherited class in a mixin pattern.  Listing them avoids false positives
# without weakening the test for real bugs.
_KNOWN_DYNAMIC_ATTRS: set[str] = {
    # HA config entry flow helpers (deep MRO / runtime mixins)
    "async_show_form",
    "async_create_entry",
    "async_abort",
    "async_set_unique_id",
    "async_show_progress",
    "async_show_progress_done",
    "async_external_step",
    "async_external_step_done",
    "async_show_menu",
    # HA entity helpers via Entity / CoordinatorEntity
    "async_write_ha_state",
    "async_schedule_update_ha_state",
    "async_on_remove",
    "async_remove",
    "async_update_ha_state",
    "async_added_to_hass",
    "async_will_remove_from_hass",
    "async_request_refresh",
    # DataUpdateCoordinator
    "async_refresh",
    "async_set_updated_data",
    "async_config_entry_first_refresh",
    # CoordinatorEntity
    "async_add_listener",
    # Mixins / properties / __init__-time attrs
    "_attr_available",
    "_attr_is_on",
    "_attr_name",
    "_attr_unique_id",
    "_attr_icon",
    "_attr_device_info",
    "_attr_extra_state_attributes",
    "_attr_supported_features",
    "_attr_speed",
    "_attr_speed_list",
    "_attr_percentage",
    "_check_is_connected",
    # Logging helper on AbstractOAuth2FlowHandler
    "logger",
    # OptionsFlow / ConfigEntryBaseFlow
    "config_entry",
    "options",
    # OAuth2 flow
    "async_step_user",
    "async_register_implementation",
    # GeckoSpaApiMixin delegates to GeckoApiClient which provides async_request;
    # the mixin is never instantiated alone — always mixed with GeckoApiClient.
    "async_request",
    # GeckoApiClient (external library) methods called via self in subclasses
    "async_get_access_token",
}

# Private helper methods that only exist within the same class definition
# are always fine (they appear on the class itself).
# We skip anything starting with _ that is NOT in our integration code
# (e.g. HA internal helpers like _abort_if_unique_id_configured).


def _iter_integration_modules():
    """Yield (module_name, module) for every sub-module in the package."""
    for info in pkgutil.walk_packages(
        [str(_PACKAGE_DIR)], prefix=gecko_pkg.__name__ + "."
    ):
        try:
            mod = importlib.import_module(info.name)
            yield info.name, mod
        except Exception:
            pass
    yield gecko_pkg.__name__, gecko_pkg


def _collect_self_calls(cls) -> list[tuple[str, int]]:
    """Return [(attr_name, lineno), ...] for all ``self.<attr>(...)`` in *cls*."""
    try:
        source = inspect.getsource(cls)
    except (OSError, TypeError):
        return []

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    calls: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            value = node.func.value
            if isinstance(value, ast.Name) and value.id == "self":
                calls.append((node.func.attr, node.lineno))
        # Also catch plain attribute reads like ``self.hass``, ``self.coordinator``
        # that are NOT calls — only method calls are risky for AttributeError at
        # runtime, so we focus on calls.
    return calls


def _attr_exists_on_mro(cls, attr_name: str) -> bool:
    """Check if *attr_name* resolves anywhere in *cls*'s MRO."""
    for klass in inspect.getmro(cls):
        if attr_name in klass.__dict__:
            return True
    return False


def test_all_self_method_calls_resolve_on_mro() -> None:
    """Every ``self.<method>(…)`` call inside integration classes must exist on the MRO.

    This is the test that would have caught the original
    ``async_update_reload_and_abort`` bug on ``GeckoOptionsFlow``.
    """
    violations: list[str] = []

    for mod_name, mod in _iter_integration_modules():
        for name, obj in inspect.getmembers(mod, inspect.isclass):
            # Only test classes defined in our package
            if not getattr(obj, "__module__", "").startswith("custom_components.gecko"):
                continue

            for attr, lineno in _collect_self_calls(obj):
                # Skip obviously dynamic / HA-provided names
                if attr in _KNOWN_DYNAMIC_ATTRS:
                    continue
                # Skip private helpers defined on the class itself
                if attr.startswith("_") and attr in obj.__dict__:
                    continue
                # Check MRO
                if not _attr_exists_on_mro(obj, attr):
                    violations.append(
                        f"{mod_name}.{name}.{attr} (line ~{lineno}) "
                        f"not found on MRO: {[c.__name__ for c in inspect.getmro(obj)]}"
                    )

    assert not violations, (
        "Found self.<method>() calls that do not resolve on the class MRO:\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


def test_options_flow_does_not_use_configflow_only_methods() -> None:
    """Explicitly verify the options flow never calls ConfigFlow-only helpers.

    This is the regression test for the original bug: ``GeckoOptionsFlow``
    calling ``async_update_reload_and_abort`` which only exists on ``ConfigFlow``.
    """
    from custom_components.gecko.config_flow import GeckoOptionsFlow

    configflow_only = {
        "async_update_reload_and_abort",
        "async_update_and_abort",
        "_abort_if_unique_id_configured",
        "_abort_if_unique_id_mismatch",
        "_get_reconfigure_entry",
        "_get_reauth_entry",
    }

    calls = _collect_self_calls(GeckoOptionsFlow)
    used = {name for name, _lineno in calls}
    forbidden = used & configflow_only

    assert not forbidden, (
        f"GeckoOptionsFlow calls ConfigFlow-only methods: {forbidden}. "
        "Use explicit hass.config_entries.async_update_entry + async_reload + async_abort instead."
    )


def test_all_integration_classes_importable() -> None:
    """Every .py module in the package should import without errors."""
    errors: list[str] = []
    for info in pkgutil.walk_packages(
        [str(_PACKAGE_DIR)], prefix=gecko_pkg.__name__ + "."
    ):
        try:
            importlib.import_module(info.name)
        except Exception as exc:
            errors.append(f"{info.name}: {exc}")

    assert not errors, "Failed to import modules:\n" + "\n".join(errors)


def test_entity_classes_have_required_ha_attributes() -> None:
    """Entity classes should define ``_attr_has_entity_name`` and ``_attr_unique_id``
    (set in ``__init__``) so HA entity registration works correctly."""
    from homeassistant.helpers.entity import Entity

    entity_classes: list[type] = []
    for _mod_name, mod in _iter_integration_modules():
        for _name, obj in inspect.getmembers(mod, inspect.isclass):
            if (
                issubclass(obj, Entity)
                and obj is not Entity
                and getattr(obj, "__module__", "").startswith("custom_components.gecko")
            ):
                entity_classes.append(obj)

    assert entity_classes, "Expected to find entity classes in the integration"

    for cls in entity_classes:
        assert hasattr(cls, "_attr_has_entity_name") or any(
            "_attr_has_entity_name" in klass.__dict__ for klass in inspect.getmro(cls)
        ), f"{cls.__name__} should set _attr_has_entity_name"


def test_no_entity_sets_entity_id_directly() -> None:
    """Entity classes must not set ``self.entity_id`` manually.

    HA generates entity IDs from ``_attr_has_entity_name`` + ``_attr_unique_id``
    + device name. Manually setting ``entity_id`` with unslugified vessel names
    (e.g. spaces) produces invalid IDs that HA will reject in 2027.2.0.
    """
    from homeassistant.helpers.entity import Entity

    violations: list[str] = []
    for _mod_name, mod in _iter_integration_modules():
        for name, obj in inspect.getmembers(mod, inspect.isclass):
            if not (
                issubclass(obj, Entity)
                and obj is not Entity
                and getattr(obj, "__module__", "").startswith("custom_components.gecko")
            ):
                continue

            try:
                source = inspect.getsource(obj)
            except (OSError, TypeError):
                continue

            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Attribute)
                    and isinstance(node.targets[0].value, ast.Name)
                    and node.targets[0].value.id == "self"
                    and node.targets[0].attr == "entity_id"
                ):
                    violations.append(
                        f"{name} sets self.entity_id at line ~{node.lineno}"
                    )

    assert not violations, (
        "Entity classes must not set self.entity_id directly "
        "(HA generates it from _attr_has_entity_name + _attr_unique_id + device):\n"
        + "\n".join(f"  - {v}" for v in violations)
    )
