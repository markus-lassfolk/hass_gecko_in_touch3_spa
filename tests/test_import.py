"""Smoke import tests (no Home Assistant runtime)."""

from __future__ import annotations

from custom_components.gecko.const import DOMAIN


def test_domain_constant() -> None:
    """Assert the integration DOMAIN matches the expected slug."""
    assert DOMAIN == "gecko"
